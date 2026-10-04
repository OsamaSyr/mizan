#!/usr/bin/env python3
"""
MIZAN — tests and measurement for the semantic tier (tier c, bge-m3).

Two uses:

    # tests — skip cleanly when the ML extras are not installed
    python3 -m pytest tests/test_semantic.py -v            # deterministic: skips
    .venv/bin/python -m pytest tests/test_semantic.py -v   # semantic: runs

    # the held-out "unknown translation" measurement -> tests/results_semantic.json
    .venv/bin/python tests/test_semantic.py --measure

What the measurement asks
-------------------------
Can MIZAN locate a Quranic quotation taken from a published translation that is
NOT in its approved index, identify the right verse, and REFER it (NEAR or
UNATTRIBUTED) rather than falsely MATCH it to some approved edition — and what
does the semantic tier add over the deterministic one on exactly the same
items? Held-out editions are verified absent from corpus.sqlite by title AND by
text before use, and every item whose rendering is literally present in the
index (sim >= T_MATCH against an approved rendering of that verse) is excluded
and counted, because "unknown" text that is in fact indexed would flatter the
result.

Every threshold used by the detector was set on a DEV split (other editions,
other verses, other negative paragraphs). Nothing here was tuned on these
items. See docs/SEMANTIC.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from mizan import semantic                                   # noqa: E402
from mizan.detect import SpanDetector, semantic_candidates   # noqa: E402
from mizan.engine import T_MATCH, Mizan, sim                 # noqa: E402
from mizan.report import (MATCH, NEAR, UNATTRIBUTED,          # noqa: E402
                          build_finding, check_document)

try:
    import pytest
except ImportError:                                          # pragma: no cover
    pytest = None

AVAILABLE = semantic.available()


def _mark_needs_model(f):
    """Without pytest: mark the test so the plain runner below skips it."""
    f.pytestmark = ["needs_model"]
    return f


needs_model = (pytest.mark.skipif(not AVAILABLE, reason=(
    "semantic tier unavailable (pip install -r requirements-ml.txt, "
    "scripts/build_embeddings.py --download, scripts/build_embeddings.py)"))
    if pytest else _mark_needs_model)

_M: list[Mizan] = []


def M() -> Mizan:
    if not _M:
        _M.append(Mizan())
    return _M[0]


# ======================================================================
# Always-run tests (no ML needed)
# ======================================================================

def test_availability_check_imports_nothing_heavy():
    """`available()` must answer without importing torch — it runs on every
    request in deterministic deployments."""
    before = set(sys.modules)
    semantic.available()
    semantic.status()
    assert "torch" not in (set(sys.modules) - before)


def test_forced_off_raises_not_guesses():
    """With the tier switched off, the contract function RAISES — it never
    returns an empty list that would read as 'nothing resembles a verse'."""
    old = os.environ.get("MIZAN_SEMANTIC")
    os.environ["MIZAN_SEMANTIC"] = "0"
    try:
        try:
            semantic_candidates("Allah, there is no deity except Him", "en")
        except semantic.SemanticUnavailable:
            pass
        else:
            raise AssertionError("must raise SemanticUnavailable when off")
    finally:
        if old is None:
            os.environ.pop("MIZAN_SEMANTIC", None)
        else:
            os.environ["MIZAN_SEMANTIC"] = old


def test_clean_rendering_drops_footnotes():
    """Footnote text after the separator is commentary, not verse."""
    raw = "(2) This is the Book[8] - ____________________ [8]- Literally, taqwa"
    assert semantic.clean_rendering(raw) == "This is the Book -"


def test_clean_arabic_strips_prefixed_basmala():
    t = semantic.clean_arabic("بسم الله الرحمن الرحيم قل هو الله أحد", 112, 1)
    assert t == "قل هو الله أحد"
    # Al-Fatiha's first verse IS the basmala and must be kept.
    assert semantic.clean_arabic("بسم الله الرحمن الرحيم", 1, 1) == "بسم الله الرحمن الرحيم"


def test_failed_model_load_backs_off():
    """
    A failed load is remembered: the next calls raise at once instead of
    re-reading 2.2 GB on every check (measured +4 s and 4.3 GB peaks per check
    before this existed). status() reports it.
    """
    calls = []

    class Boom:
        def __init__(self, *a, **k):
            calls.append(1)
            raise OSError("simulated: cannot allocate memory")

    saved = (semantic.Encoder, semantic.deps_available, semantic._ENCODER,
             semantic._LOAD_FAILURE)
    semantic.Encoder, semantic.deps_available = Boom, (lambda: True)
    semantic._ENCODER, semantic._LOAD_FAILURE = None, None
    try:
        for _ in range(3):
            try:
                semantic.encoder()
            except semantic.SemanticUnavailable:
                pass
            else:
                raise AssertionError("encoder() must raise after a failed load")
        assert len(calls) == 1, f"model load retried {len(calls)} times"
        st = semantic.status()["load_failure"]
        assert st and st["attempts"] == 1 and st["retry_in_s"] > 0
        assert "cannot allocate memory" in st["error"]
    finally:
        (semantic.Encoder, semantic.deps_available, semantic._ENCODER,
         semantic._LOAD_FAILURE) = saved


def test_verse_likeness_gate_separates_scripture_from_prose():
    """The model-free gate that decides what tier (c) embeds."""
    import mizan.detect as D
    det = SpanDetector(M(), "en", semantic=False)
    book = next(b for b in M().editions("en") if "Pickthall" in M().books[b]["title"])
    verse = M().verse(book, 49, 13)
    assert det.scripture_likeness(verse) >= 0.9
    prose = ("The committee met on Tuesday to review the annual budget and agreed "
             "to postpone a decision until the next quarter.")
    assert det.scripture_likeness(prose) < D.SEM_GATE_MIN


def test_semantic_plan_order_and_budget():
    """
    The embedding plan, with a stub in place of the model (no ML needed):

    * text the deterministic tiers already explained is never embedded;
    * within the budget EVERYTHING else is embedded (the full semantic pass);
    * verse-like sentences go first, so when the budget binds it is ordinary
      prose that is dropped — and every drop is listed, never silent.
    """
    import mizan.detect as D
    det = SpanDetector(M(), "en", semantic=True)
    book = next(b for b in M().editions("en") if "Sahih" in M().books[b]["title"])
    quoted = M().verse(book, 51, 56)
    fresh = ("O humanity, We formed you from one male and one female and arranged "
             "you into peoples and tribes so that you might come to know one another.")
    doc = (f"The committee met on Tuesday to review the annual budget. {quoted} "
           f"Members discussed the cost of the new building at length. {fresh}")
    words = doc.split()
    embedded: list[str] = []

    def fake_batch(texts, lang, top_k=20):
        embedded.extend(texts)
        return [[(49, 13, 0.9), (1, 1, 0.1)] if "tribes" in t else [(1, 1, 0.5)]
                for t in texts]

    saved = (semantic.candidates_batch, semantic.count_tokens, D.semantic_available,
             D.SEM_TOKEN_BUDGET)
    semantic.candidates_batch = fake_batch
    semantic.count_tokens = lambda texts: [len(t.split()) for t in texts]
    D.semantic_available = lambda lang=None: True
    try:
        found = det._from_references(doc, words) + det._from_windows(words, D.SPAN_THRESHOLD, None)
        assert any(sp.ref == "51:56" for sp in found), "premise: 51:56 found lexically"
        _, plan = det._semantic_plan(doc, words, found, D.SPAN_THRESHOLD)
        assert plan["complete"] and plan["dropped"] == []
        assert plan["explained_by_cheaper_tiers"] >= 1
        assert not any(quoted.split()[2] in t and "committee" not in t and "Members" not in t
                       for t in embedded), "explained text must not be embedded"
        assert any("committee" in t for t in embedded), "within budget, prose is embedded too"

        D.SEM_TOKEN_BUDGET = 30             # room for the 27-word quotation, not the prose
        embedded.clear()
        _, plan = det._semantic_plan(doc, words, found, D.SPAN_THRESHOLD)
        assert any("tribes" in t for t in embedded), "the verse-like sentence goes first"
        assert not any("committee" in t for t in embedded)
        assert not plan["complete"] and plan["dropped"]
        assert all({"start_word", "end_word", "stage", "priority", "tokens"} <= set(d)
                   for d in plan["dropped"])
        assert plan["tokens_embedded"] <= 30
    finally:
        (semantic.candidates_batch, semantic.count_tokens, D.semantic_available,
         D.SEM_TOKEN_BUDGET) = saved


def test_heldout_manifest_matches_the_measurement():
    """The committed manifest lists every edition the measurement reads, and
    the fetch script and this test agree on the test split."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fetch_heldout", os.path.join(ROOT, "scripts", "fetch_heldout.py"))
    fh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fh)
    ours = sorted(ed for eds in TEST_EDITIONS.values() for ed in eds)
    assert sorted(fh.TEST_EDITIONS) == ours
    man = os.path.join(ROOT, "data", "heldout", "manifest.json")
    assert os.path.exists(man), "data/heldout/manifest.json is committed"
    with open(man, encoding="utf-8") as f:
        eds = json.load(f)["editions"]
    assert set(fh.EDITIONS) <= set(eds)
    assert all(len(v["sha256_file"]) == 64 and v["verses"] == 6236 for v in eds.values())


# ======================================================================
# Model tests (skip without the ML extras)
# ======================================================================

@needs_model
def test_budgeted_plan_finds_what_embedding_everything_finds():
    """
    The selective plan is a latency change, not a behaviour change: on the
    AI probe and the demo samples it reports exactly the spans that embedding
    every sentence and pair reports.
    """
    import mizan.detect as D
    docs = [
        ("Many readers find comfort in one passage above all. O humanity, We formed "
         "you from one male and one female and arranged you into peoples and tribes "
         "so that you might come to know one another; the noblest of you before God "
         "is the one most conscious of Him. It is recited at many weddings."),
        ('Then He clarified that creation was not left purposeless, saying: "And I '
         'did not create the jinn and mankind except to serve Me." (Quran 51:56). '
         'This verse is a foundation for understanding the purpose of human existence.'),
    ]
    det = SpanDetector(M(), "en", semantic=True)
    old = D.SEM_SELECT
    try:
        for doc in docs:
            D.SEM_SELECT = "all"
            a = [s.as_dict() for s in det.find(doc)]
            D.SEM_SELECT = "budgeted"
            b = [s.as_dict() for s in det.find(doc)]
            assert a == b, (a, b)
            assert det.last_semantic["used"] and det.last_semantic["plan"] == "budgeted"
            assert det.last_semantic["complete"], "a short text gets the full pass"
    finally:
        D.SEM_SELECT = old

@needs_model
def test_contract_shape_ids_and_scores_only():
    """Up to 20 (surah, ayah, score) tuples, sorted, valid, and no text."""
    out = semantic_candidates("Say: He is Allah, the One and Only", "en")
    assert 0 < len(out) <= 20
    for s, a, sc in out:
        assert isinstance(s, int) and isinstance(a, int) and isinstance(sc, float)
        assert 1 <= s <= 114 and a >= 1 and 0.0 <= sc <= 1.0
        assert M().verse(M().editions("en")[0], s, a) is not None
    assert [x[2] for x in out] == sorted((x[2] for x in out), reverse=True)


@needs_model
def test_deterministic():
    q = "Neither drowsiness nor sleep overtakes Him"
    assert semantic_candidates(q, "en") == semantic_candidates(q, "en")


@needs_model
def test_fresh_paraphrase_is_retrieved():
    """The failure the tier exists for: a rendering written from scratch."""
    fresh = ("God — there is no deity except Him, the Ever-Living, the One who "
             "upholds all existence. Neither drowsiness nor sleep ever overtakes "
             "Him. To Him belongs whatever fills the heavens and whatever fills "
             "the earth.")
    top = [(s, a) for s, a, _ in semantic_candidates(fresh, "en")[:3]]
    assert (2, 255) in top, top


@needs_model
def test_cross_language_retrieval():
    """Same index design works in Urdu and Bengali, not only English."""
    for lang in ("ur", "bn"):
        book = min(M().editions(lang),
                   key=lambda b: len((M().verse(b, 112, 1) or "x" * 999).split()))
        text = semantic.clean_rendering(M().verse(book, 112, 1))
        top = [(s, a) for s, a, _ in semantic_candidates(text, lang)[:3]]
        assert (112, 1) in top, (lang, top)


@needs_model
def test_paraphrase_is_located_and_referred_never_matched():
    """End to end: located, referred, and the model never decides MATCH."""
    fresh = ("O humanity, We formed you from one male and one female and "
             "arranged you into peoples and tribes so that you might come to "
             "know one another; the noblest of you before God is the one most "
             "conscious of Him.")
    doc = ("Many readers find comfort in one passage above all. " + fresh +
           " It is recited at many weddings.")
    lexical = check_document(M(), doc, "en", detector=SpanDetector(M(), "en", semantic=False))
    assert not [f for f in lexical.findings if f.ref == "49:13"], \
        "premise: the deterministic tiers alone do not locate this"
    rep = check_document(M(), doc, "en", detector=SpanDetector(M(), "en", semantic=True))
    hits = [f for f in rep.findings if f.ref == "49:13"]
    assert hits, [(f.ref, f.state, f.tier) for f in rep.findings]
    assert hits[0].state in (NEAR, UNATTRIBUTED)
    assert all(f.state != MATCH for f in rep.findings if f.tier == "semantic")


@needs_model
def test_index_manifest_is_complete():
    man = semantic.index_manifest()
    for key in ("model", "model_revision", "dimension", "built_at",
                "corpus_version", "design", "languages"):
        assert key in man, key
    assert man["model"] == semantic.MODEL_ID
    assert man["model_revision"] == semantic.MODEL_REVISION
    assert set(man["languages"]) == set(M().languages())
    idx = semantic.load_index("en")
    assert len(idx.refs) == 6236
    assert all(mat.shape == (6236, man["dimension"]) for _, _, mat in idx.parts)


# ======================================================================
# The held-out measurement (Task: "what does the AI add")
# ======================================================================

# Held-out editions: data/heldout/ (scripts/fetch_heldout.py downloads them;
# gitignored, only the manifest of SHA-256s is committed). The prototype copies
# the measurement was first run on are the fallback; they are byte-identical.
HELDOUT_DIR = os.path.join(ROOT, "data", "heldout")
LEGACY_HELDOUT_DIR = os.path.join(os.path.dirname(ROOT), "prototype", "data", "full")
RESULTS = os.path.join(HERE, "results_semantic.json")
# Span-level detail that quotes third-party text goes here, never into the
# committed results file (data/heldout/ is gitignored except its manifest).
DETAIL = os.path.join(HELDOUT_DIR, "results_semantic_detail.json")

# Held-out editions used for the TEST measurement. Dev used en.shakir,
# en.qaribullah, ur.jalandhry, bn.hoque, hi.hindi — never these.
TEST_EDITIONS = {
    "en": ["en.asad", "en.arberry", "en.daryabadi", "en.itani",
           "en.wahiduddin", "en.ahmedali"],
    "hi": ["hi.farooq"],
    "ur": ["ur.ahmedali", "ur.jawadi", "ur.kanzuliman"],
    "bn": ["bn.bengali"],
}
# Title fragments used to prove each held-out edition is NOT in the index.
TITLE_PROBES = {"en.asad": "asad", "en.arberry": "arberry", "en.daryabadi": "daryabadi",
                "en.itani": "itani", "en.wahiduddin": "wahiduddin",
                "en.ahmedali": "ahmed ali", "hi.farooq": "farooq",
                "ur.ahmedali": "ahmed ali", "ur.jawadi": "jawadi",
                "ur.kanzuliman": "kanz", "bn.bengali": "muhiuddin"}
PER_EDITION = {"en": 50, "hi": 60, "ur": 40, "bn": 60}
SPLIT_SEED = 20261003      # same seed as the dev/test verse split used in tuning
_SENT = re.compile(r"(?<=[.!?।۔؟])\s+")


def heldout_dir() -> str:
    """Where the held-out editions are read from (see HELDOUT_DIR)."""
    env = os.environ.get("MIZAN_HELDOUT_DIR") or os.environ.get("MIZAN_PROTOTYPE_DATA")
    if env:
        return env
    needed = [ed.replace(".", "_") + ".json" for eds in TEST_EDITIONS.values() for ed in eds]
    if all(os.path.exists(os.path.join(HELDOUT_DIR, f)) for f in needed):
        return HELDOUT_DIR
    return LEGACY_HELDOUT_DIR


def _manifest_sha() -> dict[str, str]:
    path = os.path.join(HELDOUT_DIR, "manifest.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return {e: v["sha256_file"] for e, v in json.load(fh)["editions"].items()}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _verse_split(m: Mizan) -> tuple[list, list]:
    """Seeded halves of the 6,236 verses. Tuning used the first; this uses the second."""
    refs = [(s, a) for s, a in m.con.execute(
        "SELECT surah, ayah FROM arabic_ayat ORDER BY surah, ayah")]
    rnd = random.Random(SPLIT_SEED)
    rnd.shuffle(refs)
    half = len(refs) // 2
    return refs[:half], refs[half:]


def _probe_negatives() -> list[dict]:
    """Exactly the 120 paragraphs tests/real_corpus.py probes (never used in tuning)."""
    rows = [json.loads(ln) for ln in open(os.path.join(ROOT, "data", "real", "negatives.jsonl"),
                                         encoding="utf-8") if ln.strip()]
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["lang"], []).append(r)
    picked, i = [], 0
    while len(picked) < 120 and any(i < len(v) for v in by.values()):
        for lg in sorted(by):
            if i < len(by[lg]) and len(picked) < 120:
                picked.append(by[lg][i])
        i += 1
    return picked


def build_heldout_items(m: Mizan) -> tuple[list[dict], dict]:
    """The test items, plus provenance (exclusions, title checks, file hashes)."""
    _, test_refs = _verse_split(m)
    negs = _probe_negatives()
    wrappers = {lg: [r["text"] for r in negs if r["lang"] == lg] for lg in ("en", "hi")}
    titles = [b["title"].lower() for b in m.books.values()]
    rnd = random.Random(7)
    src_dir = heldout_dir()
    manifest = _manifest_sha()
    items, prov = [], {"editions": {}, "excluded_in_index": 0,
                       "source_dir": os.path.relpath(src_dir, ROOT)}
    for lang, eds in TEST_EDITIONS.items():
        approved = m.editions(lang)
        for ed in eds:
            path = os.path.join(src_dir, ed.replace(".", "_") + ".json")
            raw = open(path, "rb").read()
            data = json.loads(raw)
            probe = TITLE_PROBES[ed]
            # Compare CONTENT with the manifest (canonical form), so the
            # legacy copies and a fresh fetch are recognised as the same text.
            canon = json.dumps(dict(sorted(data.items(), key=lambda kv: tuple(
                int(x) for x in kv[0].split(":")))), ensure_ascii=False,
                separators=(",", ":")).encode("utf-8")
            prov["editions"][ed] = {
                "sha256_content": hashlib.sha256(canon).hexdigest(),
                "matches_manifest": (manifest.get(ed) == hashlib.sha256(canon).hexdigest()
                                     if manifest else None),
                "title_probe": probe,
                "title_hits_in_index": [t for t in titles if probe in t],
                "excluded_in_index": 0, "items": 0}
            n = 0
            for ref in test_refs:
                if n >= PER_EDITION[lang]:
                    break
                text = data.get(f"{ref[0]}:{ref[1]}", "")
                if not 8 <= len(text.split()) <= 90:
                    continue
                if max((sim(text, m.verse(b, *ref) or "") for b in approved),
                       default=0.0) >= T_MATCH:
                    prov["editions"][ed]["excluded_in_index"] += 1
                    prov["excluded_in_index"] += 1
                    continue
                n += 1
                conds = [("full", text)]
                words = text.split()
                if len(words) >= 14:
                    L = rnd.randint(max(7, int(len(words) * 0.5)), int(len(words) * 0.7))
                    st = rnd.randint(0, len(words) - L)
                    conds.append(("partial", " ".join(words[st:st + L])))
                for cond, quote in conds:
                    if lang in wrappers:
                        p = rnd.choice(wrappers[lang])
                        sents = _SENT.split(p)
                        k = max(1, len(sents) // 2)
                        before, after = " ".join(sents[:k]), " ".join(sents[k:])
                        doc = f"{before} {quote} {after}"
                        qs = len(before.split())
                        setting = "in_prose"
                    else:
                        # No negative prose exists in this language; the rendering
                        # is checked on its own. Reported separately.
                        doc, qs, setting = quote, 0, "bare"
                    items.append({"lang": lang, "edition": ed, "ref": list(ref),
                                  "condition": cond, "setting": setting, "text": doc,
                                  "qs": qs, "qe": qs + len(quote.split()),
                                  "item_id": f"{ed}:{ref[0]}:{ref[1]}:{cond}"})
            prov["editions"][ed]["items"] = n
    return items, prov


def _run(m: Mizan, items: list[dict], semantic_on: bool,
         select: str | None = None) -> list[dict]:
    import mizan.detect as D
    if not select:
        return _run_inner(m, items, semantic_on)
    old_select = D.SEM_SELECT
    D.SEM_SELECT = select
    try:
        return _run_inner(m, items, semantic_on)
    finally:
        D.SEM_SELECT = old_select


def _run_inner(m: Mizan, items: list[dict], semantic_on: bool) -> list[dict]:
    dets: dict[str, SpanDetector] = {}
    out = []
    for it in items:
        lang = it["lang"]
        if lang not in dets:
            dets[lang] = SpanDetector(m, lang, semantic=semantic_on)
        t0 = time.perf_counter()
        rep = check_document(m, it["text"], lang, detector=dets[lang])
        dt = time.perf_counter() - t0
        gold = tuple(it["ref"])
        inside = [f for f in rep.findings if f.start_word is not None
                  and min(f.end_word, it["qe"]) - max(f.start_word, it["qs"]) > 0]
        hit = next((f for f in inside if (f.surah, f.ayah) == gold), None)
        ls = dets[lang].last_semantic or {}
        out.append({
            "item_id": it["item_id"], "doc_sha256": _sha(it["text"]),
            "lang": lang, "edition": it["edition"], "condition": it["condition"],
            "setting": it["setting"], "ref": f"{gold[0]}:{gold[1]}",
            "words": len(it["text"].split()),
            "tokens_embedded": ls.get("tokens_embedded"),
            "chunks_embedded": ls.get("embedded"),
            "located": hit is not None,
            "state": hit.state if hit else None,
            "tier": hit.tier if hit else None,
            "wrong_verse_only": hit is None and bool(inside),
            "false_match": any(f.state == MATCH for f in inside),
            "outside_findings": sum(1 for f in rep.findings if f not in inside),
            "semantic_used": dets[lang].last_semantic.get("used", False),
            "ms": round(dt * 1000, 1),
        })
    return out


def _summ(rows: list[dict]) -> dict:
    n = len(rows)
    loc = [r for r in rows if r["located"]]
    lat = [r["ms"] for r in rows] or [0.0]
    states = {s: sum(1 for r in loc if r["state"] == s) for s in (MATCH, NEAR, UNATTRIBUTED)}
    return {
        "n": n,
        "located_correct_verse": len(loc),
        "recall_pct": round(100 * len(loc) / n, 1) if n else 0.0,
        "wrong_verse_only": sum(r["wrong_verse_only"] for r in rows),
        "states_of_located": states,
        "referred_pct_of_located": round(100 * (states[NEAR] + states[UNATTRIBUTED])
                                         / len(loc), 1) if loc else None,
        "false_match_items": sum(r["false_match"] for r in rows),
        "located_by_tier": {t: sum(1 for r in loc if r["tier"] == t)
                            for t in sorted({r["tier"] for r in loc})},
        "outside_findings": sum(r["outside_findings"] for r in rows),
        "tokens_embedded": sum(r.get("tokens_embedded") or 0 for r in rows),
        "latency_ms": {"median": round(statistics.median(lat), 1),
                       "p95": round(sorted(lat)[int(0.95 * (len(lat) - 1))], 1),
                       "mean": round(statistics.mean(lat), 1)},
    }


def _negatives(m: Mizan, semantic_on: bool, detail_sink: list | None = None) -> dict:
    """The 120-paragraph probe. Committed output: ids, verse ids, states,
    scores and SHA-256 of each spurious span — no text. The span text goes to
    `detail_sink` (written to the gitignored DETAIL file)."""
    rows = _probe_negatives()
    dets: dict[str, SpanDetector] = {}
    dirty, fa, spans, lat, detail = 0, 0, 0, [], []
    for r in rows:
        lang = r["lang"]
        if lang not in dets:
            dets[lang] = SpanDetector(m, lang, semantic=semantic_on)
        t0 = time.perf_counter()
        found = dets[lang].find(r["text"])
        lat.append((time.perf_counter() - t0) * 1000)
        fs = [build_finding(m, s.text, s.surah, s.ayah, lang, tier=s.tier) for s in found]
        dirty += bool(fs)
        spans += len(fs)
        # Only MATCH attributes; NEAR refers (since 2026-10-04).
        fa += sum(1 for f in fs if f.state == MATCH)
        for f in fs:
            detail.append({"id": r["id"], "lang": lang, "ref": f.ref, "state": f.state,
                           "tier": f.tier, "score": f.score,
                           "span_words": len(f.published_text.split()),
                           "span_sha256": _sha(f.published_text)})
            if detail_sink is not None:
                detail_sink.append({"id": r["id"], "ref": f.ref, "tier": f.tier,
                                    "semantic_on": semantic_on,
                                    "span": f.published_text})
    return {"paragraphs": len(rows), "paragraphs_with_a_finding": dirty,
            "false_positive_rate_pct": round(100 * dirty / len(rows), 1),
            "spurious_findings": spans, "false_attributions": fa,
            "latency_ms_median": round(statistics.median(lat), 1), "findings": detail}


def measure(out_path: str = RESULTS, with_all_chunks: bool = False) -> dict:
    if not semantic.available():
        raise SystemExit("semantic tier unavailable; run with .venv/bin/python")
    m = M()
    status = semantic.warmup(sorted(m.languages()))
    items, prov = build_heldout_items(m)
    print(f"{len(items)} held-out items; excluded as already-indexed: "
          f"{prov['excluded_in_index']}", file=sys.stderr)

    runs = {}
    arms = [("lexical_only", False, None), ("lexical_plus_semantic", True, None)]
    if with_all_chunks:
        # The pre-2026-10-04 plan (embed every chunk) inside the same pipeline,
        # to show what the selective plan costs or saves on identical items.
        arms.append(("semantic_all_chunks", True, "all"))
    for name, on, select in arms:
        t0 = time.perf_counter()
        rows = _run(m, items, on, select)
        runs[name] = {"rows": rows, "wall_s": round(time.perf_counter() - t0, 1)}
        print(f"{name}: {runs[name]['wall_s']} s", file=sys.stderr)

    def group(rows):
        keys = sorted({(r["lang"], r["setting"], r["condition"]) for r in rows})
        return {f"{lg}:{st}:{c}": _summ([r for r in rows if (r["lang"], r["setting"], r["condition"]) == (lg, st, c)])
                for lg, st, c in keys}

    summary = {name: {"all": _summ(r["rows"]), "by_group": group(r["rows"]),
                      "by_edition": {ed: _summ([x for x in r["rows"] if x["edition"] == ed])
                                     for ed in sorted({x["edition"] for x in r["rows"]})}}
               for name, r in runs.items()}

    # Paired view: same items, what changed.
    a, b = runs["lexical_only"]["rows"], runs["lexical_plus_semantic"]["rows"]
    gained = sum(1 for x, y in zip(a, b) if y["located"] and not x["located"])
    lost = sum(1 for x, y in zip(a, b) if x["located"] and not y["located"])

    detail: list = []
    negs = {name: _negatives(m, on, detail) for name, on in
            (("lexical_only", False), ("lexical_plus_semantic", True))}

    res = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "question": ("On quotations taken from published English (and Hindi/Urdu/"
                     "Bengali) translations that are NOT in the approved index, "
                     "placed in neutral prose with no printed reference: does "
                     "MIZAN locate them, identify the verse, and refer them "
                     "(NEAR/UNATTRIBUTED) rather than falsely MATCH — and what "
                     "does the semantic tier add on the same items?"),
        "semantic_status": status,
        "protocol": {
            "held_out_editions": TEST_EDITIONS,
            "provenance": prov,
            "verse_selection": ("second half of a seeded shuffle of all 6,236 "
                                f"verses (seed {SPLIT_SEED}); tuning used the first "
                                "half. Renderings of 8-90 words."),
            "per_edition": PER_EDITION,
            "conditions": {"full": "the whole rendering",
                           "partial": "a contiguous 50-70% slice (renderings >= 14 words)"},
            "settings": {"in_prose": ("inserted at a sentence boundary inside a "
                                      "paragraph from the 120-paragraph "
                                      "false-positive probe (en/hi), which was "
                                      "never used for tuning"),
                         "bare": "the rendering alone (ur/bn: no negative prose exists)"},
            "located": ("a finding whose verse id equals the true verse and whose "
                        "span overlaps the inserted quotation"),
            "false_match": ("any finding overlapping the quotation in state MATCH; "
                            "items whose text is literally in the index were "
                            "excluded beforehand, so a MATCH here is false"),
            "pipeline": "report.check_document (the same call app.py makes)",
        },
        "summary": summary,
        "paired": {"items": len(a), "located_gained_by_semantic": gained,
                   "located_lost_with_semantic": lost},
        "false_positive_probe_120": negs,
        "rows": {k: v["rows"] for k, v in runs.items()},
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)
    os.makedirs(os.path.dirname(DETAIL), exist_ok=True)
    with open(DETAIL, "w", encoding="utf-8") as fh:
        json.dump({"note": "local only; quotes third-party text", "negative_spans": detail},
                  fh, ensure_ascii=False, indent=1)
    return res


def _print(res: dict) -> None:
    for name in res["summary"]:
        s = res["summary"][name]
        print(f"\n== {name}")
        a = s["all"]
        print(f"  ALL  n={a['n']} located {a['located_correct_verse']} ({a['recall_pct']}%) "
              f"states {a['states_of_located']} false-MATCH items {a['false_match_items']} "
              f"latency median {a['latency_ms']['median']} ms")
        for k, g in s["by_group"].items():
            print(f"  {k:22s} n={g['n']:3d} located {g['recall_pct']:5.1f}%  "
                  f"wrong-verse {g['wrong_verse_only']:3d}  states {g['states_of_located']}  "
                  f"falseMATCH {g['false_match_items']}  by tier {g['located_by_tier']}")
    p = res["paired"]
    print(f"\npaired: +{p['located_gained_by_semantic']} gained, "
          f"-{p['located_lost_with_semantic']} lost of {p['items']}")
    for name, n in res["false_positive_probe_120"].items():
        print(f"FP probe (120) {name}: {n['paragraphs_with_a_finding']}/{n['paragraphs']} "
              f"= {n['false_positive_rate_pct']}%  false attributions {n['false_attributions']}")


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--measure", action="store_true",
                    help="run the held-out measurement and write results_semantic.json")
    ap.add_argument("--with-all-chunks", action="store_true",
                    help="also run the old embed-every-chunk plan on the same items")
    ap.add_argument("--out", default=RESULTS)
    args = ap.parse_args()
    if args.measure:
        _print(measure(args.out, with_all_chunks=args.with_all_chunks))
        print(f"\nwritten {args.out}")
        return 0
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        if getattr(fn, "pytestmark", None) and not AVAILABLE:
            print(f"  SKIP  {name}  (semantic tier unavailable)")
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as e:                       # noqa: BLE001
            if pytest and isinstance(e, pytest.skip.Exception):
                print(f"  SKIP  {name}")
                continue
            failed += 1
            print(f"  FAIL  {name}\n          {type(e).__name__}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
