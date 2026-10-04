#!/usr/bin/env python3
"""
MIZAN — web application tests (app.py, over real HTTP).

Runnable either way:
    python3 -m pytest tests/test_app.py -v
    python3 tests/test_app.py

Starts the real server (`python3 app.py`) on a random free port, twice:

    main   exactly as shipped — whatever detection, clarity and terminology
           modules are installed right now;
    stub   the same, with Panel 2 pointed at a tiny stand-in terminology module
           written to a temp dir, so the terminology wiring is exercised even
           before src/mizan/terms.py exists.

Each server gets its own throwaway review database (MIZAN_REVIEW_DB), so the
tests never touch data/review.sqlite. A handful of tests also import app.py
in-process to check pure functions (URL pattern, verdict rule, AI disclosure,
fallback labelling, timeouts) that cannot be forced over HTTP.

Detection output is owned by another track and still moving, so these tests
assert the APP's contract — shapes, invariants, persistence, errors — rather
than which verse a given sample resolves to.
"""
import http.client
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")

SAMPLE_AR = ("ثم بيّن سبحانه أن الخلق لم يُترك سدى، فقال تعالى: ﴿وَمَا خَلَقْتُ الْجِنَّ "
             "وَالْإِنسَ إِلَّا لِيَعْبُدُونِ﴾ [الذاريات: ٥٦]. وهذه الآية أصلٌ في بيان الغاية "
             "من وجود الإنسان، وعليها يُبنى ما بعدها من أحكام.")
SAMPLE_EN = ('Then He clarified that creation was not left purposeless, saying: "And I did '
             'not create the jinn and mankind except to serve Me." (Quran 51:56). This verse '
             'is a foundation for understanding the purpose of human existence.')
PROSE_ONLY_SENTENCE = "This verse is a foundation for understanding the purpose of human existence."

# A stand-in for src/mizan/terms.py, honouring the agreed contract exactly.
TERMS_STUB = '''
from dataclasses import dataclass, field

@dataclass
class TermsReport:
    findings: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    def as_dict(self):
        return {"findings": self.findings, "counts": self.counts}

def check_terms(arabic, translation, lang):
    if "RAISE_TERMS" in translation:
        raise RuntimeError("stub failure")
    rows = [{"term_ar": "الشريعة", "approved_en": "Sharia", "status": "VARIANT",
             "found": "Islamic law", "suggestion": "Sharia",
             "source_url": "https://example.invalid/glossary/sharia"},
            {"term_ar": "التوحيد", "approved_en": "Tawhid", "status": "MISSING",
             "found": None, "suggestion": "Tawhid", "source_url": None}]
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return TermsReport(rows, counts)
'''

STATE = {}


# ----------------------------------------------------------------------
# Server lifecycle
# ----------------------------------------------------------------------


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start(name, extra_env):
    port = _free_port()
    env = dict(os.environ, MIZAN_PORT=str(port), MIZAN_HOST="127.0.0.1",
               MIZAN_REVIEW_DB=os.path.join(STATE["tmp"], name + "-review.sqlite"),
               PYTHONDONTWRITEBYTECODE="1")
    env.update(extra_env)
    log = open(os.path.join(STATE["tmp"], name + ".log"), "w")
    proc = subprocess.Popen([sys.executable, APP], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % port
    deadline = time.time() + 120
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("%s server exited; see %s" % (name, log.name))
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2) as r:
                if r.status == 200:
                    break
        except OSError:
            time.sleep(0.3)
    else:
        proc.kill()
        raise RuntimeError("%s server did not start in time" % name)
    return {"proc": proc, "base": base, "port": port, "log": log, "db": env["MIZAN_REVIEW_DB"]}


def setup_module(module=None):
    if STATE.get("main"):
        return
    STATE["tmp"] = tempfile.mkdtemp(prefix="mizan-app-test-")
    with open(os.path.join(STATE["tmp"], "mizan_terms_stub.py"), "w", encoding="utf-8") as fh:
        fh.write(TERMS_STUB)
    STATE["main"] = _start("main", {})
    STATE["stub"] = _start("stub", {"MIZAN_TERMS_MODULE": "mizan_terms_stub",
                                    "PYTHONPATH": STATE["tmp"]})


def teardown_module(module=None):
    for key in ("main", "stub"):
        srv = STATE.pop(key, None)
        if srv:
            srv["proc"].terminate()
            try:
                srv["proc"].wait(timeout=10)
            except subprocess.TimeoutExpired:
                srv["proc"].kill()
            srv["log"].close()


# ----------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------


def _req(method, path, body=None, *, server="main", headers=None, raw=None,
         ctype="application/json", timeout=120):
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    h = {"Content-Type": ctype} if data is not None else {}
    h.update(headers or {})
    req = urllib.request.Request(STATE[server]["base"] + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _json(method, path, body=None, **kw):
    status, headers, raw = _req(method, path, body, **kw)
    return status, json.loads(raw.decode("utf-8")), headers


def _is_arabic(s):
    return any("؀" <= ch <= "ۿ" for ch in s or "")


def _check(translation=SAMPLE_EN, arabic=SAMPLE_AR, lang="en", level="practising", server="main"):
    status, d, _ = _json("POST", "/api/check", {"translation": translation, "arabic": arabic,
                                                "lang": lang, "level": level}, server=server)
    assert status == 200, d
    return d


def _referred_check():
    """A check with at least one referred finding. Quoting a verse in the Arabic
    and leaving it out of the translation is the most stable way to get one."""
    attempts = [
        ("A short note on purpose, written in the author's own words and quoting nothing at all.",
         SAMPLE_AR),
        ('He said: "And We sent down iron with a strange power that nobody can explain to anyone." '
         "(Quran 57:25).", ""),
    ]
    for translation, arabic in attempts:
        d = _check(translation, arabic)
        if any(f["refer"] for f in d["findings"]):
            return d, translation
    raise AssertionError("no input produced a referred finding: %r" % d)


_APP = []


def _app():
    """app.py imported in-process, with its own throwaway review database."""
    if not _APP:
        os.environ["MIZAN_REVIEW_DB"] = os.path.join(
            tempfile.mkdtemp(prefix="mizan-inproc-"), "review.sqlite")
        sys.path.insert(0, ROOT)
        import app  # noqa: WPS433
        _APP.append(app)
    return _APP[0]


# ======================================================================
# Basics
# ======================================================================


def test_health_reports_index_features_and_limits():
    status, h, _ = _json("GET", "/api/health")
    assert status == 200 and h["status"] == "ok"
    assert h["source"] == "quranpedia.net" and h["index_version"]
    assert h["n_translations"] >= 1 and h["n_languages"] >= 1
    assert set(h["features"]) >= {"clarity", "terms", "review"}
    assert [lvl["code"] for lvl in h["levels"]] == ["curious", "new_muslim", "practising", "daee"]
    assert all(_is_arabic(lvl["label_ar"]) for lvl in h["levels"])
    assert h["limits"]["max_body_bytes"] > 0 and _is_arabic(h["privacy"])
    status, _, raw = _req("HEAD", "/api/health")
    assert status == 200 and raw == b""


def test_languages_exclude_the_arabic_source():
    status, d, _ = _json("GET", "/api/languages")
    assert status == 200
    codes = [l["code"] for l in d["languages"]]
    assert "en" in codes and "ar" not in codes
    for l in d["languages"]:
        assert l["n_translations"] == len(l["translations"]) and _is_arabic(l["name_ar"])


def test_pages_and_assets_are_served_with_a_csp():
    for path, ctype in (("/", "text/html"), ("/review", "text/html"), ("/style.css", "text/css"),
                        ("/common.js", "text/javascript"), ("/app.js", "text/javascript"),
                        ("/review.js", "text/javascript"), ("/report.css", "text/css")):
        status, headers, body = _req("GET", path)
        assert status == 200, path
        assert headers["Content-Type"].startswith(ctype), path
        assert headers["X-Content-Type-Options"] == "nosniff"
        if ctype == "text/html":
            assert "script-src 'self'" in headers["Content-Security-Policy"]
            assert "لا يُفتي · لا يترجم · يُسنِد فقط".encode() in body
    for path in ("/../app.py", "/%2e%2e/app.py", "/nope.html"):
        assert _req("GET", path)[0] == 404


# ======================================================================
# Panel 1 — attribution
# ======================================================================


def test_check_returns_the_full_contract():
    d = _check()
    assert re.fullmatch(r"[0-9a-f]{16}", d["check_id"])
    assert d["document_verdict"] in ("REFER", "ATTRIBUTED", "ATTRIBUTED_WITH_EDITS",
                                     "NO_APPROVED_TRANSLATION", "NO_QUOTES")
    assert d["pipeline"] in ("full", "fallback")
    assert d["source"] == "quranpedia.net" and d["index_version"]
    assert d["links"]["report_html"] == "/report/%s.html" % d["check_id"]
    assert d["findings"], "the 51:56 sample produced no finding at all"
    for f in d["findings"]:
        for key in ("idx", "state", "state_label", "refer", "tier_label", "ai_tier",
                    "published_text", "approved_text", "attributed", "closest", "runners_up"):
            assert key in f, key
        assert _is_arabic(f["state_label"]) and _is_arabic(f["tier_label"])
    # One referred item pulls the whole document; nothing else does.
    assert (d["document_verdict"] == "REFER") == any(f["refer"] for f in d["findings"])
    assert d["gate"] == ("REFER" if d["document_verdict"] == "REFER" else "CLEAR")
    # AI disclosure appears exactly when a finding came from a model tier.
    assert d["ai_assisted"] == any(f["ai_tier"] for f in d["findings"])
    assert bool(d["ai_disclosure"]) == d["ai_assisted"]


def test_source_links_follow_the_verified_quranpedia_pattern():
    d = _check()
    for f in d["findings"]:
        if f["verse_url"]:
            assert f["verse_url"] == "https://quranpedia.net/surah/1/%d/%d" % (f["surah"], f["ayah"])
        for book in (f["attributed"], f["closest"]):
            if book and book.get("source_url"):
                assert re.fullmatch(r"https://quranpedia\.net/surah/1/%d/book/%d#verse-\d+"
                                    % (f["surah"], book["book_id"]), book["source_url"])
    app = _app()
    # Anchors checked against the live site on 2026-10-03.
    assert app.global_verse_id(51, 56) == 4731
    assert app.global_verse_id(2, 255) == 262
    assert app.global_verse_id(3, 59) == 352
    assert app.global_verse_id(112, 1) == 6222
    assert app.translation_url(13638, 51, 56) == \
        "https://quranpedia.net/surah/1/51/book/13638#verse-4731"


def test_the_full_document_is_not_stored():
    d = _check()
    con = sqlite3.connect(STATE["main"]["db"])
    row = con.execute("SELECT payload, text_sha256 FROM checks WHERE id = ?",
                      (d["check_id"],)).fetchone()
    con.close()
    payload, digest = row
    assert PROSE_ONLY_SENTENCE not in payload, "the author's prose was persisted"
    assert "خلقت" not in payload and "الذاريات" not in payload, "the Arabic source was persisted"
    assert "clarity" not in json.loads(payload)
    import hashlib
    assert digest == hashlib.sha256(SAMPLE_EN.encode("utf-8")).hexdigest()


def test_input_without_quotations_is_no_quotes():
    d = _check("Patience is a quality admired across many cultures and traditions of the world.", "")
    if not d["findings"]:
        assert d["document_verdict"] == "NO_QUOTES" and d["gate"] == "CLEAR"


# ======================================================================
# Panel 3 — clarity
# ======================================================================


def test_clarity_freezes_every_located_quotation():
    d = _check()
    c = d["clarity"]
    if c is None:
        assert not _json("GET", "/api/health")[1]["features"]["clarity"]
        return
    assert "error" not in c, c
    assert c["level"] == "practising" and c["frozen"]["verified_byte_exact"] is True
    output = " ".join(s["output"] for s in c["sentences"])
    words = SAMPLE_EN.split()
    for f in d["findings"]:
        if isinstance(f["start_word"], int) and isinstance(f["end_word"], int):
            span = " ".join(words[f["start_word"]:f["end_word"]])
            assert span in output, "quotation not preserved byte-exact: %r" % span
            quoted = [s for s in c["sentences"] if span in s["original"] or s["original"] in span]
            assert quoted and all(s["kind"] == "QUOTED" for s in quoted)


def test_clarity_level_switch_reuses_the_saved_check():
    d = _check()
    if d["clarity"] is None:
        return
    refused = None
    for level in ("curious", "new_muslim", "practising", "daee"):
        status, r, _ = _json("POST", "/api/clarity", {"check_id": d["check_id"],
                                                      "translation": SAMPLE_EN, "level": level})
        assert status == 200, r
        c = r["clarity"]
        assert c["level"] == level and c["frozen"]["verified_byte_exact"] is True
        now = {s["index"] for s in c["sentences"] if s["disposition"] == "REFUSED"}
        refused = now if refused is None else refused
        assert now == refused, "an audience level changed which sentences were refused"
    status, r, _ = _json("POST", "/api/clarity", {"check_id": d["check_id"],
                                                  "translation": SAMPLE_EN + " Edited.",
                                                  "level": "daee"})
    assert status == 409 and r["code"] == "TEXT_CHANGED" and _is_arabic(r["error"])
    status, r, _ = _json("POST", "/api/clarity", {"check_id": "0" * 16,
                                                  "translation": SAMPLE_EN, "level": "daee"})
    assert status == 404 and r["code"] == "CHECK_NOT_FOUND"


# ======================================================================
# Panel 2 — terminology (defensive wiring)
# ======================================================================


def test_terminology_is_absent_or_honours_the_contract():
    feats = _json("GET", "/api/health")[1]["features"]
    d = _check()
    if not feats["terms"]:
        assert d["terms"] is None, "terms section must not exist when the module is absent"
        return
    t = d["terms"]
    assert t["available"] is True
    for row in t.get("findings") or []:
        assert set(row) == {"term_ar", "approved_en", "status", "found", "suggestion", "source_url"}
        assert row["status"] in ("APPROVED", "VARIANT", "MISSING", "NOT_IN_GLOSSARY")


def test_terminology_panel_renders_when_a_module_is_present():
    assert _json("GET", "/api/health", server="stub")[1]["features"]["terms"] is True
    d = _check(server="stub")
    t = d["terms"]
    assert t["available"] and len(t["findings"]) == 2
    assert t["counts"] == {"VARIANT": 1, "MISSING": 1}
    status, _, raw = _req("GET", "/report/%s.html" % d["check_id"], server="stub")
    html = raw.decode("utf-8")
    assert status == 200 and "المصطلحات" in html and "الشريعة" in html and "صيغة بديلة" in html


def test_terminology_failure_never_breaks_the_check():
    d = _check(SAMPLE_EN + " RAISE_TERMS", server="stub")
    assert d["terms"]["error"] and _is_arabic(d["terms"]["error"])
    assert d["findings"] is not None and d["check_id"]


# ======================================================================
# Review queue and audit log
# ======================================================================


def test_review_flow_end_to_end():
    d, _ = _referred_check()
    cid = d["check_id"]
    target = next(f for f in d["findings"] if f["refer"])

    status, r, _ = _json("POST", "/api/review", {"check_id": cid, "finding_idx": target["idx"]})
    assert status == 201, r
    item = r["items"][0]
    assert item["status"] == "pending" and item["is_open"] and item["report_url"].endswith(cid + ".html")
    status, r, _ = _json("POST", "/api/review", {"check_id": cid, "finding_idx": target["idx"]})
    assert status == 200 and r["items"][0]["id"] == item["id"], "enqueue must be idempotent"

    clear = [f for f in d["findings"] if not f["refer"]]
    if clear:
        status, r, _ = _json("POST", "/api/review", {"check_id": cid, "finding_idx": clear[0]["idx"]})
        assert status == 400 and r["code"] == "NOT_REVIEWABLE"

    status, q, _ = _json("GET", "/api/review?status=open")
    assert status == 200 and item["id"] in [i["id"] for i in q["items"]]

    path = "/api/review/%d/decision" % item["id"]
    status, r, _ = _json("POST", path, {"decision": "escalate", "reviewer": "مراجع أ",
                                        "note": "يحتاج نظر عالم"})
    assert status == 201 and r["status"] == "escalated" and r["is_open"]

    status, r, _ = _json("POST", path, {"decision": "approve_as_published", "reviewer": " "})
    assert status == 400 and r["code"] == "REVIEWER_REQUIRED" and _is_arabic(r["error"])
    status, r, _ = _json("POST", path, {"decision": "rewrite_it", "reviewer": "R"})
    assert status == 400 and r["code"] == "BAD_DECISION"

    if target["surah"]:
        status, v, _ = _json("GET", "/api/verse?ref=%s&lang=en" % target["ref"])
        assert status == 200 and v["renderings"]
        book = v["renderings"][0]
        status, r, _ = _json("POST", path, {"decision": "replace_with_approved",
                                            "reviewer": "الشيخ ب", "note": "",
                                            "replacement_book_id": book["book_id"]})
        assert status == 201, r
        assert r["status"] == "replaced_with_approved" and not r["is_open"]
        assert r["latest"]["replacement"]["text"] == book["text"], "replacement must be verbatim"
        status, r, _ = _json("POST", path, {"decision": "replace_with_approved", "reviewer": "R"})
        assert status == 400 and r["code"] == "REPLACEMENT_REQUIRED"
        n_events = 2
    else:
        status, r, _ = _json("POST", path, {"decision": "approve_as_published", "reviewer": "الشيخ ب"})
        assert status == 201
        n_events = 2

    status, it, _ = _json("GET", "/api/review/%d" % item["id"])
    assert status == 200 and len(it["events"]) == n_events
    assert it["events"][0]["decision"] == "escalate" and it["events"][0]["reviewer"] == "مراجع أ"

    status, log, _ = _json("GET", "/api/review/log")
    mine = [e for e in log["events"] if e["item_id"] == item["id"]]
    assert log["append_only"] and len(mine) == n_events
    assert mine[0]["id"] > mine[-1]["id"], "log must be newest first"

    status, closed, _ = _json("GET", "/api/review?status=closed")
    assert item["id"] in [i["id"] for i in closed["items"]]

    html = _req("GET", "/report/%s.html" % cid)[2].decode("utf-8")
    assert "بند مراجعة #%d" % item["id"] in html and "مراجع أ" in html

    status, exp, _ = _json("GET", "/api/check/%s/export.json" % cid)
    assert status == 200
    assert [i["id"] for i in exp["review"]] == [item["id"]]
    assert len(exp["review"][0]["events"]) == n_events

    status, r, _ = _json("GET", "/api/review/999999")
    assert status == 404 and r["code"] == "ITEM_NOT_FOUND"


def test_send_several_findings_at_once():
    d, _ = _referred_check()
    ids = [f["idx"] for f in d["findings"] if f["refer"]]
    status, r, _ = _json("POST", "/api/review", {"check_id": d["check_id"], "finding_ids": ids})
    assert status == 201 and [i["finding_idx"] for i in r["items"]] == ids
    status, r, _ = _json("POST", "/api/review", {"check_id": d["check_id"], "finding_ids": "x"})
    assert status == 400 and r["code"] == "BAD_FINDING"


# ======================================================================
# Report and export
# ======================================================================


def test_report_html_is_self_contained_and_traceable():
    d = _check()
    status, headers, raw = _req("GET", d["links"]["report_html"])
    html = raw.decode("utf-8")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert '<html lang="ar" dir="rtl">' in html
    assert "<script" not in html.lower(), "the report must carry no script"
    assert not re.search(r"""(?:src|href)=["']?(?:https?:)?//(?!quranpedia\.net|tanzil\.net)""", html), \
        "the report may only link to its two data sources (Quranpedia, Tanzil — both licences require the link)"
    assert "<link" not in html.lower() and "@import" not in html
    assert d["check_id"] in html and d["index_version"] in html
    assert "https://quranpedia.net" in html and "لا يُفتي · لا يترجم · يُسنِد فقط" in html
    for f in d["findings"]:
        assert (f["ref"] or "؟") in html
        if f["attributed"]:
            assert f["attributed"]["title"] in html and f["attributed"]["source_url"] in html
    status, headers, _ = _req("GET", d["links"]["report_download"])
    assert status == 200 and "attachment" in headers["Content-Disposition"]
    status, _, raw = _req("GET", "/report/0000000000000000.html")
    assert status == 404 and _is_arabic(json.loads(raw)["error"])


def test_json_export_carries_attribution_and_every_finding():
    d = _check()
    status, headers, raw = _req("GET", d["links"]["export_json"])
    exp = json.loads(raw)
    assert status == 200 and "attachment" in headers["Content-Disposition"]
    assert exp["schema"] == "mizan.check/1" and exp["check_id"] == d["check_id"]
    assert exp["data_attribution"]["url"] == "https://quranpedia.net"
    assert exp["data_attribution"]["dump_version"] == d["index_version"]
    assert exp["created_at"].endswith("Z") and exp["exported_at"].endswith("Z")
    assert len(exp["findings"]) == len(d["findings"])
    assert re.fullmatch(r"[0-9a-f]{64}", exp["translation_sha256"])
    assert "text_sha256" not in exp
    status, plain, _ = _json("GET", "/api/check/%s" % d["check_id"])
    assert status == 200 and plain["check_id"] == d["check_id"]


def test_verse_endpoint_returns_verbatim_renderings():
    status, v, _ = _json("GET", "/api/verse?ref=51:56&lang=en")
    assert status == 200 and v["ref"] == "51:56" and v["renderings"]
    for r in v["renderings"]:
        assert r["text"] and r["source_url"].endswith("#verse-4731")
    status, r, _ = _json("GET", "/api/verse?ref=999:1&lang=en")
    assert status == 400 and r["code"] == "BAD_REF" and _is_arabic(r["error"])


# ======================================================================
# Robustness — limits, timeouts, Arabic errors that say what to do
# ======================================================================


def _expect(status, payload, code, want_status):
    assert status == want_status, (status, payload)
    assert payload["code"] == code, payload
    assert _is_arabic(payload["error"]), payload


def test_input_errors_are_arabic_and_actionable():
    _expect(*_json("POST", "/api/check", {"translation": "   ", "lang": "en"})[:2],
            "TRANSLATION_REQUIRED", 400)
    _expect(*_json("POST", "/api/check", {"translation": "word " * 10001})[:2],
            "TRANSLATION_TOO_LONG", 413)
    _expect(*_json("POST", "/api/check", {"translation": "abc def ghi", "arabic": "ب" * 50001})[:2],
            "ARABIC_TOO_LONG", 413)
    _expect(*_json("POST", "/api/check", {"translation": "abc def ghi", "level": "expert"})[:2],
            "BAD_LEVEL", 400)
    _expect(*_json("POST", "/api/check", {"translation": "abc def ghi", "lang": "EN-us!"})[:2],
            "BAD_LANG", 400)
    _expect(*_json("POST", "/api/check", {"translation": 42})[:2], "BAD_FIELD", 400)
    _expect(*_json("POST", "/api/check", raw=b"{not json")[:2], "BAD_JSON", 400)
    _expect(*_json("POST", "/api/check", raw=b"[1, 2]")[:2], "BAD_JSON", 400)
    _expect(*_json("POST", "/api/check", raw=b"[" * 100000)[:2], "BAD_JSON", 400)
    _expect(*_json("POST", "/api/check", {"translation": "abc def ghi", "lang": "fr"})[:2],
            "LANG_NOT_INDEXED", 400)
    _expect(*_json("POST", "/api/check", raw=b'{"translation": "x"}', ctype="text/plain")[:2],
            "UNSUPPORTED_MEDIA_TYPE", 415)
    _expect(*_json("GET", "/api/check")[:2], "METHOD_NOT_ALLOWED", 405)
    _expect(*_json("GET", "/api/nothing-here")[:2], "NOT_FOUND", 404)
    _expect(*_json("GET", "/api/check/not-an-id")[:2], "CHECK_NOT_FOUND", 404)


def test_oversized_body_is_refused_without_being_read():
    conn = http.client.HTTPConnection("127.0.0.1", STATE["main"]["port"], timeout=10)
    conn.putrequest("POST", "/api/check")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", str(5 * 1024 * 1024))
    conn.endheaders()                      # the 5 MB body is never sent
    resp = conn.getresponse()
    payload = json.loads(resp.read())
    conn.close()
    _expect(resp.status, payload, "BODY_TOO_LARGE", 413)


def test_chunked_bodies_are_refused():
    conn = http.client.HTTPConnection("127.0.0.1", STATE["main"]["port"], timeout=10)
    conn.request("POST", "/api/check", body=iter([b'{"translation": "abc"}']),
                 headers={"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
                 encode_chunked=True)
    resp = conn.getresponse()
    payload = json.loads(resp.read())
    conn.close()
    _expect(resp.status, payload, "LENGTH_REQUIRED", 411)


def test_cross_site_requests_are_refused():
    _expect(*_json("POST", "/api/check", {"translation": "abc def ghi"},
                   headers={"Origin": "http://evil.example"})[:2], "FOREIGN_ORIGIN", 403)
    _expect(*_json("GET", "/api/review/log", headers={"Host": "evil.example"})[:2], "BAD_HOST", 403)
    port = STATE["main"]["port"]
    status, _, _ = _json("POST", "/api/check", {"translation": "abc def ghi jkl"},
                         headers={"Origin": "http://127.0.0.1:%d" % port})
    assert status == 200, "same-origin POST must be accepted"


def test_engine_work_has_a_hard_timeout():
    app = _app()
    started = time.time()
    try:
        app.on_engine(time.sleep, 0.5, timeout=0.05)
        raise AssertionError("no timeout")
    except app.ApiError as e:
        assert e.status == 504 and e.code == "CHECK_TIMEOUT" and _is_arabic(e.message)
    assert time.time() - started < 0.4
    app.on_engine(lambda: None, timeout=5)   # the worker recovers


# ======================================================================
# Transparency — how each finding was found, and when a model was involved
# ======================================================================


def test_semantic_findings_carry_the_ai_disclosure():
    app = _app()
    original = app._attribute

    def fake_attribute(translation, arabic, lang):
        return ([{"surah": 51, "ayah": 56, "state": "UNATTRIBUTED", "score": 0.6,
                  "published_text": "a re-worded rendering", "tier": "semantic",
                  "start_word": 0, "end_word": 3}], "REFER", "full", None)

    app._attribute = fake_attribute
    try:
        d = app.run_check("a re-worded rendering of a verse", "", "en", "practising")
    finally:
        app._attribute = original
    f = d["findings"][0]
    assert f["ai_tier"] is True and "ذكاء اصطناعي" in f["tier_label"]
    assert d["ai_assisted"] is True and d["ai_disclosure"] == app.AI_DISCLOSURE
    html = app.render_report_html(app.STORE.get_check(d["check_id"]), [])
    assert "إفصاح" in html and "ذكاء اصطناعي" in html


def test_each_tier_has_a_distinct_arabic_label():
    app = _app()
    labels = {t: app._tier_info(t) for t in ("reference", "lexical", "semantic", None, "semantic+rerank")}
    assert labels["reference"][0] == "مرجع مطبوع" and not labels["reference"][2]
    assert labels["lexical"][0] == "مطابقة نصية" and not labels["lexical"][2]
    assert labels["semantic"][2] and labels["semantic+rerank"][2]
    assert len({labels[t][0] for t in ("reference", "lexical", "semantic", None)}) == 4


def test_an_unknown_state_can_only_make_the_gate_stricter():
    app = _app()
    f = app.canonical_finding(0, {"surah": 1, "ayah": 1, "state": "CANDIDATE"}, "en")
    assert f["refer"] is True
    assert app.document_verdict([f], None) == "REFER"
    ok = app.canonical_finding(0, {"surah": 1, "ayah": 1, "state": "MATCH"}, "en")
    assert app.document_verdict([ok], "REFER") == "REFER", "the report layer's REFER must win"
    assert app.document_verdict([ok], "CLEAR") == "ATTRIBUTED"


def test_a_partial_ai_pass_is_disclosed():
    """A text longer than the AI tier's token budget is still checked in full
    by the deterministic tiers; the response must say the AI saw only part."""
    app = _app()
    original_attr, original_det = app._attribute, app._detector_for

    class PartialDetector:
        last_semantic = {"used": True, "complete": False, "dropped": [{}]}

    def fake_attribute(translation, arabic, lang):
        return ([], "CLEAR", "full", None)

    app._attribute, app._detector_for = fake_attribute, lambda lang: PartialDetector()
    try:
        d = app.run_check("a long text", "", "en", "practising")
        PartialDetector.last_semantic = {"used": True, "complete": True, "dropped": []}
        full = app.run_check("a short text", "", "en", "practising")
        PartialDetector.last_semantic = {"used": False, "reason": "disabled or unavailable"}
        off = app.run_check("a short text", "", "en", "practising")
    finally:
        app._attribute, app._detector_for = original_attr, original_det
    assert d["ai_tier_complete"] is False
    assert d["pipeline_note"] == app.AI_TIER_PARTIAL_NOTE and _is_arabic(d["pipeline_note"])
    assert full["ai_tier_complete"] is True and full["pipeline_note"] is None
    assert off["ai_tier_complete"] is None and off["pipeline_note"] is None


def test_a_changed_word_refers_and_says_why():
    """Red-team, 2026-10-03: one changed word used to come out MATCH."""
    app = _app()
    si = next(b for b, v in app.ENGINE.books.items() if "Sahih International" in v["title"])
    v = app.ENGINE.verse(si, 4, 48)
    d = app.run_check('"%s" (Quran 4:48)' % v.replace("does not forgive", "does forgive", 1),
                      "", "en", "practising")
    f = d["findings"][0]
    assert d["document_verdict"] == "REFER" and f["state"] == "NEAR" and f["refer"]
    assert f["attributed"] is None and f["closest"], "a NEAR names its closest text, not a source"
    assert any(op["op"] != "equal" for op in f["diff"])
    d = app.run_check('"%s" (Quran 4:49)' % v, "", "en", "practising")
    f = d["findings"][0]
    assert d["document_verdict"] == "REFER" and f["state"] == "MATCH"
    assert f["flags"] == ["citation_mismatch"] and _is_arabic(f["flag_messages"][0])
    html = app.render_report_html(app.STORE.get_check(d["check_id"]), [])
    assert "سبب الإحالة" in html


def test_fallback_path_is_labelled_as_degraded():
    app = _app()
    original = app._optional

    def no_report(name):
        return None if name == "mizan.report" else original(name)

    app._optional = no_report
    try:
        d = app.run_check(SAMPLE_EN, "", "en", "practising")
    finally:
        app._optional = original
    assert d["pipeline"] == "fallback" and _is_arabic(d["pipeline_note"])
    refs = [f["ref"] for f in d["findings"]]
    assert "51:56" in refs, refs
    f = d["findings"][refs.index("51:56")]
    assert f["state"] in ("MATCH", "NEAR", "UNATTRIBUTED") and f["tier"] == "reference"


# ======================================================================
# Plain-python runner
# ======================================================================

def _main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = []
    print("starting two servers on random ports…")
    setup_module()
    try:
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
    finally:
        teardown_module()
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
