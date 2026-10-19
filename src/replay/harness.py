"""
Replay harness: feeds triggers in deliver_at order, injects control events
and model faults, measures latency, and writes decisions.jsonl + actions.jsonl.

Control events handled:
  - title_withdrawn        : calls data_pack.withdraw_title()
  - goal_rule_corrected    : calls data_pack.apply_goal_rule_correction()
  - dependency_unavailable : simulates full outage (model provider times out)
  - dependency_degraded    : simulates slow responses

The harness runs triggers sequentially (single-threaded replay).
For each trigger:
  1. Apply any pending control events that precede deliver_at.
  2. Apply any live events that precede deliver_at (update events_by_user).
  3. Route the trigger to the correct agent.
  4. Write the decision and action (if any) to output files.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from src.agent.agent import Agent
from src.agent.state_store import StateStore
from src.data.loader import DataPack
from src.model.mock_provider import MockModelProvider

log = logging.getLogger(__name__)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

_ACTION_ID_COUNTER = [0]


def _next_action_id() -> str:
    _ACTION_ID_COUNTER[0] += 1
    return f"A-{_ACTION_ID_COUNTER[0]:04d}"


def _parse_ts(s) -> Optional[datetime]:
    if not s:
        return None
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def run_replay(
    data_pack: DataPack,
    state_store: StateStore,
    out_decisions: str,
    out_actions: str,
    max_model_calls: int = 8,
    max_tool_calls: int = 20,
    progress_every: int = 100,
) -> dict:
    """
    Run the full replay. Returns a summary dict.

    Parameters
    ----------
    data_pack      : loaded DataPack
    state_store    : StateStore for persisting decisions
    out_decisions  : path to write decisions.jsonl
    out_actions    : path to write actions.jsonl
    max_model_calls: per-decision model call budget
    max_tool_calls : per-decision tool call budget
    progress_every : log progress every N triggers
    """
    os.makedirs(os.path.dirname(out_decisions), exist_ok=True)
    os.makedirs(os.path.dirname(out_actions), exist_ok=True)

    # Build shared model provider with fault dict
    model = MockModelProvider(
        faults=data_pack.model_faults,
        max_calls=max_model_calls,
    )

    # Cache agents (one per user)
    agents: Dict[str, Agent] = {}

    # Merge triggers and live events into one timeline, sorted by deliver_at / received_at
    all_triggers = list(data_pack.triggers)
    ctrl_iter = iter(data_pack.control_events)
    live_iter = iter(data_pack.live_events)

    next_ctrl = next(ctrl_iter, None)
    next_live = next(live_iter, None)

    # Track outage windows from dependency control events
    outage_active = False
    outage_ends_at: Optional[datetime] = None

    summary = {
        "triggers_total": len(all_triggers),
        "decisions_published": 0,
        "actions_published": 0,
        "abstentions": 0,
        "holdout_blocked": 0,
        "fallbacks_used": 0,
        "latency_fail": 0,
        "latency_warn": 0,
        "latency_ok": 0,
        "processing_time_ms": [],
    }

    with open(out_decisions, "w", encoding="utf-8") as f_dec, \
         open(out_actions, "w", encoding="utf-8") as f_act:

        for i, trigger in enumerate(all_triggers):
            deliver_at = trigger.get("_deliver_at") or _parse_ts(trigger.get("deliver_at"))

            # ---------------------------------------------------------------- #
            # Apply pending control events before this trigger                 #
            # ---------------------------------------------------------------- #
            while next_ctrl and next_ctrl.get("_at") and next_ctrl["_at"] <= deliver_at:
                _apply_control_event(next_ctrl, data_pack, model)
                if next_ctrl["type"] == "dependency_unavailable":
                    until = _parse_ts(next_ctrl.get("until"))
                    if until:
                        outage_active = True
                        outage_ends_at = until
                        log.warning("Dependency outage starts at %s until %s",
                                    next_ctrl["_at"], until)
                elif next_ctrl["type"] == "dependency_degraded":
                    log.warning("Dependency degraded at %s", next_ctrl["_at"])
                next_ctrl = next(ctrl_iter, None)

            # Check if outage has ended
            if outage_active and outage_ends_at and deliver_at > outage_ends_at:
                outage_active = False
                log.warning("Dependency outage ended at %s", deliver_at)

            # ---------------------------------------------------------------- #
            # Apply pending live events before this trigger                    #
            # ---------------------------------------------------------------- #
            while next_live and next_live.get("_received_at") and next_live["_received_at"] <= deliver_at:
                uid = next_live.get("user_id")
                if uid:
                    data_pack.events_by_user[uid].append(next_live)
                next_live = next(live_iter, None)

            # ---------------------------------------------------------------- #
            # Get or create agent for this user                               #
            # ---------------------------------------------------------------- #
            user_id = trigger.get("user_id")
            if user_id not in agents:
                agents[user_id] = Agent(
                    user_id=user_id,
                    data_pack=data_pack,
                    state_store=state_store,
                    model=model,
                    max_tool_calls=max_tool_calls,
                )

            agent = agents[user_id]

            # ---------------------------------------------------------------- #
            # Process trigger                                                  #
            # ---------------------------------------------------------------- #
            t0 = time.perf_counter()
            try:
                decision = agent.process_trigger(trigger)
            except Exception as e:
                log.error("Unhandled agent error for user %s trigger %s: %s",
                          user_id, trigger.get("event_id"), e)
                decision = _error_decision(user_id, trigger, str(e), data_pack)

            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            summary["processing_time_ms"].append(elapsed_ms)

            # ---------------------------------------------------------------- #
            # Write decision                                                   #
            # ---------------------------------------------------------------- #
            f_dec.write(json.dumps(decision, ensure_ascii=False) + "\n")
            summary["decisions_published"] += 1

            # Track latency
            ls = decision.get("latency_status", "OK")
            if ls == "FAILED":
                summary["latency_fail"] += 1
            elif ls == "WARN":
                summary["latency_warn"] += 1
            else:
                summary["latency_ok"] += 1

            if decision.get("fallback_used"):
                summary["fallbacks_used"] += 1

            # ---------------------------------------------------------------- #
            # Write action (if any)                                            #
            # ---------------------------------------------------------------- #
            if decision.get("action") != "NONE" and decision.get("action_payload"):
                arm = decision.get("arm", "agent")
                if arm != "holdout":
                    action_record = _build_action_record(decision, deliver_at)
                    f_act.write(json.dumps(action_record, ensure_ascii=False) + "\n")
                    summary["actions_published"] += 1
                else:
                    summary["holdout_blocked"] += 1
            else:
                summary["abstentions"] += 1
                if decision.get("arm") == "holdout":
                    summary["holdout_blocked"] += 1

            if (i + 1) % progress_every == 0:
                log.info("Replay progress: %d / %d triggers processed",
                         i + 1, len(all_triggers))

    # Compute latency stats
    if summary["processing_time_ms"]:
        times = sorted(summary["processing_time_ms"])
        n = len(times)
        summary["latency_p50_ms"] = times[n // 2]
        summary["latency_p95_ms"] = times[int(n * 0.95)]
        summary["latency_max_ms"] = times[-1]

    log.info("Replay complete: %d decisions, %d actions, %d abstentions",
             summary["decisions_published"], summary["actions_published"], summary["abstentions"])
    return summary


def _apply_control_event(ctrl: dict, data_pack: DataPack, model: MockModelProvider):
    """Apply one control event to the data pack or model."""
    etype = ctrl.get("type")
    at = ctrl.get("_at")
    log.warning("Control event at %s: %s", at, ctrl)

    if etype == "title_withdrawn":
        title_id = ctrl.get("title_id")
        if title_id and at:
            data_pack.withdraw_title(title_id, at)

    elif etype == "goal_rule_corrected":
        data_pack.apply_goal_rule_correction(ctrl)

    elif etype in ("dependency_unavailable", "dependency_degraded"):
        # Handled by the caller (outage window tracking)
        pass


def _build_action_record(decision: dict, deliver_at: datetime) -> dict:
    """Convert a decision into an action record for actions.jsonl / simulator."""
    payload = decision.get("action_payload", {})
    action_id = _next_action_id()
    action_type = decision.get("action", "").lower()

    record = {
        "action_id": action_id,
        "decision_id": decision["decision_id"],
        "user_id": decision["viewer_id"],
        "trigger_event_id": decision.get("trigger", {}).get("event_id"),
        "type": action_type,
        "published_at": decision.get("decided_at"),
        "dialect": decision.get("dialect", {}).get("used"),
    }

    if action_type == "home_page":
        record["hero_title_id"] = payload.get("hero_title_id")
        record["rows"] = payload.get("rows", [])

    elif action_type == "push":
        record["title_id"] = payload.get("title_id")
        record["text"] = payload.get("text", {})
        record["deeplink"] = payload.get("deeplink", "")
        record["send_at"] = payload.get("send_at", decision.get("decided_at"))

    elif action_type == "whatsapp":
        record["template_id"] = payload.get("template_id")
        record["variables"] = payload.get("variables", {})
        record["title_id"] = payload.get("title_id")
        record["send_at"] = payload.get("send_at", decision.get("decided_at"))

    elif action_type == "in_app_message":
        record["title_id"] = payload.get("title_id")
        record["text"] = payload.get("text", {})

    return record


def _error_decision(user_id: str, trigger: dict, error_msg: str, data_pack) -> dict:
    """Fallback decision when the agent crashes unexpectedly."""
    return {
        "decision_id": f"D-ERR-{trigger.get('event_id', 'unknown')[:8].upper()}",
        "viewer_id": user_id,
        "trigger": {"event": trigger.get("event"), "event_id": trigger.get("event_id"),
                    "received_at": trigger.get("received_at")},
        "goal": None,
        "action": "NONE",
        "dialect": {"used": trigger.get("app_dialect"), "source": "trigger", "conflicts": []},
        "content": [],
        "confidence": 0.0,
        "confidence_reason": f"agent_error:{error_msg[:100]}",
        "evidence": [],
        "assumptions": ["agent raised exception — safe fallback to no action"],
        "checks": {"passed": [], "failed": ["agent_exception"], "notes": [error_msg[:200]]},
        "decided_in_ms": 0,
        "decided_at": trigger.get("deliver_at") or trigger.get("received_at"),
        "status": "FALLBACK",
        "arm": data_pack.arms.get(user_id, "unknown"),
        "model_calls": 0,
        "tool_calls": 0,
        "rule_version": data_pack.goals_version,
        "fallback_used": True,
        "no_action_reason": f"agent_exception: {error_msg[:100]}",
        "action_payload": None,
    }
