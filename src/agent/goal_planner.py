"""
Goal planner: checks eligibility for each goal and selects the single best one.

Rules:
- Only one active goal per viewer at a time.
- If a viewer already has an open goal whose deadline has not passed, keep it
  (unless the situation has materially changed, e.g. a purchase).
- Skip goals with no measurable success event (G10 meaningful_engagement_7d).
- Skip goals with ambiguous rules; document interpretation as an assumption.
- Never assert eligibility without event or profile evidence.

Goal eligibility logic is deterministic (no model involvement).
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

from src.agent.fact_resolver import ViewerFacts

log = logging.getLogger(__name__)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

# Goals that cannot be measured from the available event stream.
# G10 success event is null — skip it and document why.
UNMEASURABLE_GOALS = {
    "G10": "success event is null in the data; cannot judge outcome",
}

# Goals whose eligibility rules are underspecified — document interpretation.
AMBIGUOUS_GOALS = {
    "G07": (
        "Eligibility: 'started a show or micro-drama, has unwatched episodes left, "
        "and has not played it for 3 or more days.' Interpretation: viewer has "
        "at least one in_progress_slug (started but not completed) and the last "
        "play_started for that slug was ≥3 days before the trigger."
    ),
    "G11": (
        "Success event: 'tv_app_open'. This event does not appear in the historical "
        "events stream. Assumption: the platform will emit it during the simulator "
        "run; we include the goal but mark it lower priority."
    ),
}


def _parse_ts(s) -> Optional[datetime]:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _days_since(dt: Optional[datetime], now: datetime) -> Optional[float]:
    if dt is None:
        return None
    return (now - dt).total_seconds() / 86400


def select_goal(
    facts: ViewerFacts,
    goals: List[dict],
    trigger_received_at: datetime,
    current_goal_id: Optional[str] = None,
    current_goal_status: Optional[str] = None,
    current_goal_deadline: Optional[str] = None,
) -> Tuple[Optional[dict], str, List[str]]:
    """
    Select the single best goal for this viewer.

    Returns (goal_dict, reason, assumptions_list).
    If no goal is eligible, returns (None, reason, assumptions).

    The goal_dict is the raw goals.json entry, or None for abstention.
    """
    assumptions = list(facts.assumptions)
    now = trigger_received_at

    # ------------------------------------------------------------------ #
    # Keep existing open goal if still valid                               #
    # ------------------------------------------------------------------ #
    if current_goal_id and current_goal_status == "open":
        deadline = _parse_ts(current_goal_deadline)
        if deadline and deadline > now:
            # Check if situation has materially changed (e.g. purchased → not a trial user anymore)
            current_goal = next((g for g in goals if g["goal_id"] == current_goal_id), None)
            if current_goal:
                still_eligible, _ = _check_eligibility(current_goal, facts, now)
                if still_eligible:
                    return current_goal, "keeping_existing_open_goal", assumptions
                else:
                    log.info("Goal %s no longer eligible for %s — re-planning", current_goal_id, facts.user_id)

    # ------------------------------------------------------------------ #
    # Priority order: most impactful / most specific wins                 #
    # Lower index = higher priority                                        #
    # ------------------------------------------------------------------ #
    PRIORITY = ["G03", "G01", "G04", "G06", "G05", "G02", "G07", "G08", "G09", "G12", "G11", "G10"]

    candidates = []
    for goal_id in PRIORITY:
        goal = next((g for g in goals if g["goal_id"] == goal_id), None)
        if goal is None:
            continue

        # Skip unmeasurable
        if goal_id in UNMEASURABLE_GOALS:
            assumptions.append(
                f"Goal {goal_id} ({goal.get('name')}) skipped: {UNMEASURABLE_GOALS[goal_id]}"
            )
            continue

        # Document ambiguous goals
        if goal_id in AMBIGUOUS_GOALS:
            assumptions.append(f"Goal {goal_id} rule interpretation: {AMBIGUOUS_GOALS[goal_id]}")

        eligible, reason = _check_eligibility(goal, facts, now)
        if eligible:
            candidates.append((goal, reason))

    if not candidates:
        return None, "no_eligible_goal_for_this_viewer", assumptions

    chosen_goal, reason = candidates[0]
    log.debug("Selected goal %s for user %s: %s", chosen_goal["goal_id"], facts.user_id, reason)
    return chosen_goal, reason, assumptions


def _check_eligibility(goal: dict, facts: ViewerFacts, now: datetime) -> Tuple[bool, str]:
    """
    Returns (is_eligible, reason_string).
    All eligibility decisions cite the facts they are based on.
    """
    g_id = goal["goal_id"]
    elig = goal.get("eligibility", {})
    status = facts.subscription_status or "never"

    # --- G01: first_play_24h ---
    if g_id == "G01":
        if facts.signup_at is None:
            return False, "no_signup_date_evidence"
        days_since_signup = _days_since(facts.signup_at, now)
        if days_since_signup is None or days_since_signup > 7:
            return False, f"signed_up_more_than_7_days_ago ({days_since_signup:.1f}d)"
        if facts.has_played:
            return False, "has_already_played"
        return True, f"new_user_({days_since_signup:.1f}d_ago)_never_played"

    # --- G02: first_full_episode_3d ---
    elif g_id == "G02":
        if facts.has_completed_episode:
            return False, "already_completed_an_episode"
        return True, "has_never_completed_an_episode"

    # --- G03: second_day_return_2d ---
    elif g_id == "G03":
        if facts.signup_at is None:
            return False, "no_signup_date_evidence"
        days_since_signup = _days_since(facts.signup_at, now)
        if days_since_signup is None or days_since_signup > 2:
            return False, f"signed_up_more_than_2_days_ago ({days_since_signup:.1f}d)"
        return True, f"new_user_({days_since_signup:.1f}d_ago)"

    # --- G04: trial_to_paid_7d ---
    elif g_id == "G04":
        if status == "trial_active":
            return True, "subscription_status=trial_active"
        return False, f"subscription_status={status}_not_trial"

    # --- G05: first_paid_7d ---
    elif g_id == "G05":
        if status == "never":
            return True, "subscription_status=never"
        return False, f"subscription_status={status}"

    # --- G06: reactivate_lapsed_14d ---
    elif g_id == "G06":
        if status == "lapsed":
            return True, "subscription_status=lapsed"
        return False, f"subscription_status={status}"

    # --- G07: resume_series_3d ---
    elif g_id == "G07":
        if not facts.in_progress_slugs:
            return False, "no_in_progress_series"
        # Check if last play was >= 3 days ago
        last_play_at = None
        for ph in reversed(facts.play_history):
            slug = ph.get("content_slug")
            if slug and slug in facts.in_progress_slugs:
                last_play_at = _parse_ts(ph.get("received_at"))
                break
        if last_play_at is None:
            return False, "cannot_determine_last_play_date"
        days_idle = _days_since(last_play_at, now)
        if days_idle is None or days_idle < 3:
            return False, f"last_played_recently ({days_idle:.1f}d_ago)"
        return True, f"has_unfinished_series_idle_{days_idle:.1f}d"

    # --- G08: try_new_release_7d ---
    elif g_id == "G08":
        if status != "active":
            return False, f"not_active_subscriber (status={status})"
        return True, "active_subscriber"

    # --- G09: weekly_habit_14d ---
    elif g_id == "G09":
        if status != "active":
            return False, f"not_active_subscriber (status={status})"
        # Active subscriber with plays on at most 1 day in the last 14 days
        cutoff = now - timedelta(days=14)
        recent_play_days = sum(
            1 for ph in facts.play_history
            if _parse_ts(ph.get("received_at")) and _parse_ts(ph.get("received_at")) > cutoff
        )
        if recent_play_days > 1:
            return False, f"played_on_{recent_play_days}_days_in_last_14d"
        return True, f"low_activity_subscriber ({recent_play_days}_play_days_in_14d)"

    # --- G10: unmeasurable — never selected ---
    elif g_id == "G10":
        return False, "unmeasurable_success_event"

    # --- G11: tv_activation_14d ---
    elif g_id == "G11":
        if status != "active":
            return False, f"not_active_subscriber (status={status})"
        return True, "active_subscriber_no_tv_data"

    # --- G12: return_after_absence_7d ---
    elif g_id == "G12":
        if facts.last_app_open_at is None:
            return False, "no_app_open_history"
        days_absent = _days_since(facts.last_app_open_at, now)
        if days_absent is None or days_absent < 14:
            return False, f"last_opened_{days_absent:.1f}d_ago_(need_14d)"
        return True, f"absent_for_{days_absent:.1f}d"

    return False, f"unknown_goal_id:{g_id}"


def goal_deadline(goal: dict, started_at: datetime) -> datetime:
    """Compute the UTC deadline for a goal from its window_hours."""
    window_hours = goal.get("success", {}).get("window_hours", 72)
    return started_at + timedelta(hours=window_hours)


def goal_success_signal(goal: dict) -> str:
    """Return a string description of the success signal for auditing."""
    s = goal.get("success", {})
    event = s.get("event") or "none"
    condition = s.get("condition", "")
    return f"{event}:{condition}"
