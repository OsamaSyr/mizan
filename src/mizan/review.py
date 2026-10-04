#!/usr/bin/env python3
"""
MIZAN — human review queue, append-only audit log, and saved checks.

What this module is for
-----------------------
MIZAN never decides that a quotation is acceptable on its own authority. When
a quotation cannot be attributed to an approved translation, the document is
referred, and a PERSON decides what happens next. This module records that
person's decision so it can be traced later: who decided, what they decided,
why, and when.

Three tables, in its OWN SQLite file (default `data/review.sqlite`). It never
opens, reads or writes `corpus.sqlite`.

    checks         one row per document check: the findings and the verdict,
                   kept so a report can be downloaded and a finding can be sent
                   to review later. The full translation and the Arabic source
                   are NOT stored — only the quoted spans the review needs, and
                   a SHA-256 of the translation so a later request can prove it
                   is talking about the same text.
    review_items   one row per finding sent to review (snapshot at that moment).
    review_events  one row per reviewer decision. THE AUDIT LOG.

Append-only, enforced by the database, not by convention
--------------------------------------------------------
SQLite triggers abort every UPDATE and DELETE on the two review tables, and
every UPDATE on saved checks. The only delete the database accepts is the
retention purge of a check no review item refers to. A decision
is never overwritten: a reviewer who changes their mind adds a new event, and
the item's status is simply the latest event. The earlier event stays visible
in the history with its original reviewer, note and timestamp.

Privacy
-------
No accounts, no IP addresses, no user agents, no cookies, no analytics. The
reviewer name is free text the reviewer types themselves; it is the one piece of
personal data kept, because an audit log without it is not an audit log.

Stdlib only.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DEFAULT_DB = os.path.join(ROOT, "data", "review.sqlite")

SCHEMA_VERSION = 1

# ----------------------------------------------------------------------------
# Vocabulary. Machine codes are part of the API contract; the Arabic labels are
# what a reviewer reads. Neither ever says a text is "wrong".
# ----------------------------------------------------------------------------

APPROVE = "approve_as_published"
REPLACE = "replace_with_approved"
ESCALATE = "escalate"

DECISIONS: Dict[str, str] = {
    APPROVE: "اعتماد كما نُشر",
    REPLACE: "استبدال بالنص المعتمد",
    ESCALATE: "إحالة إلى عالم مختص",
}

# Status of an item = its latest decision (or none yet).
STATUS_PENDING = "pending"
STATUS_BY_DECISION = {
    APPROVE: "approved_as_published",
    REPLACE: "replaced_with_approved",
    ESCALATE: "escalated",
}
STATUS_LABELS: Dict[str, str] = {
    STATUS_PENDING: "بانتظار المراجعة",
    "escalated": "أُحيل إلى عالم مختص",
    "approved_as_published": "اعتُمد كما نُشر",
    "replaced_with_approved": "استُبدل بالنص المعتمد",
}
# An escalated item is still open: someone with the authority has yet to decide.
OPEN_STATUSES = {STATUS_PENDING, "escalated"}

# Only findings that pulled the document to REFER can be sent to review. These
# are the states in which there was approved text to compare against (or a
# quotation to resolve) and the comparison did not land. NEAR — close to an
# approved rendering but with different words — refers since 2026-10-04.
REVIEWABLE_STATES = {"NEAR", "UNATTRIBUTED", "UNRESOLVED"}

MAX_REVIEWER_CHARS = 80
MAX_NOTE_CHARS = 2000


class ReviewError(ValueError):
    """A request the store refuses. Carries a stable code and an Arabic message
    that tells the person what to do next."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def utc_now() -> str:
    """ISO-8601 UTC timestamp, second precision, 'Z' suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_check_id() -> str:
    """16 hex characters. Unguessable enough that one report URL does not reveal
    the next, which matters once the server is reachable by more than one person."""
    return secrets.token_hex(8)


# ----------------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checks(
    id            TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    lang          TEXT NOT NULL,
    index_version TEXT NOT NULL,
    verdict       TEXT NOT NULL,
    text_sha256   TEXT NOT NULL,
    payload       TEXT NOT NULL            -- JSON; see save_check()
);

CREATE TABLE IF NOT EXISTS review_items(
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id        TEXT NOT NULL REFERENCES checks(id),
    finding_idx     INTEGER NOT NULL,
    ref             TEXT,
    surah           INTEGER,
    ayah            INTEGER,
    state           TEXT NOT NULL,
    lang            TEXT NOT NULL,
    tier            TEXT,
    published_text  TEXT NOT NULL DEFAULT '',
    closest_book_id INTEGER,
    closest_title   TEXT,
    closest_text    TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(check_id, finding_idx)
);

CREATE TABLE IF NOT EXISTS review_events(
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id             INTEGER NOT NULL REFERENCES review_items(id),
    decision            TEXT NOT NULL
                        CHECK (decision IN ('approve_as_published',
                                            'replace_with_approved',
                                            'escalate')),
    reviewer            TEXT NOT NULL CHECK (length(trim(reviewer)) > 0),
    note                TEXT NOT NULL DEFAULT '',
    replacement_book_id INTEGER,
    replacement_title   TEXT,
    replacement_text    TEXT,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_item ON review_events(item_id, id);
CREATE INDEX IF NOT EXISTS idx_items_check ON review_items(check_id);
CREATE INDEX IF NOT EXISTS idx_checks_created ON checks(created_at);
"""

# Append-only, enforced in the database. Any UPDATE or DELETE on the review
# tables aborts, whichever code path (or person with the sqlite3 CLI) attempts
# it. Saved checks can never be edited either; the one thing allowed is the
# retention purge deleting a check that NO review item refers to — evidence
# behind a decision is kept for as long as the decision is.
_APPEND_ONLY_TABLES = ("review_items", "review_events")


def _append_only_triggers() -> str:
    out = []
    for table in _APPEND_ONLY_TABLES:
        for verb in ("UPDATE", "DELETE"):
            out.append(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{verb.lower()} "
                f"BEFORE {verb} ON {table} BEGIN "
                f"SELECT RAISE(ABORT, '{table} is append-only'); END;"
            )
    out.append(
        "CREATE TRIGGER IF NOT EXISTS checks_no_update BEFORE UPDATE ON checks "
        "BEGIN SELECT RAISE(ABORT, 'checks are immutable'); END;"
    )
    out.append(
        "CREATE TRIGGER IF NOT EXISTS checks_keep_reviewed BEFORE DELETE ON checks "
        "WHEN EXISTS (SELECT 1 FROM review_items WHERE check_id = OLD.id) "
        "BEGIN SELECT RAISE(ABORT, 'check is referenced by the review log'); END;"
    )
    return "\n".join(out)


# ----------------------------------------------------------------------------
# Store
# ----------------------------------------------------------------------------


class ReviewStore:
    """
    The review database. One instance per process; safe across threads because
    every operation opens its own short-lived connection.

        store = ReviewStore()                      # data/review.sqlite
        cid = store.save_check(record)
        item = store.enqueue(cid, finding_idx=0)
        store.record_decision(item["id"], "escalate", "Reviewer A", "needs a scholar")
    """

    def __init__(self, path: Optional[str] = None,
                 clock: Callable[[], str] = utc_now):
        self.path = path or os.environ.get("MIZAN_REVIEW_DB") or DEFAULT_DB
        self._clock = clock
        self._init_lock = threading.Lock()
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        self._init_schema()

    # -- plumbing ------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def _init_schema(self) -> None:
        with self._init_lock:
            con = self._connect()
            try:
                con.execute("PRAGMA journal_mode = WAL")
                con.executescript(_SCHEMA)
                con.executescript(_append_only_triggers())
                con.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                con.commit()
            finally:
                con.close()

    # -- checks --------------------------------------------------------------

    def save_check(self, record: Dict[str, Any], *, text_sha256: str) -> str:
        """
        Persist one check and return its id.

        `record` is the minimal JSON-serialisable result the report and the
        review need: verdict, counts, index metadata and the findings (the
        quoted span, the approved text, the attributed translation, the
        source links). The caller is responsible for NOT putting the full
        document in it; this function stores what it is given.
        """
        check_id = record.get("check_id") or new_check_id()
        created_at = record.get("created_at") or self._clock()
        record = dict(record, check_id=check_id, created_at=created_at)
        con = self._connect()
        try:
            con.execute(
                "INSERT INTO checks(id, created_at, lang, index_version, verdict, "
                "text_sha256, payload) VALUES (?,?,?,?,?,?,?)",
                (
                    check_id,
                    created_at,
                    str(record.get("lang") or ""),
                    str(record.get("index_version") or ""),
                    str(record.get("document_verdict") or ""),
                    text_sha256,
                    json.dumps(record, ensure_ascii=False),
                ),
            )
            con.commit()
        finally:
            con.close()
        return check_id

    def get_check(self, check_id: str) -> Optional[Dict[str, Any]]:
        """The stored record, or None. Includes `text_sha256`."""
        con = self._connect()
        try:
            row = con.execute(
                "SELECT payload, text_sha256 FROM checks WHERE id = ?", (check_id,)
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        rec = json.loads(row["payload"])
        rec["text_sha256"] = row["text_sha256"]
        return rec

    def purge_checks(self, older_than_days: int) -> int:
        """
        Delete saved checks older than N days that no review item refers to.

        This is the declared retention policy: a check exists so its report can
        be downloaded; once that window has passed, it is removed. A check that
        a reviewer acted on is kept, because the audit log points at it (the
        database refuses that delete even if this filter were wrong).
        Returns how many were removed. `older_than_days <= 0` disables purging.
        """
        if older_than_days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400
        cutoff_iso = (datetime.fromtimestamp(cutoff, timezone.utc)
                      .isoformat(timespec="seconds").replace("+00:00", "Z"))
        con = self._connect()
        try:
            cur = con.execute(
                "DELETE FROM checks WHERE created_at < ? AND id NOT IN "
                "(SELECT DISTINCT check_id FROM review_items)",
                (cutoff_iso,),
            )
            con.commit()
            return cur.rowcount
        finally:
            con.close()

    def count_checks(self) -> int:
        con = self._connect()
        try:
            return con.execute("SELECT COUNT(*) FROM checks").fetchone()[0]
        finally:
            con.close()

    # -- queue ---------------------------------------------------------------

    def enqueue(self, check_id: str, finding_idx: int) -> Dict[str, Any]:
        """
        Send one finding of a saved check to human review.

        Idempotent: sending the same finding twice returns the existing item
        rather than creating a duplicate (a reviewer should see it once).
        Raises ReviewError if the check or finding does not exist, or if the
        finding is not in a referral state.
        """
        rec = self.get_check(check_id)
        if rec is None:
            raise ReviewError(
                "CHECK_NOT_FOUND",
                "لا يوجد فحص بهذا المعرّف. أعد فحص الوثيقة من الصفحة الرئيسية ثم أرسل البند للمراجعة.",
                404,
            )
        findings = rec.get("findings") or []
        if not isinstance(finding_idx, int) or not (0 <= finding_idx < len(findings)):
            raise ReviewError(
                "FINDING_NOT_FOUND",
                f"لا يوجد بند برقم {finding_idx} في هذا الفحص. اختر بندًا من نتائج الفحص نفسه.",
                404,
            )
        f = findings[finding_idx]
        state = f.get("state")
        # The check record says whether this finding pulled the document to
        # REFER; older records without the flag fall back to the state list.
        refer = f.get("refer")
        if refer is None:
            refer = state in REVIEWABLE_STATES
        if not refer:
            raise ReviewError(
                "NOT_REVIEWABLE",
                "هذا البند ليس محالًا (حالته: %s). تُرسَل إلى المراجعة البنود المحالة فقط: "
                "المختلفة كلماتها عن الترجمة المعتمدة، وغير المُسنَدة، وغير المحلولة." % state,
                400,
            )

        con = self._connect()
        try:
            existing = con.execute(
                "SELECT id FROM review_items WHERE check_id = ? AND finding_idx = ?",
                (check_id, finding_idx),
            ).fetchone()
            if existing is None:
                closest = f.get("closest") or {}
                con.execute(
                    "INSERT INTO review_items(check_id, finding_idx, ref, surah, ayah, state, "
                    "lang, tier, published_text, closest_book_id, closest_title, closest_text, "
                    "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        check_id,
                        finding_idx,
                        f.get("ref"),
                        f.get("surah"),
                        f.get("ayah"),
                        state,
                        str(f.get("lang") or rec.get("lang") or ""),
                        f.get("tier"),
                        f.get("published_text") or "",
                        closest.get("book_id"),
                        closest.get("title"),
                        f.get("approved_text"),
                        self._clock(),
                    ),
                )
                con.commit()
                item_id = con.execute(
                    "SELECT id FROM review_items WHERE check_id = ? AND finding_idx = ?",
                    (check_id, finding_idx),
                ).fetchone()[0]
                created = True
            else:
                item_id = existing[0]
                created = False
        finally:
            con.close()

        item = self.get_item(item_id)
        assert item is not None
        item["created"] = created
        return item

    def get_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        """One item with its full, oldest-first decision history."""
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM review_items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                return None
            events = con.execute(
                "SELECT * FROM review_events WHERE item_id = ? ORDER BY id", (item_id,)
            ).fetchall()
        finally:
            con.close()
        return self._item_dict(row, [self._event_dict(e) for e in events])

    def list_items(self, status: str = "open", check_id: Optional[str] = None
                   ) -> List[Dict[str, Any]]:
        """
        Items filtered by status: 'open' (pending or escalated), 'closed'
        (approved or replaced), or 'all'. Newest first.
        """
        if status not in ("open", "closed", "all"):
            raise ReviewError(
                "BAD_STATUS",
                "مرشّح غير معروف. استخدم: open (المفتوحة) أو closed (المغلقة) أو all (الكل).",
            )
        con = self._connect()
        try:
            if check_id:
                rows = con.execute(
                    "SELECT * FROM review_items WHERE check_id = ? ORDER BY id DESC", (check_id,)
                ).fetchall()
            else:
                rows = con.execute("SELECT * FROM review_items ORDER BY id DESC").fetchall()
            events = con.execute("SELECT * FROM review_events ORDER BY id").fetchall()
        finally:
            con.close()

        by_item: Dict[int, List[Dict[str, Any]]] = {}
        for e in events:
            by_item.setdefault(e["item_id"], []).append(self._event_dict(e))

        out = []
        for r in rows:
            item = self._item_dict(r, by_item.get(r["id"], []))
            is_open = item["status"] in OPEN_STATUSES
            if status == "all" or (status == "open") == is_open:
                out.append(item)
        return out

    def items_for_check(self, check_id: str) -> List[Dict[str, Any]]:
        return self.list_items("all", check_id=check_id)

    def counts(self) -> Dict[str, int]:
        items = self.list_items("all")
        out = {"open": 0, "closed": 0, "total": len(items)}
        for it in items:
            out["open" if it["status"] in OPEN_STATUSES else "closed"] += 1
        return out

    # -- decisions -----------------------------------------------------------

    def record_decision(self, item_id: int, decision: str, reviewer: str,
                        note: str = "", *,
                        replacement: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Append one decision to the audit log and return the updated item.

        `replacement` is required for REPLACE and must be the approved
        rendering exactly as the index holds it: {"book_id", "title", "text"}.
        The caller looks it up from the corpus; this module never composes or
        edits scripture, it only records which approved text a person chose.
        """
        if decision not in DECISIONS:
            raise ReviewError(
                "BAD_DECISION",
                "قرار غير معروف. اختر واحدًا: اعتماد كما نُشر، أو استبدال بالنص المعتمد، "
                "أو إحالة إلى عالم مختص.",
            )
        reviewer = (reviewer or "").strip()
        if not reviewer:
            raise ReviewError(
                "REVIEWER_REQUIRED",
                "اسم المراجع مطلوب. اكتب اسمك أو صفتك ليُسجَّل القرار باسمك في سجل التدقيق.",
            )
        if len(reviewer) > MAX_REVIEWER_CHARS:
            raise ReviewError(
                "REVIEWER_TOO_LONG",
                f"اسم المراجع أطول من {MAX_REVIEWER_CHARS} حرفًا. اختصره ثم أعد الإرسال.",
            )
        note = (note or "").strip()
        if len(note) > MAX_NOTE_CHARS:
            raise ReviewError(
                "NOTE_TOO_LONG",
                f"الملاحظة أطول من {MAX_NOTE_CHARS} حرف. اختصرها ثم أعد الإرسال.",
            )

        rep_id = rep_title = rep_text = None
        if decision == REPLACE:
            if not replacement or not replacement.get("text"):
                raise ReviewError(
                    "REPLACEMENT_REQUIRED",
                    "اختر الترجمة المعتمدة التي يُستبدل بها النص المنشور، ثم أعد الإرسال.",
                )
            rep_id = replacement.get("book_id")
            rep_title = replacement.get("title")
            rep_text = replacement.get("text")

        if self.get_item(item_id) is None:
            raise ReviewError(
                "ITEM_NOT_FOUND",
                "لا يوجد بند مراجعة بهذا الرقم. حدّث صفحة المراجعة لترى الطابور الحالي.",
                404,
            )

        con = self._connect()
        try:
            con.execute(
                "INSERT INTO review_events(item_id, decision, reviewer, note, "
                "replacement_book_id, replacement_title, replacement_text, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (item_id, decision, reviewer, note, rep_id, rep_title, rep_text, self._clock()),
            )
            con.commit()
        finally:
            con.close()

        item = self.get_item(item_id)
        assert item is not None
        return item

    def audit_log(self, limit: int = 500) -> List[Dict[str, Any]]:
        """Every decision ever recorded, newest first, joined to its item."""
        limit = max(1, min(int(limit), 5000))
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT e.*, i.ref AS ref, i.check_id AS check_id, i.state AS state, "
                "i.lang AS lang FROM review_events e JOIN review_items i ON i.id = e.item_id "
                "ORDER BY e.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            con.close()
        out = []
        for r in rows:
            ev = self._event_dict(r)
            ev.update(ref=r["ref"], check_id=r["check_id"], state=r["state"], lang=r["lang"])
            out.append(ev)
        return out

    # -- shaping -------------------------------------------------------------

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> Dict[str, Any]:
        d = {
            "id": row["id"],
            "item_id": row["item_id"],
            "decision": row["decision"],
            "decision_label": DECISIONS.get(row["decision"], row["decision"]),
            "reviewer": row["reviewer"],
            "note": row["note"],
            "created_at": row["created_at"],
        }
        if row["replacement_text"] is not None:
            d["replacement"] = {
                "book_id": row["replacement_book_id"],
                "title": row["replacement_title"],
                "text": row["replacement_text"],
            }
        return d

    @staticmethod
    def _item_dict(row: sqlite3.Row, events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        events = list(events)
        latest = events[-1] if events else None
        status = STATUS_BY_DECISION[latest["decision"]] if latest else STATUS_PENDING
        return {
            "id": row["id"],
            "check_id": row["check_id"],
            "finding_idx": row["finding_idx"],
            "ref": row["ref"],
            "surah": row["surah"],
            "ayah": row["ayah"],
            "state": row["state"],
            "lang": row["lang"],
            "tier": row["tier"],
            "published_text": row["published_text"],
            "closest": {
                "book_id": row["closest_book_id"],
                "title": row["closest_title"],
                "text": row["closest_text"],
            } if row["closest_text"] else None,
            "created_at": row["created_at"],
            "status": status,
            "status_label": STATUS_LABELS[status],
            "is_open": status in OPEN_STATUSES,
            "latest": latest,
            "events": events,
        }
