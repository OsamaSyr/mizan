#!/usr/bin/env python3
"""
MIZAN — review queue and audit log tests (src/mizan/review.py).

Runnable either way:
    python3 -m pytest tests/test_review.py -v
    python3 tests/test_review.py

What these tests defend
-----------------------
The audit log is only worth something if it cannot be rewritten. So the
load-bearing tests here try to UPDATE and DELETE recorded decisions straight
through sqlite3 — bypassing the store's own API, the way a person with the
sqlite3 CLI would — and require the database itself to refuse.

Every test uses its own throwaway database. Nothing touches data/review.sqlite
or corpus.sqlite.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mizan.review import (                                     # noqa: E402
    APPROVE, ESCALATE, REPLACE, ReviewError, ReviewStore,
)

# ----------------------------------------------------------------------
# Fixtures (plain functions, so the plain-python runner needs no pytest)
# ----------------------------------------------------------------------


class _Clock:
    """Deterministic timestamps: one second apart, in call order."""

    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return "2026-10-03T10:00:%02dZ" % self.n


def _store():
    d = tempfile.mkdtemp(prefix="mizan-review-test-")
    return ReviewStore(os.path.join(d, "review.sqlite"), clock=_Clock())


def _finding(idx, state, refer=None, ref="51:56"):
    s, a = (int(x) for x in ref.split(":"))
    f = {"idx": idx, "ref": ref, "surah": s, "ayah": a, "state": state, "lang": "en",
         "tier": "reference", "published_text": "published words %d" % idx,
         "approved_text": "approved words %d" % idx,
         "closest": {"book_id": 13638, "title": "Sahih International"}}
    if refer is not None:
        f["refer"] = refer
    return f


def _record(*findings):
    return {"lang": "en", "index_version": "2026-10-01", "document_verdict": "REFER",
            "findings": list(findings)}


def _saved(store):
    return store.save_check(
        _record(_finding(0, "UNATTRIBUTED", True),
                _finding(1, "MATCH", False),
                _finding(2, "UNRESOLVED", True, ref="2:255")),
        text_sha256="0" * 64)


# ----------------------------------------------------------------------
# Append-only: enforced by the database, not by convention
# ----------------------------------------------------------------------


def test_decisions_cannot_be_updated_in_the_database():
    st = _store()
    item = st.enqueue(_saved(st), 0)
    st.record_decision(item["id"], ESCALATE, "Reviewer A", "needs a scholar")
    con = sqlite3.connect(st.path)
    try:
        con.execute("UPDATE review_events SET reviewer = 'someone else'")
        con.commit()
        raise AssertionError("UPDATE on review_events was accepted")
    except sqlite3.IntegrityError as e:
        assert "append-only" in str(e)
    finally:
        con.close()
    assert st.get_item(item["id"])["events"][0]["reviewer"] == "Reviewer A"


def test_decisions_cannot_be_deleted_in_the_database():
    st = _store()
    item = st.enqueue(_saved(st), 0)
    st.record_decision(item["id"], APPROVE, "Reviewer A")
    con = sqlite3.connect(st.path)
    try:
        con.execute("DELETE FROM review_events")
        con.commit()
        raise AssertionError("DELETE on review_events was accepted")
    except sqlite3.IntegrityError:
        pass
    finally:
        con.close()
    assert len(st.get_item(item["id"])["events"]) == 1


def test_review_items_cannot_be_rewritten_or_deleted():
    st = _store()
    item = st.enqueue(_saved(st), 0)
    con = sqlite3.connect(st.path)
    for sql in ("UPDATE review_items SET published_text = 'changed'",
                "DELETE FROM review_items"):
        try:
            con.execute(sql)
            con.commit()
            raise AssertionError("accepted: " + sql)
        except sqlite3.IntegrityError:
            pass
    con.close()
    assert st.get_item(item["id"])["published_text"] == "published words 0"


def test_saved_checks_are_immutable():
    st = _store()
    cid = _saved(st)
    con = sqlite3.connect(st.path)
    try:
        con.execute("UPDATE checks SET verdict = 'CLEAR'")
        con.commit()
        raise AssertionError("UPDATE on checks was accepted")
    except sqlite3.IntegrityError:
        pass
    finally:
        con.close()
    assert st.get_check(cid)["document_verdict"] == "REFER"


def test_a_check_behind_a_review_item_cannot_be_deleted():
    st = _store()
    cid = _saved(st)
    st.enqueue(cid, 0)
    con = sqlite3.connect(st.path)
    try:
        con.execute("DELETE FROM checks WHERE id = ?", (cid,))
        con.commit()
        raise AssertionError("deleted a check the audit log points at")
    except sqlite3.IntegrityError:
        pass
    finally:
        con.close()
    assert st.get_check(cid) is not None


def test_changing_your_mind_adds_an_event_and_keeps_the_old_one():
    st = _store()
    item = st.enqueue(_saved(st), 0)
    st.record_decision(item["id"], APPROVE, "Reviewer A", "looks fine")
    after = st.record_decision(item["id"], ESCALATE, "Reviewer B", "actually, ask a scholar")
    events = after["events"]
    assert [e["decision"] for e in events] == [APPROVE, ESCALATE]
    assert events[0]["reviewer"] == "Reviewer A" and events[0]["note"] == "looks fine"
    assert events[0]["created_at"] < events[1]["created_at"]
    assert after["status"] == "escalated" and after["is_open"]


def test_audit_log_is_newest_first_and_complete():
    st = _store()
    cid = _saved(st)
    a = st.enqueue(cid, 0)
    b = st.enqueue(cid, 2)
    st.record_decision(a["id"], ESCALATE, "R1")
    st.record_decision(b["id"], ESCALATE, "R2")
    st.record_decision(a["id"], APPROVE, "R3")
    log = st.audit_log()
    assert [e["reviewer"] for e in log] == ["R3", "R2", "R1"]
    assert all(e["check_id"] == cid for e in log)
    assert {e["ref"] for e in log} == {"51:56", "2:255"}


# ----------------------------------------------------------------------
# Queue rules
# ----------------------------------------------------------------------


def test_only_referred_findings_can_be_sent_to_review():
    st = _store()
    cid = _saved(st)
    try:
        st.enqueue(cid, 1)       # MATCH
        raise AssertionError("a MATCH finding was queued")
    except ReviewError as e:
        assert e.code == "NOT_REVIEWABLE"
    assert st.enqueue(cid, 0)["state"] == "UNATTRIBUTED"
    assert st.enqueue(cid, 2)["state"] == "UNRESOLVED"


def test_records_without_a_refer_flag_fall_back_to_the_state_list():
    st = _store()
    cid = st.save_check(_record(_finding(0, "UNATTRIBUTED"), _finding(1, "MATCH"),
                                _finding(2, "NEAR")),
                        text_sha256="1" * 64)
    assert st.enqueue(cid, 0)["created"]
    # NEAR refers since 2026-10-04: its words differ from every approved text.
    assert st.enqueue(cid, 2)["created"]
    try:
        st.enqueue(cid, 1)
        raise AssertionError("a MATCH finding was queued")
    except ReviewError as e:
        assert e.code == "NOT_REVIEWABLE"


def test_enqueue_is_idempotent():
    st = _store()
    cid = _saved(st)
    first = st.enqueue(cid, 0)
    again = st.enqueue(cid, 0)
    assert first["created"] and not again["created"]
    assert first["id"] == again["id"]
    assert st.counts() == {"open": 1, "closed": 0, "total": 1}


def test_unknown_check_and_finding_are_refused():
    st = _store()
    for cid, idx, code in (("ffffffffffffffff", 0, "CHECK_NOT_FOUND"),
                           (_saved(st), 9, "FINDING_NOT_FOUND")):
        try:
            st.enqueue(cid, idx)
            raise AssertionError("accepted %s/%s" % (cid, idx))
        except ReviewError as e:
            assert e.code == code and e.status == 404


def test_status_filters():
    st = _store()
    cid = _saved(st)
    a = st.enqueue(cid, 0)
    b = st.enqueue(cid, 2)
    st.record_decision(a["id"], REPLACE, "R", replacement={
        "book_id": 13638, "title": "Sahih International", "text": "approved words 0"})
    st.record_decision(b["id"], ESCALATE, "R")
    assert [i["id"] for i in st.list_items("open")] == [b["id"]]
    assert [i["id"] for i in st.list_items("closed")] == [a["id"]]
    assert len(st.list_items("all")) == 2
    try:
        st.list_items("everything")
        raise AssertionError("unknown filter accepted")
    except ReviewError as e:
        assert e.code == "BAD_STATUS"


# ----------------------------------------------------------------------
# Decision validation — Arabic messages that say what to do
# ----------------------------------------------------------------------


def test_decision_requires_a_reviewer_and_a_known_decision():
    st = _store()
    item = st.enqueue(_saved(st), 0)
    for args, code in (((item["id"], APPROVE, "   "), "REVIEWER_REQUIRED"),
                       ((item["id"], "delete_it", "R"), "BAD_DECISION"),
                       ((item["id"], APPROVE, "x" * 81), "REVIEWER_TOO_LONG"),
                       ((item["id"], REPLACE, "R"), "REPLACEMENT_REQUIRED"),
                       ((999, APPROVE, "R"), "ITEM_NOT_FOUND")):
        try:
            st.record_decision(*args)
            raise AssertionError("accepted %r" % (args,))
        except ReviewError as e:
            assert e.code == code, (e.code, code)
            assert any("؀" <= ch <= "ۿ" for ch in e.message), "message is not Arabic"
    assert st.get_item(item["id"])["events"] == []


def test_replacement_is_stored_verbatim_with_its_source():
    st = _store()
    item = st.enqueue(_saved(st), 0)
    text = "And I did not create the jinn and mankind except to worship Me."
    after = st.record_decision(item["id"], REPLACE, "R", "use the approved text",
                               replacement={"book_id": 13638, "title": "Sahih International",
                                            "text": text})
    rep = after["latest"]["replacement"]
    assert rep == {"book_id": 13638, "title": "Sahih International", "text": text}
    assert after["status"] == "replaced_with_approved" and not after["is_open"]


def test_review_db_is_separate_from_the_corpus():
    st = _store()
    assert os.path.basename(st.path) == "review.sqlite"
    con = sqlite3.connect(st.path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "ayat" not in tables and "translations" not in tables
    assert {"checks", "review_items", "review_events"} <= tables


def test_retention_purges_only_unreviewed_old_checks():
    st = _store()
    old_free = st.save_check(dict(_record(_finding(0, "UNATTRIBUTED", True)),
                                  created_at="2020-01-01T00:00:00Z"), text_sha256="2" * 64)
    old_reviewed = st.save_check(dict(_record(_finding(0, "UNATTRIBUTED", True)),
                                      created_at="2020-01-01T00:00:00Z"), text_sha256="3" * 64)
    st.enqueue(old_reviewed, 0)
    fresh = _saved(st)
    assert st.purge_checks(0) == 0                       # 0 disables purging
    assert st.purge_checks(30) == 1
    assert st.get_check(old_free) is None
    assert st.get_check(old_reviewed) is not None
    assert st.get_check(fresh) is not None


def test_saved_check_round_trips_with_its_hash():
    st = _store()
    cid = _saved(st)
    rec = st.get_check(cid)
    assert rec["check_id"] == cid and rec["text_sha256"] == "0" * 64
    assert len(rec["findings"]) == 3 and len(cid) == 16


# ======================================================================
# Plain-python runner
# ======================================================================

def _main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed.append(name)
            print(f"  FAIL  {name}\n          {e}")
        except Exception as e:                      # noqa: BLE001
            failed.append(name)
            print(f"  ERROR {name}\n          {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
