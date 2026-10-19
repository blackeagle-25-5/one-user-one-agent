"""
Intervention planner: decides what action to take given the goal and viewer facts.

Priority:
  1. HOME_PAGE  — shown immediately on app open, no consent required.
  2. PUSH       — if consent + not quiet hours + under frequency cap.
  3. WHATSAPP   — if consent + not quiet hours + under frequency cap.
  4. IN_APP     — shown at next app open, no consent required.
  5. NONE       — with a recorded reason.

The planner uses the mock model to suggest titles, then filters for
dialect and availability. The verifier later independently checks
everything.
"""

import logging
import random
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

from src.agent.fact_resolver import ViewerFacts
from src.model.mock_provider import MockModelProvider, ModelTimeout, ModelProviderError, UnsupportedClaimError

log = logging.getLogger(__name__)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

QUIET_START = 22
QUIET_END = 8

WHATSAPP_TEMPLATES = {
    "G01": "wa_recommend_title_v1",
    "G02": "wa_recommend_title_v1",
    "G03": "wa_recommend_title_v1",
    "G04": "wa_trial_ending_v1",
    "G05": "wa_recommend_title_v1",
    "G06": "wa_recommend_title_v1",
    "G07": "wa_new_episode_v2",
    "G08": "wa_recommend_title_v1",
    "G09": "wa_recommend_title_v1",
    "G12": "wa_recommend_title_v1",
}


class InterventionPlan:
    def __init__(self):
        self.action_type: str = "NONE"
        self.payload: dict = {}
        self.title_ids: List[str] = []
        self.hero_title_id: Optional[str] = None
        self.rows: List[dict] = []
        self.confidence: float = 0.0
        self.confidence_reason: str = ""
        self.no_action_reason: Optional[str] = None
        self.fallback_used: bool = False
        self.model_calls: int = 0
        self.model_claim: Optional[dict] = None


def plan_intervention(
    facts: ViewerFacts,
    goal: dict,
    publish_at: datetime,
    data_pack,           # DataPack object
    model: MockModelProvider,
    messages_sent_today: int,
    tool_calls_used: int = 0,
    max_tool_calls: int = 20,
) -> InterventionPlan:
    """
    Build a proposed action. Returns InterventionPlan.
    All content must be in the viewer's dialect and available at publish_at.
    """
    plan = InterventionPlan()
    goal_id = goal.get("goal_id", "")
    dialect = facts.dialect
    publish_ist = publish_at.astimezone(IST)
    ist_hour = publish_ist.hour
    in_quiet = ist_hour >= QUIET_START or ist_hour < QUIET_END

    # ------------------------------------------------------------------ #
    # Get available titles in viewer's dialect                            #
    # ------------------------------------------------------------------ #
    if not dialect:
        plan.action_type = "NONE"
        plan.no_action_reason = "no_dialect_resolved"
        return plan

    available_titles = data_pack.titles_for_dialect(dialect, publish_at)
    if not available_titles:
        plan.action_type = "NONE"
        plan.no_action_reason = f"no_available_titles_in_dialect_{dialect}"
        return plan

    # ------------------------------------------------------------------ #
    # Ask model for suggestions (with fault tolerance)                    #
    # ------------------------------------------------------------------ #
    catalogue_sample = [data_pack.catalogue[tid] for tid in available_titles[:50]]
    suggested_titles = []

    try:
        suggestion = model.suggest_content(
            user_facts=facts.as_dict(),
            goal=goal,
            catalogue_sample=catalogue_sample,
            dialect=dialect,
        )
        plan.model_calls = model.calls_used
        raw_ids = suggestion.get("title_ids", [])
        # Filter: must be in our dialect and available (safety re-check)
        suggested_titles = [
            tid for tid in raw_ids
            if tid in data_pack.catalogue
            and data_pack.catalogue[tid].get("dialect") == dialect
            and data_pack.is_available(tid, publish_at)
        ]
        plan.confidence = suggestion.get("confidence", 0.4)
        plan.confidence_reason = suggestion.get("reason", "mock_provider")

    except UnsupportedClaimError as e:
        # Model made a claim without evidence — do NOT use it; fallback to rules
        log.warning("Unsupported model claim for user %s: %s", facts.user_id, e.claim)
        plan.model_claim = e.claim
        plan.fallback_used = True
        plan.confidence_reason = "fallback_after_unsupported_model_claim"
        # Use rule-based title selection
        suggested_titles = available_titles[:8]
        plan.confidence = 0.3

    except (ModelTimeout, ModelProviderError) as e:
        log.warning("Model failure for user %s: %s — using rule-based fallback", facts.user_id, e)
        plan.fallback_used = True
        plan.confidence_reason = f"fallback_after_model_error:{type(e).__name__}"
        suggested_titles = available_titles[:8]
        plan.confidence = 0.25

    if not suggested_titles:
        suggested_titles = available_titles[:8]
        plan.confidence_reason += ":fell_back_to_all_available"

    # ------------------------------------------------------------------ #
    # Choose action type                                                   #
    # ------------------------------------------------------------------ #
    # HOME_PAGE: best for immediate engagement at app open
    # Always try HOME_PAGE first (no consent required, immediate effect)
    can_push = (
        facts.notifications_opt_in
        and not in_quiet
        and messages_sent_today < 1
    )
    can_whatsapp = (
        facts.whatsapp_opt_in
        and not in_quiet
        and messages_sent_today < 1
        and goal_id in WHATSAPP_TEMPLATES
    )

    # Prefer HOME_PAGE for most goals (it's immediate and doesn't require consent)
    # Prefer PUSH/WA for re-engagement goals where user is not currently in app
    if goal_id in ("G06", "G12"):
        # Re-engagement: push is more impactful if available
        if can_push and suggested_titles:
            return _build_push_plan(plan, facts, goal, suggested_titles, data_pack, model, publish_at, dialect)
        elif can_whatsapp and suggested_titles:
            return _build_whatsapp_plan(plan, facts, goal, suggested_titles, data_pack, publish_at, dialect)
        elif suggested_titles:
            return _build_homepage_plan(plan, facts, goal, suggested_titles, publish_at, dialect)
    else:
        # Default: HOME_PAGE first
        if suggested_titles:
            return _build_homepage_plan(plan, facts, goal, suggested_titles, publish_at, dialect)
        elif can_push:
            return _build_push_plan(plan, facts, goal, suggested_titles, data_pack, model, publish_at, dialect)

    plan.action_type = "NONE"
    plan.no_action_reason = "no_suitable_action_available"
    return plan


def _build_homepage_plan(plan: InterventionPlan, facts: ViewerFacts,
                         goal: dict, title_ids: List[str],
                         publish_at: datetime, dialect: str) -> InterventionPlan:
    """Build a HOME_PAGE action plan."""
    plan.action_type = "HOME_PAGE"
    goal_id = goal.get("goal_id", "")

    # Hero: pick first title
    hero = title_ids[0] if title_ids else None

    # Build rows based on goal
    rows = []
    seen_slugs = set()
    if hero:
        seen_slugs.add(_PLACEHOLDER_SLUG)  # track hero slug later

    # Row 1: goal-specific row
    row1_type = _goal_row_type(goal_id, facts)
    row1_titles = _pick_row_titles(title_ids, 6, seen_slugs)
    if len(row1_titles) >= 3:
        rows.append({"row_type": row1_type, "title_ids": row1_titles})

    # Row 2: popular_in_dialect
    row2_titles = _pick_row_titles(title_ids, 5, seen_slugs)
    if len(row2_titles) >= 3:
        rows.append({"row_type": "popular_in_dialect", "title_ids": row2_titles})

    plan.hero_title_id = hero
    plan.rows = rows
    plan.payload = {
        "type": "home_page",
        "dialect": dialect,
        "hero_title_id": hero,
        "rows": rows,
    }
    plan.title_ids = title_ids
    plan.confidence = max(plan.confidence, 0.55)
    if not plan.confidence_reason:
        plan.confidence_reason = f"homepage_for_goal_{goal_id}"
    return plan


def _build_push_plan(plan: InterventionPlan, facts: ViewerFacts,
                     goal: dict, title_ids: List[str], data_pack,
                     model: MockModelProvider, publish_at: datetime, dialect: str) -> InterventionPlan:
    plan.action_type = "PUSH"
    title_id = title_ids[0] if title_ids else None
    title = data_pack.catalogue.get(title_id, {}) if title_id else {}

    try:
        copy = model.suggest_push_copy(title, goal)
        push_title = copy.get("push_title", "Watch now")[:40]
        push_body = copy.get("push_body", "Great content awaits.")[:120]
        plan.confidence = max(plan.confidence, copy.get("confidence", 0.4))
        plan.confidence_reason = copy.get("reason", "mock_push_copy")
    except (ModelTimeout, ModelProviderError):
        push_title = f"Watch {title.get('title', 'a great show')}"[:40]
        push_body = "Great regional content is waiting for you."[:120]
        plan.fallback_used = True
        plan.confidence_reason += ":push_copy_fallback"

    plan.payload = {
        "type": "push",
        "dialect": dialect,
        "title_id": title_id,
        "text": {"title": push_title, "body": push_body},
        "deeplink": f"ott://title/{title_id}" if title_id else "",
        "send_at": publish_at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    plan.title_ids = [title_id] if title_id else []
    return plan


def _build_whatsapp_plan(plan: InterventionPlan, facts: ViewerFacts,
                         goal: dict, title_ids: List[str], data_pack,
                         publish_at: datetime, dialect: str) -> InterventionPlan:
    plan.action_type = "WHATSAPP"
    goal_id = goal.get("goal_id", "")
    title_id = title_ids[0] if title_ids else None
    title = data_pack.catalogue.get(title_id, {}) if title_id else {}
    template_id = WHATSAPP_TEMPLATES.get(goal_id, "wa_recommend_title_v1")

    variables = {}
    if template_id == "wa_new_episode_v2":
        variables = {
            "title_name": title.get("title", ""),
            "episode_number": "1",
        }
    elif template_id == "wa_recommend_title_v1":
        dialect_names = {"d1": "Bhojpuri", "d2": "Rajasthani", "d3": "Haryanvi",
                         "d4": "Maithili", "d5": "Chhattisgarhi", "d6": "Avadhi"}
        variables = {
            "title_name": title.get("title", ""),
            "dialect_name": dialect_names.get(dialect, dialect),
        }
    elif template_id == "wa_trial_ending_v1":
        variables = {"plan_price": "\u20b9149"}

    plan.payload = {
        "type": "whatsapp",
        "dialect": dialect,
        "template_id": template_id,
        "variables": variables,
        "title_id": title_id,
        "send_at": publish_at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    plan.title_ids = [title_id] if title_id else []
    plan.confidence = max(plan.confidence, 0.45)
    plan.confidence_reason = f"whatsapp_template_{template_id}"
    return plan


def _goal_row_type(goal_id: str, facts: ViewerFacts) -> str:
    mapping = {
        "G01": "popular_in_dialect",
        "G02": "popular_in_dialect",
        "G03": "popular_in_dialect",
        "G04": "popular_in_dialect",
        "G05": "popular_in_dialect",
        "G06": "popular_in_dialect",
        "G07": "next_episode",
        "G08": "new_releases",
        "G09": "popular_in_dialect",
        "G11": "new_releases",
        "G12": "popular_in_dialect",
    }
    return mapping.get(goal_id, "popular_in_dialect")


def _pick_row_titles(all_titles: List[str], n: int, seen_slugs: set) -> List[str]:
    result = []
    for tid in all_titles:
        if len(result) >= n:
            break
        result.append(tid)
    return result


_PLACEHOLDER_SLUG = "__hero__"
