"""
SQLite-backed per-viewer state store.

Each viewer has one row per agent decision, plus persisted facts (with provenance),
active goal, and outcome judgements. The store is idempotent: inserting a decision
with the same decision_id twice is a no-op.

Schema
------
viewers     : user_id, arm, current_goal_id, goal_started_at, goal_deadline,
              goal_success_signal, goal_status, last_action_type, last_action_at,
              last_action_day_ist, updated_at
decisions   : decision_id, user_id, trigger_event_id, decided_at, goal_id,
              action_type, dialect_used, status, payload_json
facts       : user_id, fact_key, fact_value, source, received_at, freshness_note
outcomes    : decision_id, user_id, goal_id, judged_at, outcome, notes
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class StateStore:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        c = self.conn
        c.executescript("""
            CREATE TABLE IF NOT EXISTS viewers (
                user_id TEXT PRIMARY KEY,
                arm TEXT,
                current_goal_id TEXT,
                goal_started_at TEXT,
                goal_deadline TEXT,
                goal_success_signal TEXT,
                goal_status TEXT DEFAULT 'open',
                last_action_type TEXT,
                last_action_at TEXT,
                last_action_day_ist TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS decisions (
                decision_id TEXT PRIMARY KEY,
                user_id TEXT,
                trigger_event_id TEXT,
                decided_at TEXT,
                goal_id TEXT,
                action_type TEXT,
                dialect_used TEXT,
                status TEXT,
                payload_json TEXT
            );
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                fact_key TEXT,
                fact_value TEXT,
                source TEXT,
                received_at TEXT,
                freshness_note TEXT,
                UNIQUE(user_id, fact_key, source)
            );
            CREATE TABLE IF NOT EXISTS outcomes (
                decision_id TEXT,
                user_id TEXT,
                goal_id TEXT,
                judged_at TEXT,
                outcome TEXT,
                notes TEXT,
                PRIMARY KEY (decision_id)
            );
        """)
        c.commit()

    # ------------------------------------------------------------------
    # Viewer record
    # ------------------------------------------------------------------

    def get_viewer(self, user_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM viewers WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None

    def upsert_viewer(self, user_id: str, arm: str, **kwargs):
        existing = self.get_viewer(user_id)
        if existing is None:
            self.conn.execute(
                "INSERT INTO viewers (user_id, arm, updated_at) VALUES (?, ?, ?)",
                (user_id, arm, _now_utc())
            )
        # Apply updates
        kwargs["updated_at"] = _now_utc()
        if arm:
            kwargs["arm"] = arm
        for key, val in kwargs.items():
            self.conn.execute(
                f"UPDATE viewers SET {key} = ? WHERE user_id = ?", (val, user_id)
            )
        self.conn.commit()

    def set_active_goal(self, user_id: str, goal_id: str, started_at: str,
                        deadline: str, success_signal: str):
        self.conn.execute("""
            UPDATE viewers SET current_goal_id=?, goal_started_at=?, goal_deadline=?,
            goal_success_signal=?, goal_status='open', updated_at=?
            WHERE user_id=?
        """, (goal_id, started_at, deadline, success_signal, _now_utc(), user_id))
        self.conn.commit()

    def close_goal(self, user_id: str, status: str):
        """status: 'succeeded' | 'failed' | 'inconclusive'"""
        self.conn.execute(
            "UPDATE viewers SET goal_status=?, updated_at=? WHERE user_id=?",
            (status, _now_utc(), user_id)
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Decisions  (idempotent: same decision_id → no-op)
    # ------------------------------------------------------------------

    def save_decision(self, decision: dict) -> bool:
        """Returns True if saved, False if it was a duplicate (idempotent)."""
        try:
            self.conn.execute("""
                INSERT INTO decisions
                  (decision_id, user_id, trigger_event_id, decided_at, goal_id,
                   action_type, dialect_used, status, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                decision["decision_id"],
                decision["viewer_id"],
                decision.get("trigger", {}).get("event_id"),
                decision.get("decided_at"),
                decision.get("goal", {}).get("id") if decision.get("goal") else None,
                decision.get("action"),
                decision.get("dialect", {}).get("used"),
                decision.get("status"),
                json.dumps(decision, ensure_ascii=False)
            ))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            log.debug("Duplicate decision_id %s — skipped", decision.get("decision_id"))
            return False

    def get_decision(self, decision_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT payload_json FROM decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def decisions_for_user(self, user_id: str) -> List[dict]:
        rows = self.conn.execute(
            "SELECT payload_json FROM decisions WHERE user_id=? ORDER BY decided_at",
            (user_id,)
        ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]

    def all_decisions(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT payload_json FROM decisions ORDER BY decided_at"
        ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]

    # ------------------------------------------------------------------
    # Facts  (provenance store)
    # ------------------------------------------------------------------

    def upsert_fact(self, user_id: str, key: str, value: str,
                    source: str, received_at: str, freshness_note: str = ""):
        self.conn.execute("""
            INSERT INTO facts (user_id, fact_key, fact_value, source, received_at, freshness_note)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, fact_key, source) DO UPDATE SET
                fact_value=excluded.fact_value,
                received_at=excluded.received_at,
                freshness_note=excluded.freshness_note
        """, (user_id, key, value, source, received_at, freshness_note))
        self.conn.commit()

    def facts_for_user(self, user_id: str) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM facts WHERE user_id=?", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Outcomes
    # ------------------------------------------------------------------

    def save_outcome(self, decision_id: str, user_id: str, goal_id: str,
                     outcome: str, notes: str = ""):
        try:
            self.conn.execute("""
                INSERT INTO outcomes (decision_id, user_id, goal_id, judged_at, outcome, notes)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (decision_id, user_id, goal_id, _now_utc(), outcome, notes))
            self.conn.commit()
        except sqlite3.IntegrityError:
            pass  # already judged

    def all_outcomes(self) -> List[dict]:
        rows = self.conn.execute("SELECT * FROM outcomes ORDER BY judged_at").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Message frequency tracking
    # ------------------------------------------------------------------

    def messages_sent_today_ist(self, user_id: str, ist_date: str) -> int:
        """Count push+whatsapp actions sent on an IST calendar day."""
        count = self.conn.execute("""
            SELECT COUNT(*) FROM decisions
            WHERE user_id=? AND action_type IN ('PUSH', 'WHATSAPP')
            AND substr(last_action_day_ist, 1, 10) = ?
            AND status = 'PUBLISHED'
        """, (user_id, ist_date[:10])).fetchone()[0]
        return count

    def get_last_action_day(self, user_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT last_action_day_ist FROM viewers WHERE user_id=?", (user_id,)
        ).fetchone()
        return row["last_action_day_ist"] if row else None

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------------
    # Export helpers
    # ------------------------------------------------------------------

    def export_viewer_state(self, user_id: str, out_path: str):
        """Write a JSON snapshot of everything known for one viewer."""
        data = {
            "viewer": self.get_viewer(user_id),
            "facts": self.facts_for_user(user_id),
            "decisions": self.decisions_for_user(user_id),
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
