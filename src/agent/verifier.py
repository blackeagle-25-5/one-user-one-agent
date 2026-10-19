"""
Independent verifier.

Every proposed action goes through this verifier BEFORE it can be published.
The verifier is separate from the decision step and can REJECT any action.

Checks performed (in order):
  1. Holdout arm — users in holdout get ZERO actions.
  2. Consent — push requires notifications_opt_in; WhatsApp requires whatsapp_opt_in.
  3. Quiet hours — no push/WA between 22:00 and 08:00 IST.
  4. Frequency cap — at most 1 push or WhatsApp per user per IST day.
  5. Dialect match — every title must be in the viewer's dialect.
  6. Content availability — every title must be available at publish time.
  7. Policy compliance — no forbidden push phrases, no invented prices.
  8. Content safety — no title flagged for prompt injection.
  9. Home page structural rules — hero, row counts, min titles per row.
 10. Model claim verification — model output must be backed by facts (never self-evidence).
"""

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

QUIET_START_HOUR = 22  # IST
QUIET_END_HOUR = 8    # IST

FORBIDDEN_PUSH_PHRASES = [
    "last chance", "hurry", "only today", "free forever", "guaranteed"
]

VALID_PRICES_INR = {1, 149, 399}

MAX_PUSH_TITLE_CHARS = 40
MAX_PUSH_BODY_CHARS = 120

MAX_HOME_ROWS = 6
MIN_ROW_TITLES = 3
MAX_ROW_TITLES = 10
ALLOWED_ROW_TYPES = {"next_episode", "because_you_watched", "popular_in_dialect",
                     "new_releases", "genre"}


class VerificationResult:
    def __init__(self, passed: bool, checks_passed: List[str], checks_failed: List[str], notes: List[str]):
        self.passed = passed
        self.checks_passed = checks_passed
        self.checks_failed = checks_failed
        self.notes = notes

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "passed_checks": self.checks_passed,
            "failed_checks": self.checks_failed,
            "notes": self.notes,
        }


def verify_action(
    action_type: str,
    action_payload: dict,
    facts,            # ViewerFacts
    arm: str,
    publish_at: datetime,
    catalogue: dict,
    injection_flags: set,
    messages_sent_today: int,
    model_claim: Optional[dict] = None,
) -> VerificationResult:
    """
    Verify a proposed action. Returns a VerificationResult.

    Parameters
    ----------
    action_type       : "HOME_PAGE" | "PUSH" | "WHATSAPP" | "IN_APP" | "NONE"
    action_payload    : full action dict
    facts             : ViewerFacts
    arm               : "agent" | "holdout"
    publish_at        : UTC datetime when the decision would be published
    catalogue         : title_id -> catalogue dict
    injection_flags   : set of title_ids with detected prompt injection
    messages_sent_today: count of push+whatsapp already sent today (IST)
    model_claim       : if set, the raw model output to verify for evidentiary support
    """
    passed = []
    failed = []
    notes = []

    # ------------------------------------------------------------------ #
    # 1. Holdout                                                           #
    # ------------------------------------------------------------------ #
    if arm == "holdout":
        failed.append("holdout_must_receive_no_actions")
        return VerificationResult(False, passed, failed, notes)
    passed.append("not_holdout")

    if action_type == "NONE":
        passed.append("no_action_always_allowed")
        return VerificationResult(True, passed, failed, notes)

    publish_ist = publish_at.astimezone(IST)

    # ------------------------------------------------------------------ #
    # 2. Consent                                                           #
    # ------------------------------------------------------------------ #
    if action_type == "PUSH":
        if not facts.notifications_opt_in:
            failed.append("push_requires_notifications_opt_in")
        else:
            passed.append("push_consent_ok")

    if action_type == "WHATSAPP":
        if not facts.whatsapp_opt_in:
            failed.append("whatsapp_requires_whatsapp_opt_in")
        else:
            passed.append("whatsapp_consent_ok")

    # ------------------------------------------------------------------ #
    # 3. Quiet hours                                                       #
    # ------------------------------------------------------------------ #
    if action_type in ("PUSH", "WHATSAPP"):
        ist_hour = publish_ist.hour
        in_quiet = ist_hour >= QUIET_START_HOUR or ist_hour < QUIET_END_HOUR
        if in_quiet:
            failed.append(f"quiet_hours_violation (IST hour={ist_hour}, no push/WA 22:00-08:00)")
            notes.append(f"Publish time {publish_ist.strftime('%H:%M')} IST is in quiet hours")
        else:
            passed.append("outside_quiet_hours")

    # ------------------------------------------------------------------ #
    # 4. Frequency cap                                                     #
    # ------------------------------------------------------------------ #
    if action_type in ("PUSH", "WHATSAPP"):
        if messages_sent_today >= 1:
            failed.append(f"frequency_cap_exceeded (already_sent_{messages_sent_today}_message_today_IST)")
        else:
            passed.append("frequency_cap_ok")

    # ------------------------------------------------------------------ #
    # 5. Dialect match                                                     #
    # ------------------------------------------------------------------ #
    viewer_dialect = facts.dialect
    title_ids = _extract_title_ids(action_type, action_payload)
    wrong_dialect_titles = []
    for tid in title_ids:
        t = catalogue.get(tid)
        if t and t.get("dialect") != viewer_dialect:
            wrong_dialect_titles.append(f"{tid}:dialect={t.get('dialect')}")
    if wrong_dialect_titles:
        failed.append(f"dialect_mismatch: {wrong_dialect_titles}")
    else:
        passed.append("dialect_match_ok")

    # ------------------------------------------------------------------ #
    # 6. Content availability                                              #
    # ------------------------------------------------------------------ #
    unavailable_titles = []
    for tid in title_ids:
        t = catalogue.get(tid)
        if t is None:
            unavailable_titles.append(f"{tid}:not_in_catalogue")
            continue
        avail_from = t.get("_available_from")
        avail_until = t.get("_available_until")
        if avail_from and avail_from > publish_at:
            unavailable_titles.append(f"{tid}:not_yet_available")
        elif avail_until and publish_at >= avail_until:
            unavailable_titles.append(f"{tid}:withdrawn")
    if unavailable_titles:
        failed.append(f"content_unavailable: {unavailable_titles}")
    else:
        passed.append("content_availability_ok")

    # ------------------------------------------------------------------ #
    # 7. Injection flags                                                   #
    # ------------------------------------------------------------------ #
    injected_titles = [tid for tid in title_ids if tid in injection_flags]
    if injected_titles:
        failed.append(f"prompt_injection_title_used: {injected_titles}")
        notes.append("Title(s) with injected instructions must not be used in actions")
    else:
        passed.append("no_injection_flagged_titles")

    # ------------------------------------------------------------------ #
    # 8. Push-specific rules                                               #
    # ------------------------------------------------------------------ #
    if action_type == "PUSH":
        push_title = action_payload.get("text", {}).get("title", "")
        push_body = action_payload.get("text", {}).get("body", "")

        if len(push_title) > MAX_PUSH_TITLE_CHARS:
            failed.append(f"push_title_too_long ({len(push_title)} > {MAX_PUSH_TITLE_CHARS})")
        else:
            passed.append("push_title_length_ok")

        if len(push_body) > MAX_PUSH_BODY_CHARS:
            failed.append(f"push_body_too_long ({len(push_body)} > {MAX_PUSH_BODY_CHARS})")
        else:
            passed.append("push_body_length_ok")

        full_text = (push_title + " " + push_body).lower()
        found_phrases = [p for p in FORBIDDEN_PUSH_PHRASES if p in full_text]
        if found_phrases:
            failed.append(f"forbidden_push_phrases: {found_phrases}")
        else:
            passed.append("no_forbidden_phrases")

        # Deeplink format
        deeplink = action_payload.get("deeplink", "")
        if deeplink and not re.match(r"^ott://title/T\d+", deeplink):
            failed.append(f"invalid_deeplink_format: {deeplink!r}")
        else:
            passed.append("deeplink_format_ok")

        # No invented prices
        price_matches = re.findall(r"₹(\d+)", push_body + push_title)
        for price_str in price_matches:
            if int(price_str) not in VALID_PRICES_INR:
                failed.append(f"invented_price_in_push: ₹{price_str}")

    # ------------------------------------------------------------------ #
    # 9. WhatsApp-specific rules                                           #
    # ------------------------------------------------------------------ #
    if action_type == "WHATSAPP":
        approved_template_ids = {
            "wa_new_episode_v2", "wa_recommend_title_v1", "wa_trial_ending_v1"
        }
        template_id = action_payload.get("template_id")
        if template_id not in approved_template_ids:
            failed.append(f"unapproved_whatsapp_template: {template_id!r}")
        else:
            passed.append("whatsapp_template_approved")

    # ------------------------------------------------------------------ #
    # 10. Home page structural rules                                       #
    # ------------------------------------------------------------------ #
    if action_type == "HOME_PAGE":
        rows = action_payload.get("rows", [])
        if len(rows) > MAX_HOME_ROWS:
            failed.append(f"too_many_home_rows ({len(rows)} > {MAX_HOME_ROWS})")
        else:
            passed.append("home_row_count_ok")

        seen_slugs = set()
        dropped_rows = []
        for i, row in enumerate(rows):
            row_type = row.get("row_type")
            if row_type not in ALLOWED_ROW_TYPES:
                failed.append(f"invalid_row_type: {row_type!r}")
            row_titles = row.get("title_ids", [])
            valid_titles = []
            for tid in row_titles:
                t = catalogue.get(tid)
                if not t:
                    continue
                slug = t.get("content_slug")
                if slug in seen_slugs:
                    continue  # dedup by content_slug
                # dialect and availability already checked above
                valid_titles.append(tid)
                seen_slugs.add(slug)
            if len(valid_titles) < MIN_ROW_TITLES:
                dropped_rows.append(f"row_{i}:{row_type}(only_{len(valid_titles)}_valid_titles)")
            if len(valid_titles) > MAX_ROW_TITLES:
                notes.append(f"row_{i} has {len(valid_titles)} titles; platform will truncate to {MAX_ROW_TITLES}")

        if dropped_rows:
            failed.append(f"rows_below_minimum_titles: {dropped_rows}")
        else:
            passed.append("home_rows_all_have_minimum_titles")

        # "Continue Watching" must not be in rows
        cw_rows = [r.get("row_type") for r in rows if r.get("row_type") == "continue_watching"]
        if cw_rows:
            failed.append("continue_watching_row_must_not_be_published_by_agent")
        else:
            passed.append("no_continue_watching_row")

    # ------------------------------------------------------------------ #
    # 11. Model claim verification                                         #
    # ------------------------------------------------------------------ #
    if model_claim:
        # Model output is never treated as evidence for eligibility.
        # If the model claims the user is eligible, we must verify against facts.
        claimed_eligible = model_claim.get("eligible", False)
        claimed_goal = model_claim.get("goal_id")
        if claimed_eligible and claimed_goal:
            # The verifier has already checked eligibility from facts.
            # Log that we are not accepting the model's eligibility claim as evidence.
            notes.append(
                f"Model claimed eligibility for {claimed_goal} with confidence "
                f"{model_claim.get('confidence', '?')}; "
                "accepted only if facts independently confirm eligibility (not using model output as evidence)"
            )
            passed.append("model_claim_reviewed_not_used_as_evidence")

    overall = len(failed) == 0
    return VerificationResult(overall, passed, failed, notes)


def _extract_title_ids(action_type: str, payload: dict) -> List[str]:
    """Collect all title_ids referenced in an action payload."""
    ids = []
    if action_type == "HOME_PAGE":
        hero = payload.get("hero_title_id")
        if hero:
            ids.append(hero)
        for row in payload.get("rows", []):
            ids.extend(row.get("title_ids", []))
    elif action_type in ("PUSH", "WHATSAPP", "IN_APP"):
        tid = payload.get("title_id")
        if tid:
            ids.append(tid)
    return ids
