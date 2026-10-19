"""
Agent orchestrator: owns one viewer's goal, plan, decisions, actions, and history.

The agent is stateful per viewer. It:
  1. Builds viewer facts from events + profile (via fact_resolver).
  2. Selects one goal (via goal_planner).
  3. Plans an intervention (via intervention planner).
  4. Verifies the proposed action (via verifier — independent check).
  5. Publishes or abstains with a recorded reason.
  6. Saves the decision to the state store (idempotent).

Idempotency: the decision_id is derived from the trigger event_id so that
replaying the same trigger twice produces exactly the same decision and no
duplicate actions.
"""

import logging
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from src.agent.fact_resolver import resolve_facts, ViewerFacts
from src.agent.goal_planner import select_goal, goal_deadline, goal_success_signal
from src.agent.intervention import plan_intervention
from src.agent.verifier import verify_action
from src.agent.state_store import StateStore
from src.model.mock_provider import MockModelProvider

log = logging.getLogger(__name__)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

# Latency budget: warn at 30s, fail at 300s (5 min)
LATENCY_WARN_S = 30
LATENCY_FAIL_S = 300


class Agent:
    """
    One agent instance per viewer.
    Created by the replay harness for each triggered user.
    """

    def __init__(self, user_id: str, data_pack, state_store: StateStore,
                 model: MockModelProvider, max_tool_calls: int = 20):
        self.user_id = user_id
        self.data_pack = data_pack
        self.store = state_store
        self.model = model
        self.max_tool_calls = max_tool_calls
        self._tool_calls = 0

    def process_trigger(self, trigger: dict) -> dict:
        """
        Process one app_open / login trigger and return the decision dict.

        The decision is saved to the state store.
        If the same trigger_event_id is processed again, returns the cached decision.
        """
        start_time = time.perf_counter()
        trigger_event_id = trigger.get("event_id")
        decision_id = f"D-{trigger_event_id[:8].upper()}"

        # ---------------------------------------------------------------- #
        # Idempotency: return existing decision if already processed        #
        # ---------------------------------------------------------------- #
        existing = self.store.get_decision(decision_id)
        if existing:
            log.debug("Duplicate trigger %s — returning cached decision", trigger_event_id)
            return existing

        deliver_at_str = trigger.get("deliver_at") or trigger.get("received_at")
        deliver_at = _parse_ts(deliver_at_str)

        # ---------------------------------------------------------------- #
        # Determine arm                                                     #
        # ---------------------------------------------------------------- #
        arm = self.data_pack.arms.get(self.user_id, "agent")
        self.store.upsert_viewer(self.user_id, arm)

        # ---------------------------------------------------------------- #
        # Holdout fast-path                                                 #
        # ---------------------------------------------------------------- #
        if arm == "holdout":
            decision = self._build_none_decision(
                decision_id, trigger, deliver_at, "holdout_arm_receives_no_actions",
                start_time, arm
            )
            self.store.save_decision(decision)
            return decision

        # ---------------------------------------------------------------- #
        # Step 1: Resolve facts                                             #
        # ---------------------------------------------------------------- #
        profile = self.data_pack.profiles.get(self.user_id)
        events = self.data_pack.user_events(self.user_id)
        self._tool_calls += 1  # loading events counts as a tool call

        facts = resolve_facts(
            self.user_id, profile, events, trigger, self.data_pack.catalogue
        )

        # Stale trigger check: if offline-buffered, downgrade
        sent_at = _parse_ts(trigger.get("sent_at"))
        received_at = _parse_ts(trigger.get("received_at"))
        if sent_at and received_at and (received_at - sent_at).total_seconds() > 600:
            log.info("Stale trigger for user %s: lag=%ds — recording but not treating as live session",
                     self.user_id, (received_at - sent_at).total_seconds())
            decision = self._build_none_decision(
                decision_id, trigger, deliver_at,
                f"stale_offline_buffered_trigger (lag={(received_at-sent_at).total_seconds():.0f}s)",
                start_time, arm
            )
            self.store.save_decision(decision)
            return decision

        # ---------------------------------------------------------------- #
        # Step 2: Select goal                                               #
        # ---------------------------------------------------------------- #
        viewer_row = self.store.get_viewer(self.user_id)
        current_goal_id = viewer_row.get("current_goal_id") if viewer_row else None
        current_goal_status = viewer_row.get("goal_status") if viewer_row else None
        current_goal_deadline = viewer_row.get("goal_deadline") if viewer_row else None

        goal, goal_reason, assumptions = select_goal(
            facts, self.data_pack.goals, deliver_at,
            current_goal_id=current_goal_id,
            current_goal_status=current_goal_status,
            current_goal_deadline=current_goal_deadline,
        )

        if goal is None:
            decision = self._build_none_decision(
                decision_id, trigger, deliver_at, f"no_goal: {goal_reason}",
                start_time, arm, facts=facts, assumptions=assumptions
            )
            self.store.save_decision(decision)
            return decision

        # Update state store with new/current goal
        deadline = goal_deadline(goal, deliver_at)
        success_signal = goal_success_signal(goal)
        self.store.set_active_goal(
            self.user_id, goal["goal_id"],
            deliver_at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            deadline.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            success_signal
        )

        # ---------------------------------------------------------------- #
        # Step 3: Plan intervention                                         #
        # ---------------------------------------------------------------- #
        self.model.start_decision(trigger_event_id)

        # Count messages sent today (IST)
        ist_today = deliver_at.astimezone(IST).strftime("%Y-%m-%d")
        messages_today = _count_messages_today(self.store, self.user_id, ist_today)

        try:
            intervention = plan_intervention(
                facts=facts,
                goal=goal,
                publish_at=deliver_at,
                data_pack=self.data_pack,
                model=self.model,
                messages_sent_today=messages_today,
                tool_calls_used=self._tool_calls,
                max_tool_calls=self.max_tool_calls,
            )
        except Exception as e:
            log.error("Intervention planning failed for user %s: %s", self.user_id, e)
            decision = self._build_none_decision(
                decision_id, trigger, deliver_at,
                f"intervention_planning_error: {type(e).__name__}",
                start_time, arm, facts=facts, goal=goal, assumptions=assumptions
            )
            self.store.save_decision(decision)
            return decision

        # ---------------------------------------------------------------- #
        # Step 4: Verify                                                    #
        # ---------------------------------------------------------------- #
        verification = verify_action(
            action_type=intervention.action_type,
            action_payload=intervention.payload,
            facts=facts,
            arm=arm,
            publish_at=deliver_at,
            catalogue=self.data_pack.catalogue,
            injection_flags=self.data_pack.injection_flags,
            messages_sent_today=messages_today,
            model_claim=intervention.model_claim,
        )

        # ---------------------------------------------------------------- #
        # Step 5: Build and publish decision                                #
        # ---------------------------------------------------------------- #
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)

        if verification.passed and intervention.action_type != "NONE":
            action = intervention.action_type
            status = "PUBLISHED"
        else:
            if not verification.passed:
                log.info("Action REJECTED for user %s: %s", self.user_id, verification.checks_failed)
                action = "NONE"
                status = "REJECTED"
                intervention.no_action_reason = f"verifier_rejected: {verification.checks_failed}"
            else:
                action = "NONE"
                status = "PUBLISHED"

        # Dialect conflict info
        dialect_info = {
            "used": facts.dialect,
            "source": facts.dialect_source,
            "conflicts": [
                f"{c.get('conflict_source') or c.get('source')}:{c.get('conflict_value') or c.get('value')}"
                for c in facts.dialect_conflicts
            ],
        }

        # Collect content references
        content_refs = [f"title:{tid}" for tid in (intervention.title_ids or [])]

        decision = {
            "decision_id": decision_id,
            "viewer_id": self.user_id,
            "trigger": {
                "event": trigger.get("event"),
                "event_id": trigger_event_id,
                "received_at": trigger.get("received_at"),
                "event_age_s": int((received_at - sent_at).total_seconds()) if (received_at and sent_at) else 0,
            },
            "goal": {
                "id": goal["goal_id"],
                "name": goal.get("name"),
                "deadline": deadline.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "success_signal": success_signal,
            },
            "action": action,
            "dialect": dialect_info,
            "content": content_refs,
            "confidence": round(intervention.confidence, 3),
            "confidence_reason": intervention.confidence_reason,
            "evidence": facts.evidence_refs[:10],  # cap for readability
            "assumptions": assumptions,
            "checks": {
                "passed": verification.checks_passed,
                "failed": verification.checks_failed,
                "notes": verification.notes,
            },
            "decided_in_ms": elapsed_ms,
            "decided_at": deliver_at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "status": status,
            "arm": arm,
            "model_calls": self.model.calls_used,
            "tool_calls": self._tool_calls,
            "rule_version": self.data_pack.goals_version,
            "fallback_used": intervention.fallback_used,
            "no_action_reason": intervention.no_action_reason,
            "action_payload": intervention.payload if action != "NONE" else None,
        }

        if elapsed_ms > LATENCY_FAIL_S * 1000:
            decision["latency_status"] = "FAILED"
        elif elapsed_ms > LATENCY_WARN_S * 1000:
            decision["latency_status"] = "WARN"
        else:
            decision["latency_status"] = "OK"

        # Save facts to state store
        for ref in facts.evidence_refs:
            parts = ref.split(":")
            self.store.upsert_fact(
                self.user_id, parts[0] if parts else "unknown",
                ref, "evidence_ref", deliver_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            )

        self.store.save_decision(decision)

        # Update last action tracking
        if action != "NONE":
            self.store.conn.execute(
                "UPDATE viewers SET last_action_type=?, last_action_at=?, last_action_day_ist=? WHERE user_id=?",
                (action, deliver_at.isoformat(), ist_today, self.user_id)
            )
            self.store.conn.commit()

        log.info("Decision %s for %s: action=%s goal=%s in %dms [%s]",
                 decision_id, self.user_id, action, goal["goal_id"], elapsed_ms,
                 decision.get("latency_status"))
        return decision

    def _build_none_decision(self, decision_id, trigger, deliver_at, reason,
                              start_time, arm, facts=None, goal=None, assumptions=None):
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        deliver_at_str = deliver_at.strftime("%Y-%m-%dT%H:%M:%S.000Z") if deliver_at else None
        sent_at = _parse_ts(trigger.get("sent_at"))
        received_at = _parse_ts(trigger.get("received_at"))
        return {
            "decision_id": decision_id,
            "viewer_id": self.user_id,
            "trigger": {
                "event": trigger.get("event"),
                "event_id": trigger.get("event_id"),
                "received_at": trigger.get("received_at"),
                "event_age_s": int((received_at - sent_at).total_seconds()) if (received_at and sent_at) else 0,
            },
            "goal": {"id": goal["goal_id"], "name": goal.get("name")} if goal else None,
            "action": "NONE",
            "dialect": {"used": facts.dialect if facts else None,
                        "source": facts.dialect_source if facts else None,
                        "conflicts": []},
            "content": [],
            "confidence": 0.0,
            "confidence_reason": reason,
            "evidence": facts.evidence_refs[:5] if facts else [],
            "assumptions": assumptions or [],
            "checks": {"passed": [], "failed": [], "notes": []},
            "decided_in_ms": elapsed_ms,
            "decided_at": deliver_at_str,
            "status": "PUBLISHED",
            "arm": arm,
            "model_calls": 0,
            "tool_calls": self._tool_calls,
            "rule_version": self.data_pack.goals_version,
            "fallback_used": False,
            "no_action_reason": reason,
            "action_payload": None,
        }


def _count_messages_today(store: StateStore, user_id: str, ist_date: str) -> int:
    """Count push + whatsapp already published for this viewer today (IST)."""
    count = store.conn.execute("""
        SELECT COUNT(*) FROM decisions
        WHERE user_id=? AND action_type IN ('PUSH', 'WHATSAPP')
        AND substr(decided_at, 1, 10) = ? AND status='PUBLISHED'
    """, (user_id, ist_date)).fetchone()[0]
    return count


def _parse_ts(s) -> Optional[datetime]:
    if not s:
        return None
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
