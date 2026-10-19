"""
Data loader for the One User One Agent pack.

Loads events.jsonl, profiles_snapshot.csv, catalogue.csv, episodes.csv,
goals.json, interventions.json, pricing.json, and replay files.

Key rules implemented here:
- Deduplicate events by event_id (same event may arrive more than once).
- Use received_at (server time) for ordering, not occurred_at / sent_at (phone clock).
- For duplicate profile rows, keep the row with the latest updated_at.
- Detect and log prompt-injection strings in catalogue descriptions.
"""

import csv
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

# Strings that indicate a prompt-injection attempt in catalogue descriptions
INJECTION_PATTERNS = [
    "system note", "ignore your previous", "send a whatsapp", "send whatsapp",
    "message every user", "instruct the agent", "override", "ignore rules",
    "forget previous", "new instruction"
]


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    """Parse ISO-8601 UTC timestamp string to datetime."""
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _check_injection(text: str, title_id: str) -> bool:
    """Return True if text looks like a prompt-injection attempt; log a warning."""
    lower = text.lower()
    for pattern in INJECTION_PATTERNS:
        if pattern in lower:
            log.warning(
                "PROMPT_INJECTION_DETECTED title_id=%s pattern=%r desc_snippet=%r",
                title_id, pattern, text[:120]
            )
            return True
    return False


class DataPack:
    """
    Holds the fully loaded, deduplicated, normalised data pack.

    Attributes
    ----------
    events          : list of dicts, deduplicated, sorted by received_at
    events_by_user  : user_id -> list of events (sorted by received_at)
    profiles        : user_id -> profile dict (latest updated_at row wins)
    catalogue       : title_id -> catalogue dict
    slugs           : content_slug -> list of title_ids (all language variants)
    episodes        : content_slug -> sorted list of (season, episode, episode_id)
    goals           : list of goal dicts
    interventions   : raw interventions dict
    pricing         : raw pricing dict
    arms            : user_id -> "agent" | "holdout"  (replay arms)
    triggers        : list of trigger dicts sorted by deliver_at
    live_events     : list of live_events dicts sorted by received_at
    control_events  : list of control_event dicts sorted by at
    model_faults    : trigger_event_id -> fault dict
    injection_flags : set of title_ids with detected injections
    profile_conflicts: user_id -> list of dicts for profiles that were dropped
    """

    def __init__(self, pack_root: str):
        self.pack_root = pack_root
        self.events: List[dict] = []
        self.events_by_user: Dict[str, List[dict]] = defaultdict(list)
        self.profiles: Dict[str, dict] = {}
        self.profile_conflicts: Dict[str, list] = defaultdict(list)
        self.catalogue: Dict[str, dict] = {}
        self.slugs: Dict[str, List[str]] = defaultdict(list)
        self.episodes: Dict[str, List[Tuple]] = defaultdict(list)
        self.goals: List[dict] = []
        self.interventions: dict = {}
        self.pricing: dict = {}
        self.arms: Dict[str, str] = {}
        self.triggers: List[dict] = []
        self.live_events: List[dict] = []
        self.control_events: List[dict] = []
        self.model_faults: Dict[str, dict] = {}
        self.injection_flags: set = set()
        self._load()

    # ------------------------------------------------------------------
    # Internal loaders
    # ------------------------------------------------------------------

    def _path(self, *parts) -> str:
        return os.path.join(self.pack_root, *parts)

    def _load(self):
        self._load_events()
        self._load_profiles()
        self._load_catalogue()
        self._load_episodes()
        self._load_config()
        self._load_replay()
        log.info(
            "DataPack loaded: %d events, %d profiles, %d titles, %d goals, "
            "%d triggers, %d arms",
            len(self.events), len(self.profiles), len(self.catalogue),
            len(self.goals), len(self.triggers), len(self.arms)
        )

    def _load_events(self):
        path = self._path("data", "events.jsonl")
        seen_ids: set = set()
        raw: List[dict] = []
        dup_count = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ev = json.loads(line)
                eid = ev.get("event_id")
                if eid in seen_ids:
                    dup_count += 1
                    continue
                seen_ids.add(eid)
                ev["_received_at"] = _parse_ts(ev.get("received_at"))
                raw.append(ev)
        raw.sort(key=lambda e: e["_received_at"] or datetime.min.replace(tzinfo=UTC))
        self.events = raw
        for ev in raw:
            self.events_by_user[ev["user_id"]].append(ev)
        log.info("Events: %d loaded, %d duplicates dropped", len(raw), dup_count)

    def _load_profiles(self):
        path = self._path("data", "profiles_snapshot.csv")
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                uid = row["user_id"]
                # Rule: keep the row with the latest updated_at (freshest warehouse export row)
                existing = self.profiles.get(uid)
                if existing is None:
                    self.profiles[uid] = row
                elif row.get("updated_at", "") > existing.get("updated_at", ""):
                    self.profile_conflicts[uid].append(existing)
                    self.profiles[uid] = row
                else:
                    self.profile_conflicts[uid].append(row)
        dup_users = len(self.profile_conflicts)
        log.info("Profiles: %d users, %d with duplicate rows (older row dropped)", len(self.profiles), dup_users)

    def _load_catalogue(self):
        path = self._path("data", "catalogue.csv")
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                tid = row["title_id"]
                desc = row.get("description", "")
                if _check_injection(desc, tid):
                    self.injection_flags.add(tid)
                    row["_injection_detected"] = True
                else:
                    row["_injection_detected"] = False
                row["_available_from"] = _parse_ts(row.get("available_from"))
                row["_available_until"] = _parse_ts(row.get("available_until")) if row.get("available_until") else None
                self.catalogue[tid] = row
                self.slugs[row["content_slug"]].append(tid)
        log.info("Catalogue: %d titles, %d content slugs, %d injection flags",
                 len(self.catalogue), len(self.slugs), len(self.injection_flags))

    def _load_episodes(self):
        path = self._path("data", "episodes.csv")
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                self.episodes[row["content_slug"]].append(
                    (int(row["season"]), int(row["episode"]), row["episode_id"])
                )
        for slug in self.episodes:
            self.episodes[slug].sort()

    def _load_config(self):
        with open(self._path("config", "goals.json"), encoding="utf-8") as f:
            data = json.load(f)
            self.goals = data["goals"]
            self.goals_version = data.get("version", "unknown")
        with open(self._path("config", "interventions.json"), encoding="utf-8") as f:
            self.interventions = json.load(f)
        with open(self._path("config", "pricing.json"), encoding="utf-8") as f:
            self.pricing = json.load(f)

    def _load_replay(self):
        # Triggers
        path = self._path("replay", "triggers.jsonl")
        if os.path.exists(path):
            raw = []
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        t = json.loads(line)
                        t["_deliver_at"] = _parse_ts(t.get("deliver_at"))
                        raw.append(t)
            raw.sort(key=lambda t: t["_deliver_at"] or datetime.min.replace(tzinfo=UTC))
            self.triggers = raw

        # Live events
        path = self._path("replay", "live_events.jsonl")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        ev = json.loads(line)
                        ev["_received_at"] = _parse_ts(ev.get("received_at"))
                        self.live_events.append(ev)
            self.live_events.sort(key=lambda e: e["_received_at"] or datetime.min.replace(tzinfo=UTC))

        # Control events
        path = self._path("replay", "control_events.jsonl")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        ev = json.loads(line)
                        ev["_at"] = _parse_ts(ev.get("at"))
                        self.control_events.append(ev)
            self.control_events.sort(key=lambda e: e["_at"] or datetime.min.replace(tzinfo=UTC))

        # Model faults
        path = self._path("replay", "model_faults.jsonl")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        fault = json.loads(line)
                        self.model_faults[fault["trigger_event_id"]] = fault

        # Arms
        path = self._path("replay", "arms.csv")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    self.arms[row["user_id"]] = row["arm"]

    # ------------------------------------------------------------------
    # Helper queries
    # ------------------------------------------------------------------

    def is_available(self, title_id: str, at: datetime) -> bool:
        """Check if a title is available at a given UTC datetime."""
        t = self.catalogue.get(title_id)
        if t is None:
            return False
        avail_from = t["_available_from"]
        avail_until = t["_available_until"]
        if avail_from is None:
            return False
        if avail_from > at:
            return False
        if avail_until is not None and at >= avail_until:
            return False
        return True

    def titles_for_dialect(self, dialect: str, at: datetime, exclude_slugs: set = None) -> List[str]:
        """Return title_ids matching dialect, available at `at`, deduplicated by content_slug."""
        seen_slugs = set(exclude_slugs or [])
        result = []
        for tid, t in self.catalogue.items():
            if t["dialect"] != dialect:
                continue
            if not self.is_available(tid, at):
                continue
            slug = t["content_slug"]
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            result.append(tid)
        return result

    def user_events(self, user_id: str) -> List[dict]:
        """All historical events for a user, sorted by received_at."""
        return self.events_by_user.get(user_id, [])

    def withdraw_title(self, title_id: str, at: datetime):
        """Apply a title_withdrawn control event: update the availability window."""
        t = self.catalogue.get(title_id)
        if t is None:
            return
        existing_until = t["_available_until"]
        if existing_until is None or at < existing_until:
            t["_available_until"] = at
            log.warning("Title %s withdrawn at %s", title_id, at.isoformat())

    def apply_goal_rule_correction(self, correction: dict):
        """Apply a goal_rule_corrected control event; bump version for traceability."""
        goal_id = correction.get("goal_id")
        new_rule = correction.get("new_rule")
        if not goal_id or not new_rule:
            log.warning("Malformed goal_rule_corrected event: %s", correction)
            return
        for g in self.goals:
            if g["goal_id"] == goal_id:
                g["_previous_rule"] = g.get("success", {})
                g["success"] = new_rule
                self.goals_version = correction.get("rule_version", self.goals_version + "-corrected")
                log.warning("Goal rule corrected: goal_id=%s new_version=%s", goal_id, self.goals_version)
                return
        log.warning("Goal rule correction: goal_id %s not found", goal_id)
