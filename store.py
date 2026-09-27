"""Context + conversation state for the Vera bot.

Kept separate from the HTTP layer so composer logic and tests can build a store
without standing up FastAPI. In-memory is explicitly allowed by the testing
brief (§2.1 "Storing in memory is fine; just don't restart between calls").
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

VALID_SCOPES = ("category", "merchant", "customer", "trigger")

# Durability. The brief allows in-memory storage but warns "don't restart between
# calls" (§2.1), and warmup fails outright unless /healthz reports all 255
# contexts (§4 Phase 1). On a free host a restart is not fully under our control,
# so state is mirrored to a local SQLite file and reloaded on boot.
#
# Local file, deliberately: §11 forbids transmitting payload data outside the
# test environment, so a hosted database is not an option for merchant or
# customer context. Set VERA_DB="" to run purely in memory.
DB_PATH = os.getenv("VERA_DB", str(Path(__file__).parent / "vera_state.db"))


def _connect(path: str) -> Optional[sqlite3.Connection]:
    try:
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS contexts (
                scope TEXT NOT NULL,
                context_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (scope, context_id)
            );
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY,
                state TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS suppressions (
                suppression_key TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL
            );
            """
        )
        conn.commit()
        return conn
    except sqlite3.Error:
        # Durability is insurance, never a hard dependency -- a read-only or full
        # filesystem must not stop the bot from serving.
        return None


class ContextStore:
    """Versioned context store, idempotent on (scope, context_id, version)."""

    def __init__(self, db_path: Optional[str] = DB_PATH) -> None:
        self._lock = threading.RLock()
        # (scope, context_id) -> {"version": int, "payload": dict}
        self._ctx: dict[tuple[str, str], dict[str, Any]] = {}
        self._db = _connect(db_path) if db_path else None
        self._reload()

    def _reload(self) -> None:
        """Repopulate memory from disk after a restart."""
        if self._db is None:
            return
        try:
            rows = self._db.execute(
                "SELECT scope, context_id, version, payload FROM contexts"
            ).fetchall()
        except sqlite3.Error:
            return
        for scope, cid, version, payload in rows:
            try:
                self._ctx[(scope, cid)] = {"version": version, "payload": json.loads(payload)}
            except json.JSONDecodeError:
                continue

    # --- writes ---------------------------------------------------------

    def put(self, scope: str, context_id: str, version: int, payload: dict) -> dict:
        """Store a context version.

        Returns {"accepted": True, ...} on success, or
        {"accepted": False, "reason": "stale_version", "current_version": N}
        when we already hold this version or newer. Re-posting an identical
        version is a no-op per the brief, and the judge expects a 409 for it
        (api-call-examples.md Example 1.5).
        """
        key = (scope, context_id)
        with self._lock:
            cur = self._ctx.get(key)
            if cur is not None and cur["version"] >= version:
                return {
                    "accepted": False,
                    "reason": "stale_version",
                    "current_version": cur["version"],
                }
            self._ctx[key] = {"version": version, "payload": payload}
            if self._db is not None:
                try:
                    self._db.execute(
                        "INSERT INTO contexts (scope, context_id, version, payload) "
                        "VALUES (?,?,?,?) ON CONFLICT(scope, context_id) DO UPDATE SET "
                        "version=excluded.version, payload=excluded.payload",
                        (scope, context_id, version, json.dumps(payload, ensure_ascii=False)),
                    )
                    self._db.commit()
                except sqlite3.Error:
                    pass  # memory is still correct; persistence is best-effort
            return {"accepted": True, "version": version}

    # --- reads ----------------------------------------------------------

    def get(self, scope: str, context_id: Optional[str]) -> Optional[dict]:
        if not context_id:
            return None
        with self._lock:
            rec = self._ctx.get((scope, context_id))
            return rec["payload"] if rec else None

    def version_of(self, scope: str, context_id: str) -> Optional[int]:
        with self._lock:
            rec = self._ctx.get((scope, context_id))
            return rec["version"] if rec else None

    def all_of(self, scope: str) -> dict[str, dict]:
        with self._lock:
            return {
                cid: rec["payload"]
                for (s, cid), rec in self._ctx.items()
                if s == scope
            }

    def counts(self) -> dict[str, int]:
        counts = {s: 0 for s in VALID_SCOPES}
        with self._lock:
            for (scope, _cid) in self._ctx:
                counts[scope] = counts.get(scope, 0) + 1
        return counts

    def resolve_bundle(self, trigger_id: str) -> Optional[dict]:
        """Resolve a trigger id into the 4-context bundle compose() needs.

        Returns None when the trigger, its merchant, or that merchant's category
        hasn't been pushed yet -- the bot simply declines to act on it.
        """
        trigger = self.get("trigger", trigger_id)
        if not trigger:
            return None
        merchant_id = trigger.get("merchant_id")
        merchant = self.get("merchant", merchant_id)
        if not merchant:
            return None
        category = self.get("category", merchant.get("category_slug"))
        if not category:
            return None
        customer = self.get("customer", trigger.get("customer_id"))
        return {
            "category": category,
            "merchant": merchant,
            "trigger": trigger,
            "customer": customer,
        }

    def wipe(self) -> int:
        with self._lock:
            n = len(self._ctx)
            self._ctx.clear()
            if self._db is not None:
                try:
                    self._db.execute("DELETE FROM contexts")
                    self._db.commit()
                except sqlite3.Error:
                    pass
            return n


class ConversationStore:
    """Per-conversation turn log + the state the reply handler reasons over."""

    def __init__(self, db_path: Optional[str] = DB_PATH) -> None:
        self._lock = threading.RLock()
        self._convs: dict[str, dict[str, Any]] = {}
        # suppression_key -> conversation_id that consumed it
        self._suppressed: dict[str, str] = {}
        self._db = _connect(db_path) if db_path else None
        self._reload()

    def _reload(self) -> None:
        if self._db is None:
            return
        try:
            for cid, state in self._db.execute(
                "SELECT conversation_id, state FROM conversations"
            ).fetchall():
                self._convs[cid] = json.loads(state)
            for key, cid in self._db.execute(
                "SELECT suppression_key, conversation_id FROM suppressions"
            ).fetchall():
                self._suppressed[key] = cid
        except (sqlite3.Error, json.JSONDecodeError):
            return

    def _persist(self, conversation_id: str) -> None:
        if self._db is None:
            return
        conv = self._convs.get(conversation_id)
        if conv is None:
            return
        try:
            self._db.execute(
                "INSERT INTO conversations (conversation_id, state) VALUES (?,?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET state=excluded.state",
                (conversation_id, json.dumps(conv, ensure_ascii=False)),
            )
            self._db.commit()
        except (sqlite3.Error, TypeError):
            pass

    def start(
        self,
        conversation_id: str,
        merchant_id: Optional[str],
        customer_id: Optional[str],
        trigger_id: Optional[str],
    ) -> dict:
        with self._lock:
            conv = self._convs.setdefault(
                conversation_id,
                {
                    "conversation_id": conversation_id,
                    "merchant_id": merchant_id,
                    "customer_id": customer_id,
                    "trigger_id": trigger_id,
                    "turns": [],
                    "bodies_sent": [],
                    "ended": False,
                    "nudges_unanswered": 0,
                },
            )
            # Late-arriving identity (e.g. /v1/reply before we ever sent) fills in.
            if merchant_id and not conv.get("merchant_id"):
                conv["merchant_id"] = merchant_id
            if customer_id and not conv.get("customer_id"):
                conv["customer_id"] = customer_id
        self._persist(conversation_id)
        with self._lock:
            return self._convs[conversation_id]

    def get(self, conversation_id: str) -> Optional[dict]:
        with self._lock:
            return self._convs.get(conversation_id)

    def record_outbound(self, conversation_id: str, body: str, **meta: Any) -> None:
        with self._lock:
            conv = self._convs.get(conversation_id)
            if conv is None:
                return
            conv["turns"].append({"from": "bot", "body": body, **meta})
            conv["bodies_sent"].append(body)
            conv["nudges_unanswered"] += 1
        self._persist(conversation_id)

    def record_inbound(self, conversation_id: str, body: str, **meta: Any) -> None:
        with self._lock:
            conv = self._convs.get(conversation_id)
            if conv is None:
                return
            conv["turns"].append({"from": "counterparty", "body": body, **meta})
            conv["nudges_unanswered"] = 0
        self._persist(conversation_id)

    def already_sent(self, conversation_id: str, body: str) -> bool:
        """Anti-repetition guard -- verbatim resend costs -2 per the brief."""
        with self._lock:
            conv = self._convs.get(conversation_id)
            return bool(conv and body in conv["bodies_sent"])

    def sent_to_merchant(self, merchant_id: Optional[str], body: str) -> bool:
        """True if this exact body already went to this merchant, any conversation.

        The failure table only penalises repeats within one conversation_id, but
        two placeholder triggers of the same kind for the same merchant compose
        identically, and sending the same words twice is a quality problem
        whether or not it is scored as one.
        """
        if not merchant_id or not body:
            return False
        with self._lock:
            return any(
                conv.get("merchant_id") == merchant_id and body in conv["bodies_sent"]
                for conv in self._convs.values()
            )

    def mark_ended(self, conversation_id: str) -> None:
        with self._lock:
            conv = self._convs.get(conversation_id)
            if conv:
                conv["ended"] = True
        self._persist(conversation_id)

    def is_ended(self, conversation_id: str) -> bool:
        with self._lock:
            conv = self._convs.get(conversation_id)
            return bool(conv and conv["ended"])

    def exists(self, conversation_id: str) -> bool:
        with self._lock:
            return conversation_id in self._convs

    # --- suppression ----------------------------------------------------

    def is_suppressed(self, suppression_key: str) -> bool:
        if not suppression_key:
            return False
        with self._lock:
            return suppression_key in self._suppressed

    def suppress(self, suppression_key: str, conversation_id: str) -> None:
        if not suppression_key:
            return
        with self._lock:
            self._suppressed[suppression_key] = conversation_id
            if self._db is not None:
                try:
                    self._db.execute(
                        "INSERT OR REPLACE INTO suppressions "
                        "(suppression_key, conversation_id) VALUES (?,?)",
                        (suppression_key, conversation_id),
                    )
                    self._db.commit()
                except sqlite3.Error:
                    pass

    def wipe(self) -> int:
        with self._lock:
            n = len(self._convs)
            self._convs.clear()
            self._suppressed.clear()
            if self._db is not None:
                try:
                    self._db.execute("DELETE FROM conversations")
                    self._db.execute("DELETE FROM suppressions")
                    self._db.commit()
                except sqlite3.Error:
                    pass
            return n
