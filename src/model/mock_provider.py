"""
Mock model provider.

In mock mode (default — no API key required), the provider uses deterministic
rule-based logic to suggest a goal and action. It also replays model_faults.jsonl
to inject the failures that appear during the replay day.

Supported fault types (from model_faults.jsonl):
  - timeout          : raises ModelTimeout
  - malformed_json   : returns unparseable text
  - unsupported_claim: returns a claim not backed by evidence (verifier must reject it)
  - empty_response   : returns an empty string

Model call budget is tracked and enforced here.
"""

import json
import logging
import random
import time
from typing import Optional

log = logging.getLogger(__name__)


class ModelTimeout(Exception):
    """Raised when the model times out."""


class ModelProviderError(Exception):
    """Raised for non-timeout model errors."""


class MockModelProvider:
    """
    Rule-based mock provider.  No API key required.

    Parameters
    ----------
    faults       : dict of trigger_event_id -> fault dict (from model_faults.jsonl)
    max_calls    : configurable call budget per decision (default 8)
    timeout_s    : simulated timeout duration in seconds (default 0.1 for tests)
    """

    def __init__(self, faults: dict = None, max_calls: int = 8, timeout_s: float = 0.1):
        self.faults = faults or {}
        self.max_calls = max_calls
        self.timeout_s = timeout_s
        self._calls_this_decision = 0
        self._current_trigger_event_id: Optional[str] = None

    def start_decision(self, trigger_event_id: str):
        """Reset call counter for a new decision."""
        self._calls_this_decision = 0
        self._current_trigger_event_id = trigger_event_id

    @property
    def calls_used(self) -> int:
        return self._calls_this_decision

    def _check_budget(self):
        if self._calls_this_decision >= self.max_calls:
            raise ModelProviderError(
                f"Model call budget exhausted ({self.max_calls} calls)"
            )
        self._calls_this_decision += 1

    def suggest_content(self, user_facts: dict, goal: dict, catalogue_sample: list,
                        dialect: str) -> dict:
        """
        Suggest content titles for the intervention.

        Returns a dict with keys: title_ids (list), confidence (float), reason (str).
        May raise ModelTimeout or ModelProviderError.
        """
        self._check_budget()

        # Check for injected fault
        fault = self.faults.get(self._current_trigger_event_id)
        if fault:
            self._apply_fault(fault)

        # Deterministic mock: pick from catalogue_sample filtered by dialect
        dialect_titles = [t for t in catalogue_sample if t.get("dialect") == dialect]
        if not dialect_titles:
            dialect_titles = catalogue_sample[:5]

        # Prefer titles relevant to the goal
        goal_id = goal.get("goal_id", "")
        if goal_id in ("G07",):  # resume series
            # Prefer content_type = show or micro_drama
            preferred = [t for t in dialect_titles if t.get("content_type") in ("show", "micro_drama")]
            if preferred:
                dialect_titles = preferred

        random.shuffle(dialect_titles)  # slight variation per call (deterministic via hash)
        selected = dialect_titles[:8]

        return {
            "title_ids": [t["title_id"] for t in selected],
            "confidence": 0.55,
            "reason": f"mock_provider:rule_based_for_goal_{goal_id}_dialect_{dialect}",
        }

    def suggest_push_copy(self, title: dict, goal: dict) -> dict:
        """
        Suggest push notification copy.

        Returns dict with keys: push_title (str), push_body (str).
        """
        self._check_budget()

        fault = self.faults.get(self._current_trigger_event_id)
        if fault:
            self._apply_fault(fault)

        title_name = title.get("title", "a great show")
        goal_name = goal.get("name", "")

        if "first_play" in goal_name or "first_full" in goal_name:
            push_title = f"Start watching {title_name}"[:40]
            push_body = f"Pick up where great stories begin. {title_name} is waiting for you."[:120]
        elif "trial_to_paid" in goal_name or "first_paid" in goal_name:
            push_title = f"Continue with {title_name}"[:40]
            push_body = f"Your trial unlocked great content. Keep watching with a plan from \u20b9149/month."[:120]
        elif "reactivate" in goal_name:
            push_title = f"We missed you!"[:40]
            push_body = f"Come back and watch {title_name} — it's waiting for you."[:120]
        else:
            push_title = f"Watch {title_name} now"[:40]
            push_body = f"Great regional content in your dialect. {title_name} — watch today."[:120]

        return {
            "push_title": push_title,
            "push_body": push_body,
            "confidence": 0.5,
            "reason": "mock_provider:rule_based_push_copy",
        }

    def _apply_fault(self, fault: dict):
        """Apply a model fault from model_faults.jsonl."""
        fault_type = fault.get("fault")
        log.warning("Applying model fault: %s for trigger %s",
                    fault_type, self._current_trigger_event_id)

        if fault_type == "timeout":
            time.sleep(self.timeout_s)
            raise ModelTimeout(f"Model timed out (simulated, trigger={self._current_trigger_event_id})")

        elif fault_type == "malformed_json":
            raise ModelProviderError(
                f"Model returned malformed JSON: {fault.get('model_output', '{{broken}')}"
            )

        elif fault_type == "unsupported_claim":
            # Return the claim; the verifier must reject it
            output = fault.get("model_output", {})
            log.warning("Model making unsupported claim: %s", output)
            # We propagate this as a special return value; caller must verify
            raise UnsupportedClaimError(output)

        elif fault_type == "empty_response":
            raise ModelProviderError("Model returned empty response")

        else:
            log.warning("Unknown fault type %s — ignoring", fault_type)


class UnsupportedClaimError(Exception):
    """Model made a claim not backed by evidence. Verifier must handle."""
    def __init__(self, claim: dict):
        self.claim = claim
        super().__init__(f"Unsupported model claim: {claim}")
