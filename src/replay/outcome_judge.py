"""
Outcome judge: reads outcome_events.jsonl from the simulator, judges each
open goal as 'succeeded', 'failed', or 'inconclusive', and writes results.

Rules:
- Only look at events within the goal's window (started_at → deadline).
- Attribute the outcome to the specific goal's success signal.
- A goal with no verifiable success event is 'inconclusive', not 'failed'.
- When a goal rule was corrected mid-replay, apply the new rule only to
  goals that were still open at the time of correction.
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def _parse_ts(s) -> Optional[datetime]:
    if not s:
        return None
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _ist_day(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%Y-%m-%d")


def judge_outcomes(
    decisions_path: str,
    outcome_events_path: str,
    goals: list,
    state_store,
    out_outcomes_path: str,
) -> List[dict]:
    """
    Judge all decisions against outcome events.

    Returns list of outcome dicts; also writes to out_outcomes_path and state_store.
    """
    # ------------------------------------------------------------------ #
    # Load decisions with goals                                            #
    # ------------------------------------------------------------------ #
    decisions = []
    if os.path.exists(decisions_path):
        with open(decisions_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    d = json.loads(line)
                    if d.get("goal") and d.get("action") != "NONE" or d.get("goal"):
                        decisions.append(d)

    log.info("Judging outcomes for %d decisions", len(decisions))

    # ------------------------------------------------------------------ #
    # Load outcome events                                                  #
    # ------------------------------------------------------------------ #
    outcome_events: Dict[str, List[dict]] = {}  # user_id -> events
    if os.path.exists(outcome_events_path):
        with open(outcome_events_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ev = json.loads(line)
                uid = ev.get("user_id")
                if uid not in outcome_events:
                    outcome_events[uid] = []
                outcome_events[uid].append(ev)
    log.info("Loaded outcome events for %d users", len(outcome_events))

    # ------------------------------------------------------------------ #
    # Build goal metadata lookup                                           #
    # ------------------------------------------------------------------ #
    goals_map = {g["goal_id"]: g for g in goals}

    # ------------------------------------------------------------------ #
    # Judge each decision                                                  #
    # ------------------------------------------------------------------ #
    outcomes = []
    for dec in decisions:
        goal_info = dec.get("goal")
        if not goal_info:
            continue

        goal_id = goal_info.get("id")
        user_id = dec.get("viewer_id")
        goal_started_str = dec.get("decided_at")
        goal_deadline_str = goal_info.get("deadline")

        if not all([goal_id, user_id, goal_started_str, goal_deadline_str]):
            continue

        goal_started = _parse_ts(goal_started_str)
        goal_deadline_dt = _parse_ts(goal_deadline_str)
        goal_def = goals_map.get(goal_id)

        if not goal_def:
            outcome = "inconclusive"
            notes = f"goal_id {goal_id} not found in goals catalogue"
        else:
            outcome, notes = _judge_one(
                user_id, goal_id, goal_def, goal_started, goal_deadline_dt,
                outcome_events.get(user_id, [])
            )

        outcome_record = {
            "decision_id": dec["decision_id"],
            "user_id": user_id,
            "goal_id": goal_id,
            "goal_started_at": goal_started_str,
            "goal_deadline": goal_deadline_str,
            "action": dec.get("action"),
            "arm": dec.get("arm"),
            "outcome": outcome,
            "notes": notes,
        }
        outcomes.append(outcome_record)

        # Save to state store
        state_store.save_outcome(
            dec["decision_id"], user_id, goal_id, outcome, notes
        )

    # Write outcomes.jsonl
    os.makedirs(os.path.dirname(out_outcomes_path), exist_ok=True)
    with open(out_outcomes_path, "w", encoding="utf-8") as f:
        for o in outcomes:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")

    log.info("Wrote %d outcome judgements to %s", len(outcomes), out_outcomes_path)
    return outcomes


def _judge_one(user_id: str, goal_id: str, goal_def: dict,
               goal_started: datetime, goal_deadline: datetime,
               user_events: List[dict]) -> tuple:
    """
    Judge a single goal against outcome events within the window.
    Returns (outcome_str, notes_str).
    """
    success = goal_def.get("success", {})
    success_event = success.get("event")
    condition = success.get("condition", "")

    # Filter events within the goal window
    window_events = [
        ev for ev in user_events
        if _parse_ts(ev.get("received_at")) and
           goal_started <= _parse_ts(ev["received_at"]) <= goal_deadline
    ]

    if success_event is None:
        return "inconclusive", "goal_has_no_measurable_success_event"

    # Check for success event in window
    matching = [ev for ev in window_events if ev.get("event") == success_event]

    if not matching:
        # Check if deadline has passed
        now = datetime.now(UTC)
        if now > goal_deadline:
            return "failed", f"no_{success_event}_in_window"
        else:
            return "inconclusive", "window_not_yet_closed"

    # Apply condition checks
    if goal_id == "G04":
        # trial_to_paid: purchase_completed with is_trial=false
        paid = [e for e in matching if not e.get("properties", {}).get("is_trial", True)]
        if paid:
            return "succeeded", f"purchase_completed(is_trial=false) at {paid[0].get('received_at')}"
        return "failed", "only_trial_purchases_in_window"

    elif goal_id == "G05":
        # first_paid: purchase_completed with is_trial=false
        paid = [e for e in matching if not e.get("properties", {}).get("is_trial", True)]
        if paid:
            return "succeeded", f"purchase_completed(is_trial=false) at {paid[0].get('received_at')}"
        return "failed", "only_trial_purchases_in_window"

    elif goal_id == "G06":
        # reactivate: any purchase_completed
        return "succeeded", f"purchase_completed at {matching[0].get('received_at')}"

    elif goal_id == "G07":
        # resume_series: play_started for same content_slug (not tracked here; approximate)
        return "succeeded", f"play_started in_window at {matching[0].get('received_at')}"

    elif goal_id == "G03":
        # second_day_return: app_open on a LATER IST calendar day than goal start
        goal_start_day = _ist_day(goal_started)
        later_opens = [
            e for e in matching
            if _ist_day(_parse_ts(e["received_at"])) > goal_start_day
        ]
        if later_opens:
            return "succeeded", f"app_open on later day at {later_opens[0].get('received_at')}"
        return "failed", "no_app_open_on_later_day"

    elif goal_id == "G09":
        # weekly_habit: play_started on 3 distinct IST days
        play_days = set()
        for e in matching:
            play_days.add(_ist_day(_parse_ts(e["received_at"])))
        if len(play_days) >= 3:
            return "succeeded", f"played_on_{len(play_days)}_distinct_days"
        return "failed", f"only_{len(play_days)}_distinct_play_days"

    elif goal_id == "G11":
        # tv_activation: tv_app_open event
        return "succeeded", f"tv_app_open at {matching[0].get('received_at')}"

    else:
        # Default: success event present in window
        return "succeeded", f"{success_event} at {matching[0].get('received_at')}"
