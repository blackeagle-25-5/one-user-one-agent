"""
Fact resolver: extracts viewer facts from events + profile snapshot,
applies precedence rules, and records conflicts.

Precedence rules (documented here as the authoritative source):
  dialect   : app_setting (most recent event) > profile.preferred_dialect > campaign_deeplink
  consent   : a later notification_opt_out event overrides the profile snapshot
  subscription: a purchase_completed / subscription_lapsed event beats profile snapshot
               (profile is 1-day stale; event is real-time)
  signup_at : profile snapshot (warehouse is authoritative for sign-up date)

All facts are returned with their source and a received_at timestamp.
"""

import logging
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

# Pattern for dialect in campaign deeplinks: ott://c/<campaign>/<dialect>/title/<title_id>
_DEEPLINK_DIALECT_RE = re.compile(r"ott://c/[^/]+/([^/]+)/title/")

# How old an event can be (received_at vs trigger) before we downgrade its freshness
STALE_EVENT_SECONDS = 6 * 3600  # 6 hours


class ViewerFacts:
    """
    Resolved facts for one viewer, with provenance.
    """
    def __init__(self, user_id: str):
        self.user_id = user_id
        # Core facts (resolved)
        self.dialect: Optional[str] = None
        self.dialect_source: Optional[str] = None
        self.dialect_conflicts: List[dict] = []

        self.subscription_status: Optional[str] = None
        self.subscription_source: Optional[str] = None
        self.plan_ends_at: Optional[datetime] = None

        self.notifications_opt_in: bool = False
        self.whatsapp_opt_in: bool = False
        self.consent_source: str = "none"

        self.signup_at: Optional[datetime] = None
        self.signup_source: str = "none"

        # Watch history
        self.has_played: bool = False
        self.has_completed_episode: bool = False
        self.play_days: set = set()  # IST calendar days with a play_started
        self.play_history: List[dict] = []  # list of {title_id, content_slug, episode_id, received_at}
        self.completed_slugs: set = set()
        self.last_app_open_at: Optional[datetime] = None
        self.app_open_days: set = set()  # IST days

        # Unfinished series: content_slug -> last episode watched
        self.in_progress_slugs: Dict[str, str] = {}

        # All raw fact entries for auditing
        self.evidence_refs: List[str] = []
        self.conflicts: List[dict] = []
        self.assumptions: List[str] = []

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "dialect": self.dialect,
            "dialect_source": self.dialect_source,
            "dialect_conflicts": self.dialect_conflicts,
            "subscription_status": self.subscription_status,
            "subscription_source": self.subscription_source,
            "notifications_opt_in": self.notifications_opt_in,
            "whatsapp_opt_in": self.whatsapp_opt_in,
            "consent_source": self.consent_source,
            "signup_at": self.signup_at.isoformat() if self.signup_at else None,
            "signup_source": self.signup_source,
            "has_played": self.has_played,
            "has_completed_episode": self.has_completed_episode,
            "play_days_count": len(self.play_days),
            "completed_slugs_count": len(self.completed_slugs),
            "in_progress_slugs": list(self.in_progress_slugs.keys()),
            "last_app_open_at": self.last_app_open_at.isoformat() if self.last_app_open_at else None,
            "evidence_refs": self.evidence_refs,
            "conflicts": self.conflicts,
            "assumptions": self.assumptions,
        }


def resolve_facts(user_id: str, profile: Optional[dict],
                  events: List[dict], trigger: dict,
                  catalogue: dict) -> ViewerFacts:
    """
    Build a ViewerFacts object from profile + events + trigger.

    Parameters
    ----------
    user_id  : the viewer
    profile  : profile snapshot row (may be None for brand-new users)
    events   : historical events for this user, sorted by received_at
    trigger  : the app_open / login trigger event
    catalogue: title_id -> catalogue dict (for content-slug lookups)
    """
    facts = ViewerFacts(user_id)
    trigger_received_at = _parse_ts(trigger.get("received_at"))

    # ------------------------------------------------------------------ #
    # 1. Dialect                                                           #
    # ------------------------------------------------------------------ #
    # Collect all dialect sources
    dialect_profile = profile.get("preferred_dialect") if profile else None
    dialect_app = trigger.get("app_dialect")  # most authoritative
    dialect_deeplink = _extract_deeplink_dialect(trigger)

    all_dialects = {}
    if dialect_app:
        all_dialects["app_setting"] = dialect_app
    if dialect_profile:
        all_dialects["profile"] = dialect_profile
    if dialect_deeplink:
        all_dialects["campaign_deeplink"] = dialect_deeplink

    # Precedence: app_setting > profile > campaign_deeplink
    if dialect_app:
        facts.dialect = dialect_app
        facts.dialect_source = "app_setting"
    elif dialect_profile:
        facts.dialect = dialect_profile
        facts.dialect_source = "profile"
    elif dialect_deeplink:
        facts.dialect = dialect_deeplink
        facts.dialect_source = "campaign_deeplink"

    # Record conflicts (any source that differs from the resolved value)
    for src, val in all_dialects.items():
        if src != facts.dialect_source and val != facts.dialect:
            facts.dialect_conflicts.append({
                "source": src,
                "value": val,
                "conflict_source": src,
                "conflict_value": val,
            })
            facts.conflicts.append({
                "fact": "dialect",
                "resolved_to": facts.dialect,
                "resolved_source": facts.dialect_source,
                "conflict_source": src,
                "conflict_value": val,
            })

    if facts.dialect:
        facts.evidence_refs.append(f"trigger:{trigger.get('event_id')}:app_dialect")

    # ------------------------------------------------------------------ #
    # 2. Consent  (profile baseline + event overrides)                    #
    # ------------------------------------------------------------------ #
    if profile:
        facts.notifications_opt_in = profile.get("notifications_opt_in", "").lower() == "true"
        facts.whatsapp_opt_in = profile.get("whatsapp_opt_in", "").lower() == "true"
        facts.consent_source = "profile_snapshot"
        facts.evidence_refs.append(f"profile:{user_id}:notifications_opt_in")

    # Apply notification_opt_out events (override profile, per messaging policy rule 1)
    for ev in events:
        if ev["event"] == "notification_opt_out":
            channel = ev.get("properties", {}).get("channel", "")
            if channel == "push":
                facts.notifications_opt_in = False
                facts.consent_source = "opt_out_event"
                facts.evidence_refs.append(f"event:{ev['event_id']}:notification_opt_out:push")
            elif channel == "whatsapp":
                facts.whatsapp_opt_in = False
                facts.consent_source = "opt_out_event"
                facts.evidence_refs.append(f"event:{ev['event_id']}:notification_opt_out:whatsapp")

    # ------------------------------------------------------------------ #
    # 3. Subscription status  (events override stale profile)             #
    # ------------------------------------------------------------------ #
    if profile:
        facts.subscription_status = profile.get("subscription_status", "never")
        facts.subscription_source = "profile_snapshot"
        if profile.get("plan_ends_at"):
            facts.plan_ends_at = _parse_ts(profile["plan_ends_at"])

    # Real-time event overrides profile (profile is 1-day stale)
    for ev in events:
        event_type = ev["event"]
        ev_at = ev["_received_at"]
        if ev_at is None:
            continue
        if event_type == "purchase_completed":
            is_trial = ev.get("properties", {}).get("is_trial", False)
            if is_trial:
                facts.subscription_status = "trial_active"
            else:
                facts.subscription_status = "active"
            facts.subscription_source = "purchase_event"
            facts.evidence_refs.append(f"event:{ev['event_id']}:purchase_completed")
        elif event_type == "subscription_renewed":
            facts.subscription_status = "active"
            facts.subscription_source = "subscription_renewed_event"
            facts.evidence_refs.append(f"event:{ev['event_id']}:subscription_renewed")
        elif event_type == "subscription_lapsed":
            facts.subscription_status = "lapsed"
            facts.subscription_source = "subscription_lapsed_event"
            facts.evidence_refs.append(f"event:{ev['event_id']}:subscription_lapsed")

    # ------------------------------------------------------------------ #
    # 4. Signup date                                                       #
    # ------------------------------------------------------------------ #
    if profile and profile.get("signup_at"):
        facts.signup_at = _parse_ts(profile["signup_at"])
        facts.signup_source = "profile_snapshot"
        facts.evidence_refs.append(f"profile:{user_id}:signup_at")
    else:
        # Brand-new user: derive from signup_completed event
        for ev in events:
            if ev["event"] == "signup_completed":
                facts.signup_at = ev["_received_at"]
                facts.signup_source = "signup_completed_event"
                facts.evidence_refs.append(f"event:{ev['event_id']}:signup_completed")
                facts.assumptions.append("signup_at derived from signup_completed event (no profile row yet)")
                break

    # ------------------------------------------------------------------ #
    # 5. Watch history                                                     #
    # ------------------------------------------------------------------ #
    for ev in events:
        ev_at = ev["_received_at"]
        if ev_at is None:
            continue
        ev_ist_day = ev_at.astimezone(IST).date()
        event_type = ev["event"]

        if event_type == "app_open":
            facts.last_app_open_at = ev_at
            facts.app_open_days.add(ev_ist_day)

        elif event_type == "play_started":
            tid = ev.get("properties", {}).get("title_id")
            ep_id = ev.get("properties", {}).get("episode_id")
            facts.has_played = True
            facts.play_days.add(ev_ist_day)
            cat_entry = catalogue.get(tid) if tid else None
            slug = cat_entry["content_slug"] if cat_entry else None
            facts.play_history.append({
                "title_id": tid,
                "content_slug": slug,
                "episode_id": ep_id,
                "received_at": ev_at.isoformat(),
            })
            if slug:
                facts.in_progress_slugs[slug] = ep_id

        elif event_type == "episode_completed":
            tid = ev.get("properties", {}).get("title_id")
            ep_id = ev.get("properties", {}).get("episode_id")
            facts.has_completed_episode = True
            cat_entry = catalogue.get(tid) if tid else None
            slug = cat_entry["content_slug"] if cat_entry else None
            if slug:
                facts.completed_slugs.add(slug)
                facts.in_progress_slugs.pop(slug, None)

    # ------------------------------------------------------------------ #
    # 6. Freshness note for stale trigger                                  #
    # ------------------------------------------------------------------ #
    sent_at = _parse_ts(trigger.get("sent_at"))
    received_at = _parse_ts(trigger.get("received_at"))
    if sent_at and received_at:
        lag = (received_at - sent_at).total_seconds()
        if lag > STALE_EVENT_SECONDS:
            facts.assumptions.append(
                f"Trigger may be offline-buffered: sent_at vs received_at lag = {lag:.0f}s"
            )

    return facts


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _extract_deeplink_dialect(trigger: dict) -> Optional[str]:
    """Extract dialect from an ott://c/<campaign>/<dialect>/title/<id> deeplink."""
    deeplink = trigger.get("properties", {}).get("deeplink", "")
    if not deeplink:
        return None
    m = _DEEPLINK_DIALECT_RE.search(deeplink)
    return m.group(1) if m else None
