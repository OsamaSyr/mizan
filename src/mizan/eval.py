#!/usr/bin/env python3
"""
MIZAN — one-command evaluation.

    make eval
    PYTHONPATH=src python3 -m mizan.eval --suite all --runs 3

Every number MIZAN publishes is re-derived here, from the index a clean clone
builds with `make setup`, with fixed seeds. The output is

    results/summary.json   machine-readable: every metric, every run, mean ± sd
    results/SUMMARY.md     the same, as tables a person can read

Suites (select one, several comma-separated, or `all`)
------------------------------------------------------
index     The approved-index tests from tests/run_eval.py, called by import so
          the harness logic is reused rather than copied: A control false
          alarms, B edition identity, C separation, D rejection. Plus two
          diagnostics computed here: the wrong-verse score margin for EVERY
          language (the harness skips Hindi, which has one edition), and how
          many wrong-edition answers are exact score ties between two book
          entries of the same translation.
real      tests/real_corpus.py over the published-prose corpus: detection,
          reference accuracy, state distribution, printed-reference tier,
          the 120-paragraph false-positive probe, latency. The harness's own
          main() is called, with its output file redirected into results/cache
          so the measurement never overwrites anything under tests/.
fp        The false-positive probe over EVERY no-quotation paragraph in the
          corpus (the harness samples 120), using the harness's own function.
clarity   mizan.clarity over the English publisher prose that surrounds the
          collected quotations: refusal rate per audience level, readability
          before/after, byte-exact preservation of frozen spans.
terms     mizan.terms (terminology panel): its measurement on the real
          corpus and its two calibrations (approved translations, the
          dictionary's own parallel definitions), via the module's functions.
semantic  Lexical vs lexical+semantic on the same real-corpus items and the
          same no-quotation paragraphs — only when `mizan.semantic` reports
          itself available (ML extra installed, pinned model on disk,
          embeddings built). Skipped, with the reason, otherwise.

Detection path
--------------
`real` and `fp` always measure the DETERMINISTIC path (MIZAN_SEMANTIC=0 for
the duration of the suite), so a stdlib-only install reproduces them exactly.
What the optional model adds is measured separately, by `semantic`, on the
same items. Run `make eval PYTHON=.venv/bin/python` (an environment with
requirements-ml.txt) to include it.

Seeds and runs
--------------
Run k uses seed BASE_SEED + k. BASE_SEED is the seed tests/run_eval.py uses
on its own, so run 0 of the index suite reproduces tests/results_approved.json
exactly. The sampled suites therefore report variance across three different
fixed samples; the deterministic suites (real, fp, clarity) report sd 0 on
every quality metric — anything else would be a bug — and non-zero sd only on
wall-clock latency.

Each run is cached under results/cache/ keyed by the index fingerprint, the
code fingerprint, the corpus fingerprint, the seed and the parameters, so an
interrupted evaluation resumes and an unchanged one is free. `--fresh`
ignores the cache.

Stdlib only.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import hashlib
import importlib
import importlib.util
import io
import json
import os
import platform
import random
import re
import sqlite3
import statistics
import sys
import time
from typing import Any, Callable

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from mizan.engine import DB, T_MATCH, T_NEAR, Mizan, sim  # noqa: E402

TESTS = os.path.join(ROOT, "tests")
RUN_EVAL = os.path.join(TESTS, "run_eval.py")
REAL_CORPUS = os.path.join(TESTS, "real_corpus.py")
REAL_DIR = os.path.join(ROOT, "data", "real")
CORPUS = os.path.join(REAL_DIR, "corpus.jsonl")
NEGATIVES = os.path.join(REAL_DIR, "negatives.jsonl")
RESULTS = os.path.join(ROOT, "results")
README = os.path.join(ROOT, "README.md")

BASE_SEED = 20261001          # tests/run_eval.py's own seed
SUITES = ("index", "real", "fp", "clarity", "terms", "semantic")

# The index every published number was measured on. A clean clone that runs
# `make setup` must reproduce this exactly; `--check-index` (called by
# scripts/setup.sh) fails loudly if it does not. The fingerprint covers the
# CONTENT of the index — every translation's metadata, every rendering and
# its normalised form, every Arabic verse — and deliberately excludes the
# dump-date label, which Quranpedia bumps daily even when nothing changed.
REFERENCE_INDEX_FINGERPRINT = (
    "c5ce4090a69fb9439ac48edff9b2cd08784ee7268a984f64688f9a37b1952073")

# The real-world corpus every real/fp/clarity number was measured on (473
# quotations, 560 no-quotation paragraphs, collected 2026-10-02). It is not
# committed; scripts/rebuild_real_corpus.py rebuilds it from the publishers'
# pages and this fingerprint says whether the rebuild is byte-identical.
REFERENCE_CORPUS_FINGERPRINT = (
    "e185c9b4a670fe52f121b5cc80f5df89f286f5b3be027ba4a331e2064a441dd9")

README_START = "<!-- mizan:results:start -->"
README_END = "<!-- mizan:results:end -->"


# ===========================================================================
# Fingerprints — what exactly was measured
# ===========================================================================
def _sha(chunks) -> str:
    h = hashlib.sha256()
    for c in chunks:
        h.update(c if isinstance(c, bytes) else c.encode("utf-8"))
    return h.hexdigest()


def index_fingerprint(db: str = DB) -> str:
    """SHA-256 over the index CONTENT, independent of file layout and label."""
    if not os.path.exists(db):
        raise FileNotFoundError(db)
    con = sqlite3.connect(db)
    try:
        def rows(sql: str):
            for r in con.execute(sql):
                yield json.dumps(list(r), ensure_ascii=False) + "\n"
        has_ar = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='arabic_ayat'"
        ).fetchone()
        parts = [
            rows("SELECT book_id, title, language, locale, direction, source, n_ayat "
                 "FROM translations ORDER BY book_id"),
            rows("SELECT book_id, surah, ayah, text, text_norm FROM ayat "
                 "ORDER BY book_id, surah, ayah"),
        ]
        if has_ar:
            parts.append(rows("SELECT surah, ayah, text_uthmani, text_simple, text_norm "
                              "FROM arabic_ayat ORDER BY surah, ayah"))
        h = hashlib.sha256()
        for p in parts:
            for line in p:
                h.update(line.encode("utf-8"))
        return h.hexdigest()
    finally:
        con.close()


def index_facts(db: str = DB) -> dict:
    con = sqlite3.connect(db)
    try:
        q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
        langs = dict(con.execute(
            "SELECT language, COUNT(*) FROM translations GROUP BY language "
            "ORDER BY language").fetchall())
        versions = [r[0] for r in con.execute(
            "SELECT version, COUNT(*) c FROM translations GROUP BY version ORDER BY c DESC")]
        has_ar = q("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                   "AND name='arabic_ayat'")
        return {
            "source": "quranpedia.net",
            "version": versions[0] if versions else None,
            "translations": q("SELECT COUNT(*) FROM translations"),
            "languages": langs,
            "renderings": q("SELECT COUNT(*) FROM ayat"),
            "arabic_verses": q("SELECT COUNT(*) FROM arabic_ayat") if has_ar else 0,
        }
    finally:
        con.close()


def code_fingerprint() -> str:
    """
    Everything besides the index and corpus whose change could move a number:
    the package, the two harnesses, the curated glossary and the embedding
    manifest. Used as part of the run-cache key.
    """
    files = sorted(
        os.path.join(SRC, "mizan", f) for f in os.listdir(os.path.join(SRC, "mizan"))
        if f.endswith(".py") and f != "eval.py")
    files += [RUN_EVAL, REAL_CORPUS]
    gdir = os.path.join(ROOT, "data", "glossary")
    if os.path.isdir(gdir):
        files += sorted(os.path.join(gdir, f) for f in os.listdir(gdir)
                        if f.endswith((".json", ".jsonl")) and f != "measurement.json")
    files.append(os.path.join(ROOT, "data", "embeddings", "index.json"))
    return _sha(open(f, "rb").read() for f in files if os.path.exists(f))


def corpus_fingerprint() -> str | None:
    """SHA-256 over the MEASURED fields of the real corpus, in file order."""
    if not (os.path.exists(CORPUS) and os.path.exists(NEGATIVES)):
        return None

    def lines():
        for path, fields in ((CORPUS, ("id", "lang", "published_text", "printed_ref",
                                       "context_before", "context_after",
                                       "scripture_marked", "markup_hint")),
                             (NEGATIVES, ("id", "lang", "text"))):
            with open(path, encoding="utf-8") as fh:
                for ln in fh:
                    if ln.strip():
                        r = json.loads(ln)
                        yield json.dumps([r.get(k) for k in fields],
                                         ensure_ascii=False) + "\n"
    return _sha(lines())


# ===========================================================================
# Small helpers
# ===========================================================================
def _load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # type: ignore[union-attr]
    return mod


def _load_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def _pct(n: float, d: float) -> float:
    return round(100.0 * n / d, 2) if d else 0.0


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class Skip(Exception):
    """A suite whose inputs are missing. Reported, never silently dropped."""


@contextlib.contextmanager
def _semantic_env(on: bool):
    """Force the detector's tier (c) off, or leave it to availability."""
    old = os.environ.get("MIZAN_SEMANTIC")
    if on:
        os.environ.pop("MIZAN_SEMANTIC", None)
    else:
        os.environ["MIZAN_SEMANTIC"] = "0"
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("MIZAN_SEMANTIC", None)
        else:
            os.environ["MIZAN_SEMANTIC"] = old


# Human labels and units for the tables. Keys not listed still appear in
# summary.json; the table shows them with their raw key.
LABELS: dict[str, tuple[str, str]] = {
    # index
    "control.renderings": ("A control — approved renderings fed back", "n"),
    "control.false_alarm_pct": ("A control — false alarms (approved text flagged)", "%"),
    "identity.cases": ("B identity — cases", "n"),
    "identity.correct_edition_pct": ("B identity — attributed to the exact edition", "%"),
    "separation.mean_sim": ("C separation — mean sim, two approved editions of one verse", ""),
    "separation.collisions_pct": ("C separation — edition pairs ≥ MATCH threshold", "%"),
    "rejection.cases": ("D rejection — wrong-verse texts", "n"),
    "rejection.recall_pct": ("D rejection — wrong verse returned UNATTRIBUTED", "%"),
    "ties.wrong_edition_pct": ("Edition confusion — wrong edition named", "%"),
    "ties.tie_share_of_wrong_pct": ("Edition confusion — of those, exact score ties", "%"),
    # real
    "stripped.detection_pct": ("Citation stripped — quotation located", "%"),
    "stripped.ref_accuracy_pct": ("Citation stripped — same verse the publisher cited", "%"),
    "published.detection_pct": ("As published — quotation located", "%"),
    "published.ref_accuracy_pct": ("As published — same verse the publisher cited", "%"),
    "published.attributed_pct": ("As published — attributed to an approved translation", "%"),
    "published.MATCH_pct": ("As published — MATCH", "%"),
    "published.NEAR_pct": ("As published — NEAR", "%"),
    "published.UNATTRIBUTED_pct": ("As published — UNATTRIBUTED (referred)", "%"),
    "published.NO_APPROVED_TRANSLATION_pct": ("As published — NO_APPROVED_TRANSLATION", "%"),
    "published.UNRESOLVED_pct": ("As published — UNRESOLVED", "%"),
    "tier_a.precision_pct": ("Printed-reference tier — cited verse located", "%"),
    "tier_a.survival_pct": ("Printed-reference tier — survived to final report", "%"),
    "probe120.fp_rate_pct": ("FP probe (120 ¶) — paragraphs with any finding", "%"),
    "probe120.false_attribution_pct": ("FP probe (120 ¶) — FALSE ATTRIBUTION", "%"),
    "latency.quote_mean_ms": ("Latency per quotation — mean", "ms"),
    "latency.quote_p95_ms": ("Latency per quotation — p95", "ms"),
    "latency.document_mean_ms": ("Latency per document — mean", "ms"),
    "quotations": ("Quotations measured", "n"),
    # fp
    "paragraphs": ("No-quotation paragraphs", "n"),
    "fp_rate_pct": ("FP probe, every no-quotation ¶ — paragraphs with any finding", "%"),
    "false_attribution_pct": ("FP probe, every no-quotation ¶ — FALSE ATTRIBUTION (MATCH on prose)", "%"),
    "spurious_referral_per_100": ("Spurious referrals per 100 paragraphs", ""),
    "latency_mean_ms": ("Latency per paragraph — mean", "ms"),
    "false_attributions": ("False attributions (count)", "n"),
    # clarity
    "text.paragraphs": ("Publisher prose — distinct English paragraphs", "n"),
    "text.words": ("Publisher prose — words", "n"),
    # semantic
    "delta.stripped.detection_pct": ("Semantic tier adds — quotations located, citation stripped", "%"),
    "delta.published.detection_pct": ("Semantic tier adds — quotations located, as published", "%"),
    "delta.stripped.ref_accuracy_pct": ("Semantic tier adds — cited verse, citation stripped", "%"),
    "delta.fp.fp_rate_pct": ("Semantic tier adds — FP probe paragraphs with a finding", "%"),
    "delta.fp.false_attribution_pct": ("Semantic tier adds — FALSE ATTRIBUTION", "%"),
    # misc
    "ties.cases": ("Edition confusion — cases", "n"),
    "stripped.attributed_pct": ("Citation stripped — attributed to an approved translation", "%"),
    "published.with_printed_ref": ("As published — located items carrying a printed citation", "n"),
    "probe120.paragraphs": ("FP probe — paragraphs (harness default)", "n"),
}

# Metrics that measure wall-clock time: the only ones allowed to vary on a
# deterministic suite.
TIMING = re.compile(r"(latency|_ms$|seconds)")


# ===========================================================================
# Suite: index  (tests/run_eval.py, by import)
# ===========================================================================
_RUN_EVAL = None


def _run_eval():
    global _RUN_EVAL
    if _RUN_EVAL is None:
        _RUN_EVAL = _load_module("mizan_run_eval", RUN_EVAL)
    return _RUN_EVAL


_IDENT_LINE = re.compile(r"^\s{4}(\w+)\s+(\d+)/(\d+)\s+\(")


def suite_index(seed: int, params: dict, log: io.StringIO) -> dict:
    re_ = _run_eval()
    random.seed(seed)                 # the harness draws from the global RNG
    n = params["n"]
    out: dict[str, Any] = {}
    with contextlib.redirect_stdout(log):
        ctl = re_.test_control(n)
        mark = log.tell()
        idn = re_.test_identity(n)
        ident_text = log.getvalue()[mark:]
        sep = re_.test_separation(max(n, 500) if n >= 400 else n)
        rej = re_.test_rejection(300 if n >= 400 else max(60, n))

    m: dict[str, float] = {
        "control.renderings": ctl["n"],
        "control.false_alarm_pct": round(100 * ctl["rate"], 3),
        "identity.cases": idn["n"],
        "identity.correct_edition_pct": round(100 * idn["accuracy"], 2),
        "separation.mean_sim": sep["diff_mean"],
        "separation.collisions_pct": _pct(sep["collisions"], sep["n"]),
        "rejection.cases": rej["n"],
        "rejection.recall_pct": round(100 * rej["recall"], 2),
    }
    for lang, v in sorted(ctl["per_lang"].items()):
        m[f"control.{lang}.false_alarm_pct"] = _pct(v["total"] - v["ok"], v["total"])
    for line in ident_text.splitlines():
        mm = _IDENT_LINE.match(line)
        if mm:
            m[f"identity.{mm.group(1)}.correct_edition_pct"] = _pct(
                int(mm.group(2)), int(mm.group(3)))

    # ---- diagnostics computed here (own RNG; the harness stream is untouched)
    M = re_.M
    rng = random.Random(seed ^ 0x5EED)
    margins, ties = _margins_and_ties(M, rng, params["diag_n"])
    foot = _footnoted_editions(M, rng, params["diag_n"])
    m.update(margins["metrics"])
    m.update(ties["metrics"])
    m.update(foot["metrics"])
    out["metrics"] = m
    out["detail"] = {"confused_pairs": ties["pairs"],
                     "margin_note": margins["note"],
                     "footnoted_editions": foot["detail"],
                     "thresholds": {"MATCH": T_MATCH, "NEAR": T_NEAR}}
    return out


_FOOTNOTE_BLOCK = re.compile(r"_{4,}")


def _footnoted_editions(M: Mizan, rng: random.Random, n: int) -> dict:
    """
    Editions that store translator commentary after a '____' separator in the
    same field as the verse. A publisher who quotes only the verse part of such
    an edition is compared against verse + commentary, so the score drops. We
    feed the verse part back and record what MIZAN answers.
    """
    metrics: dict[str, float] = {}
    detail = []
    for bid, b in sorted(M.books.items()):
        rows = [r for r in M.con.execute(
            "SELECT surah, ayah, text FROM ayat WHERE book_id=?", (bid,))
            if _FOOTNOTE_BLOCK.search(r[2])]
        if len(rows) < 50:
            continue
        lang = b["language"]
        sample = rng.sample(rows, min(n, len(rows)))
        own = other = unattr = 0
        for s, a, t in sample:
            part = _FOOTNOTE_BLOCK.split(t, maxsplit=1)[0]
            r = M.attribute(part, s, a, lang)
            if r["state"] == "UNATTRIBUTED":
                unattr += 1
            elif r["attributed_to"] == bid:
                own += 1
            else:
                other += 1
        k = len(sample)
        metrics[f"footnoted.{bid}.verse_part_unattributed_pct"] = _pct(unattr, k)
        detail.append({"book_id": bid, "lang": lang, "title": b["title"],
                       "verses_with_commentary": len(rows), "sampled": k,
                       "attributed_to_itself": own, "attributed_to_another_edition": other,
                       "unattributed": unattr,
                       "editions_in_language": len(M.editions(lang))})
    return {"metrics": metrics, "detail": detail}


def _margins_and_ties(M: Mizan, rng: random.Random, n: int) -> tuple[dict, dict]:
    refs = [(r[0], r[1]) for r in M.con.execute(
        "SELECT DISTINCT surah, ayah FROM ayat ORDER BY surah, ayah")]
    metrics: dict[str, float] = {}

    # Wrong-verse margin, EVERY language. The harness's rejection test needs
    # >= 2 editions and therefore never looks at Hindi.
    for lang, ids in sorted(M.languages().items()):
        best = []
        for _ in range(n):
            (s, a), (s2, a2) = rng.sample(refs, 2)
            t = M.verse(rng.choice(ids), s2, a2)
            if not t or len(t) < 40:
                continue
            best.append(M.attribute(t, s, a, lang)["score"])
        best.sort()
        if best:
            metrics[f"margin.{lang}.p95"] = round(best[int(0.95 * (len(best) - 1))], 3)
            metrics[f"margin.{lang}.max"] = round(best[-1], 3)
            metrics[f"margin.{lang}.over_near_pct"] = _pct(
                sum(1 for x in best if x >= T_NEAR), len(best))
    margins = {"metrics": metrics, "note": (
        "best score a DIFFERENT verse's approved text reaches against all approved "
        f"renderings of the target verse; NEAR threshold is {T_NEAR}")}

    # Edition identity, decomposed. Same decision rule as engine.attribute
    # (first maximum in edition order), but keeping the full score vector so a
    # wrong answer can be classified as a TIE (undecidable by any text
    # comparison: the two editions print the same words) or a genuine miss.
    wrong = tie = total = 0
    pairs: dict[tuple[int, int], dict] = {}
    for lang, ids in sorted(M.languages().items()):
        if len(ids) < 2:
            continue
        for s, a in rng.sample(refs, n):
            texts = {b: M.verse(b, s, a) for b in ids}
            for true_b in ids:
                t = texts[true_b]
                if not t or len(t) < 25:
                    continue
                scored = [(b, sim(t, texts[b])) for b in ids if texts[b]]
                best_b, best_s = scored[0]
                for b, sc in scored[1:]:
                    if sc > best_s:
                        best_b, best_s = b, sc
                total += 1
                if best_b == true_b:
                    continue
                wrong += 1
                is_tie = dict(scored)[true_b] == best_s
                tie += is_tie
                p = pairs.setdefault((true_b, best_b), {"n": 0, "ties": 0})
                p["n"] += 1
                p["ties"] += is_tie
    top = sorted(pairs.items(), key=lambda kv: -kv[1]["n"])[:6]
    detail = [{
        "true": M.books[x]["title"], "true_id": x,
        "predicted": M.books[y]["title"], "predicted_id": y,
        "n": v["n"], "exact_ties": v["ties"],
    } for (x, y), v in top]
    ties = {"metrics": {
        "ties.cases": total,
        "ties.wrong_edition_pct": _pct(wrong, total),
        "ties.tie_share_of_wrong_pct": _pct(tie, wrong),
    }, "pairs": detail}
    return margins, ties


# ===========================================================================
# Suite: real  (tests/real_corpus.py main(), output redirected)
# ===========================================================================
_REAL = None


def _real_corpus():
    global _REAL
    if _REAL is None:
        _REAL = _load_module("mizan_real_corpus", REAL_CORPUS)
    return _REAL


def _need_corpus() -> None:
    if not os.path.exists(CORPUS):
        raise Skip("data/real/corpus.jsonl is not present. It holds third-party "
                   "published text and is not committed; rebuild it from the "
                   "publishers' pages with `make real-corpus` "
                   "(scripts/rebuild_real_corpus.py).")


def suite_real(seed: int, params: dict, log: io.StringIO, cache_json: str) -> dict:
    _need_corpus()
    rc = _real_corpus()
    rc._DETECTORS.clear()             # every run builds its own detectors
    rc.RESULTS = cache_json           # never write into tests/
    argv = ["real_corpus.py", "--negatives", str(params["negatives"])]
    if params.get("limit"):
        argv += ["--limit", str(params["limit"])]
    old_argv = sys.argv
    sys.argv = argv
    try:
        with _semantic_env(False), contextlib.redirect_stdout(log), \
                contextlib.redirect_stderr(log):
            code = rc.main()
    finally:
        sys.argv = old_argv
    if code:
        raise RuntimeError(f"tests/real_corpus.py exited {code}; see the log")
    res = json.load(open(cache_json, encoding="utf-8"))

    q, qr = res["quotations"], res["quotations_with_printed_ref"]
    o, orf = q["overall"], qr["overall"]
    m: dict[str, float] = {
        "quotations": o["n"],
        "stripped.detection_pct": o["detection_rate_pct"],
        "stripped.ref_accuracy_pct": o["ref_accuracy_pct"],
        "stripped.attributed_pct": o["attribution_rate_pct"],
        "published.detection_pct": orf["detection_rate_pct"],
        "published.ref_accuracy_pct": orf["ref_accuracy_pct"],
        "published.with_printed_ref": orf["with_printed_ref"],
        "published.attributed_pct": orf["attribution_rate_pct"],
    }
    for st, v in orf["state_pct"].items():
        m[f"published.{st}_pct"] = v
    for lg, v in sorted(qr["by_language"].items()):
        m[f"published.{lg}.n"] = v["n"]
        m[f"published.{lg}.detection_pct"] = v["detection_rate_pct"]
        if v["with_printed_ref"]:
            m[f"published.{lg}.ref_accuracy_pct"] = v["ref_accuracy_pct"]
        m[f"published.{lg}.attributed_pct"] = v["attribution_rate_pct"]
    rt = res["reference_tier_diagnostic"]["overall"]
    m["tier_a.precision_pct"] = rt.get("tier_a_precision_pct", 0.0)
    m["tier_a.survival_pct"] = rt.get("survival_pct", 0.0)
    neg = res["false_positive_probe"]
    m["probe120.paragraphs"] = neg["paragraphs"]
    m["probe120.fp_rate_pct"] = neg["false_positive_rate_pct"]
    m["probe120.false_attribution_pct"] = neg["false_attribution_rate_pct"]
    m["latency.quote_mean_ms"] = orf["latency_ms"]["mean"]
    m["latency.quote_p95_ms"] = orf["latency_ms"]["p95"]
    if res["documents"]["documents"]:
        m["latency.document_mean_ms"] = res["documents"]["latency_ms"]["mean"]
    return {"metrics": m, "detail": {
        "corpus": res["corpus"], "caveats": res.get("caveats", []),
        "detection_path": "deterministic (MIZAN_SEMANTIC=0)",
        "harness_output": os.path.relpath(cache_json, ROOT)}}


# ===========================================================================
# Suite: fp  (the probe over every no-quotation paragraph)
# ===========================================================================
def suite_fp(seed: int, params: dict, log: io.StringIO) -> dict:
    _need_corpus()
    negs = _load_jsonl(NEGATIVES)
    if not negs:
        raise Skip("data/real/negatives.jsonl is empty or missing")
    if params.get("limit"):
        negs = negs[:params["limit"]]
    rc = _real_corpus()
    rc._DETECTORS.clear()
    m_ = Mizan()
    with _semantic_env(False), contextlib.redirect_stdout(log):
        _, s = rc.measure_negatives(m_, negs)
    m: dict[str, float] = {
        "paragraphs": s["paragraphs"],
        "fp_rate_pct": s["false_positive_rate_pct"],
        "false_attribution_pct": s["false_attribution_rate_pct"],
        "false_attributions": s["false_attributions"],
        "spurious_referral_per_100": round(100 * s["spurious_referrals"]
                                           / max(1, s["paragraphs"]), 2),
        "latency_mean_ms": s["latency_ms"]["mean"],
    }
    for lg, v in sorted(s["by_language"].items()):
        m[f"{lg}.paragraphs"] = v["paragraphs"]
        m[f"{lg}.fp_rate_pct"] = v["false_positive_rate_pct"]
    detail = {"false_attribution_examples": [
        {"lang": d["lang"], "ref": d["finding"]["ref"], "state": d["finding"]["state"],
         "score": d["finding"]["score"], "span_words": len(d["finding"]["span"].split())}
        for d in s["false_attribution_detail"][:10]],
        "spurious_span_widths": s["spurious_span_widths"]}
    return {"metrics": m, "detail": detail}


# ===========================================================================
# Suite: clarity
# ===========================================================================
def clarity_text() -> tuple[str, int]:
    """
    The measured text: every distinct English context paragraph around a
    collected quotation, in corpus order, joined by blank lines. This is the
    publisher's own prose, not ours.
    """
    _need_corpus()
    rows = [r for r in _load_jsonl(CORPUS) if r.get("lang") == "en"]
    seen: list[str] = []
    for r in rows:
        for k in ("context_before", "context_after"):
            c = (r.get(k) or "").strip()
            if c and c not in seen:
                seen.append(c)
    return "\n\n".join(seen), len(seen)


def suite_clarity(seed: int, params: dict, log: io.StringIO) -> dict:
    from mizan import clarity
    text, n_par = clarity_text()
    if not text:
        raise Skip("no English context paragraphs in the real corpus")
    m: dict[str, float] = {"text.paragraphs": n_par, "text.words": len(text.split())}
    breakdown = {}
    for level in clarity.LEVELS:
        rep = clarity.adapt_document(text, level)
        m[f"{level}.prose_sentences"] = rep.n_prose
        m[f"{level}.refused"] = rep.n_refused
        m[f"{level}.refusal_rate_pct"] = round(100 * rep.refusal_rate, 2)
        m[f"{level}.adapted"] = rep.n_adapted
        if rep.readability_before and rep.readability_before.flesch is not None:
            m[f"{level}.flesch_before"] = rep.readability_before.flesch
        if rep.readability_after and rep.readability_after.flesch is not None:
            m[f"{level}.flesch_after"] = rep.readability_after.flesch
        m[f"{level}.byte_exact"] = 1.0 if rep.frozen_tokens_verified else 0.0
        breakdown[level] = dict(sorted(rep.refusal_counts.items(), key=lambda kv: -kv[1]))
    return {"metrics": m, "detail": {"refusal_counts": breakdown,
                                     "method": " ".join(clarity_text.__doc__.split())}}


# ===========================================================================
# Suite: terms  (mizan.terms measurement functions)
# ===========================================================================
def _tally_metrics(prefix: str, t: dict | None, m: dict) -> None:
    if not t:
        return
    for k in ("units", "units_with_findings", "findings"):
        if isinstance(t.get(k), (int, float)):
            m[f"{prefix}.{k}"] = t[k]
    for st, v in (t.get("share") or {}).items():
        if isinstance(v, (int, float)):
            m[f"{prefix}.{st}_pct"] = round(100 * v, 2)


def suite_terms(seed: int, params: dict, log: io.StringIO) -> dict:
    try:
        terms = importlib.import_module("mizan.terms")
    except Exception as exc:                                  # noqa: BLE001
        raise Skip(f"mizan.terms does not import ({type(exc).__name__}: {exc})")
    m: dict[str, float] = {}
    detail: dict[str, Any] = {}
    with contextlib.redirect_stdout(log):
        g = terms.load_glossary()
        detail["glossary"] = {"source": getattr(g, "source", None),
                              "terms": len(getattr(g, "terms", []) or [])}
        m["glossary.terms"] = detail["glossary"]["terms"]
        if os.path.exists(CORPUS):
            rc = terms.measure_real_corpus(CORPUS, "en")
            _tally_metrics("real.per_quote", rc.get("per_quote"), m)
            _tally_metrics("real.per_article", rc.get("per_article"), m)
        else:
            detail["real"] = "skipped: data/real/corpus.jsonl not built"
        pd = terms.measure_parallel_definitions()
        if pd is None:
            detail["dictionary_definitions"] = (
                "skipped: data/glossary/parallel_definitions.jsonl holds الجمهرة's "
                "own definitions and is kept local (not redistributed); re-collect "
                "it with scripts/fetch_jamhara.py to run this calibration")
        _tally_metrics("calib.dictionary_definitions", pd, m)
        at = terms.measure_approved_translations(lang="en")
        _tally_metrics("calib.approved_translations", at, m)
    return {"metrics": m, "detail": detail}


# ===========================================================================
# Suite: semantic  (lexical vs lexical + semantic, same items)
# ===========================================================================
def _semantic_module():
    try:
        sem = importlib.import_module("mizan.semantic")
    except Exception as exc:                                  # noqa: BLE001
        raise Skip(f"mizan.semantic does not import ({type(exc).__name__}: {exc})")
    try:
        ok = sem.available()
    except Exception as exc:                                  # noqa: BLE001
        raise Skip(f"mizan.semantic.available() raised {exc!r}")
    if not ok:
        st = {}
        try:
            st = sem.status()
        except Exception:                                     # noqa: BLE001
            pass
        why = ", ".join(f"{k}={st[k]}" for k in
                        ("deps_installed", "model_on_disk", "index_built") if k in st)
        raise Skip("the semantic tier is not available in this environment"
                   + (f" ({why})" if why else "")
                   + ". Install requirements-ml.txt, run scripts/build_embeddings.py "
                     "--download && scripts/build_embeddings.py, then "
                     "`make eval PYTHON=.venv/bin/python`.")
    return sem


def suite_semantic(seed: int, params: dict, log: io.StringIO) -> dict:
    _need_corpus()
    sem = _semantic_module()
    rc = _real_corpus()
    rows, negs = _load_jsonl(CORPUS), _load_jsonl(NEGATIVES)
    if params.get("limit"):
        rows, negs = rows[:params["limit"]], negs[:params["limit"]]
    m_ = Mizan()
    m: dict[str, float] = {}
    by_lang: dict[str, dict] = {}
    for mode in ("lexical", "semantic"):
        rc._DETECTORS.clear()
        with _semantic_env(mode == "semantic"), contextlib.redirect_stdout(log), \
                contextlib.redirect_stderr(log):
            items_s, q_s = rc.measure_quotations(m_, rows, with_printed_ref=False)
            items_p, q_p = rc.measure_quotations(m_, rows, with_printed_ref=True)
            _, n_s = rc.measure_negatives(m_, negs)
        for cond, q in (("stripped", q_s), ("published", q_p)):
            o = q["overall"]
            m[f"{mode}.{cond}.detection_pct"] = o["detection_rate_pct"]
            m[f"{mode}.{cond}.ref_accuracy_pct"] = o["ref_accuracy_pct"]
            m[f"{mode}.{cond}.attributed_pct"] = o["attribution_rate_pct"]
            m[f"{mode}.{cond}.UNATTRIBUTED_pct"] = o["state_pct"].get("UNATTRIBUTED", 0.0)
            for lg, v in q["by_language"].items():
                by_lang.setdefault(lg, {})[f"{mode}.{cond}.detection_pct"] = \
                    v["detection_rate_pct"]
        m[f"{mode}.stripped.found_by_semantic_tier"] = sum(
            1 for it in items_s if it.get("tier") == "semantic")
        m[f"{mode}.fp.fp_rate_pct"] = n_s["false_positive_rate_pct"]
        m[f"{mode}.fp.false_attribution_pct"] = n_s["false_attribution_rate_pct"]
        m[f"{mode}.latency.quote_mean_ms"] = q_p["overall"]["latency_ms"]["mean"]
    for k in [k for k in m if k.startswith("lexical.") and "latency" not in k
              and "found_by" not in k]:
        tail = k[len("lexical."):]
        m[f"delta.{tail}"] = round(m[f"semantic.{tail}"] - m[k], 2)
    st = {}
    try:
        st = sem.status()
    except Exception:                                         # noqa: BLE001
        pass
    return {"metrics": m, "detail": {
        "quotations": len(rows), "negatives": len(negs),
        "by_language": by_lang,
        "semantic_status": {k: st.get(k) for k in
                            ("model", "model_revision", "index_languages", "index_design")}}}


# ===========================================================================
# Orchestration
# ===========================================================================
DETERMINISTIC = {"index": False, "real": True, "fp": True, "clarity": True,
                 "terms": True, "semantic": False}
KIND = {"index": "sampled — seed changes the sample",
        "real": "deterministic", "fp": "deterministic", "clarity": "deterministic",
        "terms": "deterministic",
        "semantic": "model-dependent (pinned revision; scores rounded to 4 decimals)"}


def _params(suite: str, quick: bool) -> dict:
    if suite == "index":
        return {"n": 100 if quick else 400, "diag_n": 60 if quick else 150}
    if suite == "real":
        return {"negatives": 30 if quick else 120, "limit": 60 if quick else 0}
    if suite == "fp":
        return {"limit": 60 if quick else 0}
    if suite == "semantic":
        return {"limit": 60 if quick else 0}
    return {}


def run_suite(suite: str, runs: int, quick: bool, fresh: bool, cache_dir: str,
              fps: dict) -> dict:
    params = _params(suite, quick)
    per_run: list[dict] = []
    for k in range(runs):
        seed = BASE_SEED + k
        key = _sha([suite, str(seed), json.dumps(params, sort_keys=True),
                    fps["index"], fps["code"], fps.get("corpus") or "-"])[:12]
        stem = os.path.join(cache_dir, f"{suite}.run{k}.{key}")
        if not fresh and os.path.exists(stem + ".json"):
            per_run.append(json.load(open(stem + ".json", encoding="utf-8")))
            _log(f"  [{suite}] run {k + 1}/{runs} seed {seed}: cached")
            continue
        log = io.StringIO()
        t0 = time.time()
        try:
            if suite == "index":
                r = suite_index(seed, params, log)
            elif suite == "real":
                r = suite_real(seed, params, log, stem + ".harness.json")
            elif suite == "fp":
                r = suite_fp(seed, params, log)
            elif suite == "clarity":
                r = suite_clarity(seed, params, log)
            elif suite == "terms":
                r = suite_terms(seed, params, log)
            elif suite == "semantic":
                r = suite_semantic(seed, params, log)
            else:
                raise ValueError(suite)
        except Skip as why:
            _log(f"  [{suite}] SKIPPED: {why}")
            return {"status": "skipped", "reason": str(why)}
        finally:
            with open(stem + ".log", "w", encoding="utf-8") as fh:
                fh.write(log.getvalue())
        r["seed"] = seed
        r["seconds"] = round(time.time() - t0, 1)
        with open(stem + ".json", "w", encoding="utf-8") as fh:
            json.dump(r, fh, ensure_ascii=False, indent=1)
        per_run.append(r)
        _log(f"  [{suite}] run {k + 1}/{runs} seed {seed}: {r['seconds']}s")

    return aggregate(suite, per_run, params)


def aggregate(suite: str, per_run: list[dict], params: dict) -> dict:
    keys: list[str] = []
    for r in per_run:
        for k in r["metrics"]:
            if k not in keys:
                keys.append(k)
    metrics = {}
    drift = []
    for k in keys:
        vals = [r["metrics"][k] for r in per_run if k in r["metrics"]]
        sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
        metrics[k] = {
            "mean": round(statistics.mean(vals), 4),
            "sd": round(sd, 4),
            "min": min(vals), "max": max(vals),
            "values": vals,
        }
        if DETERMINISTIC[suite] and sd > 0 and not TIMING.search(k):
            drift.append(k)
    out = {
        "status": "ok",
        "deterministic": DETERMINISTIC[suite],
        "kind": KIND[suite],
        "params": params,
        "seeds": [r["seed"] for r in per_run],
        "seconds": [r.get("seconds") for r in per_run],
        "metrics": metrics,
        "detail": per_run[0].get("detail", {}),
    }
    if drift:
        # A deterministic suite that varies across runs is a bug, and the
        # report says so instead of averaging it away.
        out["nondeterminism_detected"] = drift
    return out


# ===========================================================================
# Reporting
# ===========================================================================
def _fmt(key: str, v: dict) -> str:
    unit = LABELS.get(key, ("", ""))[1] or ("%" if key.endswith("_pct") else "")
    mean, sd = v["mean"], v["sd"]
    if key.startswith("delta."):
        s = f"{mean:+.1f}" + (f" ± {sd:.1f}" if sd else "")
        return s + (" pp" if key.endswith("_pct") else "")
    if unit == "n" or (float(mean).is_integer() and sd == 0 and unit != "%"):
        s = f"{mean:,.0f}" if sd == 0 else f"{mean:,.1f} ± {sd:,.1f}"
        return s
    dec = 3 if (unit == "" and abs(mean) < 2) else (3 if unit == "%" and abs(mean) < 1 else 1)
    if 0 < sd < 0.05 and dec < 2:
        dec = 2
    s = f"{mean:.{dec}f}"
    if sd:
        s += f" ± {sd:.{dec}f}"
    else:
        s += " ± 0"
    if unit == "%":
        s += " %"
    elif unit == "ms":
        s += " ms"
    return s


SUITE_TITLES = {
    "index": "Approved index — calibration (tests/run_eval.py)",
    "real": "Published prose — real-corpus harness (tests/real_corpus.py)",
    "fp": "False-positive probe — every no-quotation paragraph",
    "clarity": "Clarity panel — refusal rate and readability (mizan.clarity)",
    "terms": "Terminology panel — measurement and calibration (mizan.terms)",
    "semantic": "Lexical vs lexical + semantic (same items)",
}

# (suite, metric, metric holding its n)
HEADLINE = [
    ("index", "control.false_alarm_pct", "control.renderings"),
    ("index", "identity.correct_edition_pct", "identity.cases"),
    ("index", "ties.tie_share_of_wrong_pct", None),
    ("index", "rejection.recall_pct", "rejection.cases"),
    ("real", "published.detection_pct", "quotations"),
    ("real", "published.ref_accuracy_pct", "published.with_printed_ref"),
    ("real", "published.attributed_pct", "quotations"),
    ("real", "stripped.detection_pct", "quotations"),
    ("fp", "fp_rate_pct", "paragraphs"),
    ("fp", "false_attribution_pct", "paragraphs"),
    ("clarity", "curious.refusal_rate_pct", "curious.prose_sentences"),
    ("semantic", "delta.stripped.detection_pct", None),
    ("semantic", "delta.fp.false_attribution_pct", None),
]


_PATTERNS = [
    (re.compile(r"^control\.(\w+)\.false_alarm_pct$"), "A control — false alarms, {0}"),
    (re.compile(r"^identity\.(\w+)\.correct_edition_pct$"), "B identity — exact edition, {0}"),
    (re.compile(r"^real\.per_quote\.(\w+?)(_pct)?$"), "Real corpus, per quoted passage — {0}"),
    (re.compile(r"^real\.per_article\.(\w+?)(_pct)?$"), "Real corpus, per article — {0}"),
    (re.compile(r"^calib\.approved_translations\.(\w+?)(_pct)?$"),
     "Calibration, approved translations (verse × edition) — {0}"),
    (re.compile(r"^calib\.dictionary_definitions\.(\w+?)(_pct)?$"),
     "Calibration, Jamhara's own AR/EN definitions — {0}"),
    (re.compile(r"^glossary\.terms$"), "Glossary terms loaded"),
    (re.compile(r"^(lexical|semantic|delta)\.(stripped|published)\.(\w+?)(_pct)?$"),
     "{0} · {1} — {2}"),
    (re.compile(r"^(lexical|semantic|delta)\.fp\.(\w+?)(_pct)?$"), "{0} · FP probe — {1}"),
    (re.compile(r"^(lexical|semantic)\.latency\.quote_mean_ms$"),
     "{0} · latency per quotation — mean"),
    (re.compile(r"^footnoted\.(\d+)\.verse_part_unattributed_pct$"),
     "Footnoted edition {0} — its verse text alone UNATTRIBUTED"),
    (re.compile(r"^margin\.(\w+)\.p95$"), "Wrong-verse best score, {0} — p95"),
    (re.compile(r"^margin\.(\w+)\.max$"), "Wrong-verse best score, {0} — max"),
    (re.compile(r"^margin\.(\w+)\.over_near_pct$"), "Wrong-verse texts reaching NEAR, {0}"),
    (re.compile(r"^published\.(\w\w)\.n$"), "As published, {0} — quotations"),
    (re.compile(r"^published\.(\w\w)\.detection_pct$"), "As published, {0} — located"),
    (re.compile(r"^published\.(\w\w)\.ref_accuracy_pct$"), "As published, {0} — cited verse"),
    (re.compile(r"^published\.(\w\w)\.attributed_pct$"), "As published, {0} — attributed"),
    (re.compile(r"^(\w\w)\.paragraphs$"), "{0} — paragraphs"),
    (re.compile(r"^(\w\w)\.fp_rate_pct$"), "{0} — paragraphs with any finding"),
    (re.compile(r"^(\w+)\.prose_sentences$"), "`{0}` — prose sentences"),
    (re.compile(r"^(\w+)\.refused$"), "`{0}` — refused (left untouched)"),
    (re.compile(r"^(\w+)\.refusal_rate_pct$"), "`{0}` — refusal rate"),
    (re.compile(r"^(\w+)\.adapted$"), "`{0}` — sentences restructured"),
    (re.compile(r"^(\w+)\.flesch_before$"), "`{0}` — Flesch reading ease, before"),
    (re.compile(r"^(\w+)\.flesch_after$"), "`{0}` — Flesch reading ease, after"),
    (re.compile(r"^(\w+)\.byte_exact$"), "`{0}` — frozen spans byte-exact (1 = yes)"),
]


def _label(key: str) -> str:
    if key in LABELS:
        return LABELS[key][0]
    for rx, fmt in _PATTERNS:
        mm = rx.match(key)
        if mm:
            return fmt.format(*[g or "" for g in mm.groups()])
    return key


def render_markdown(summary: dict) -> str:
    L: list[str] = []
    ix = summary["index"]
    L.append("# MIZAN — evaluation summary")
    L.append("")
    L.append(f"Generated by `{summary['command']}` on {summary['generated_at']}.")
    L.append("")
    L.append("| | |")
    L.append("|---|---|")
    L.append(f"| Index | quranpedia.net dump `{ix['version']}` · "
             f"{ix['translations']} approved translations · "
             f"{', '.join(f'{k} {v}' for k, v in ix['languages'].items())} · "
             f"{ix['renderings']:,} renderings · {ix['arabic_verses']:,} Arabic verses (Tanzil) |")
    match = ("matches the reference index" if summary["fingerprints"]["index_matches_reference"]
             else "**differs from the reference index — numbers are not comparable**")
    L.append(f"| Index fingerprint | `{summary['fingerprints']['index'][:16]}…` ({match}) |")
    cf = summary["fingerprints"].get("corpus")
    cmatch = ("matches the reference corpus"
              if summary["fingerprints"].get("corpus_matches_reference")
              else "**differs from the reference corpus** — some pages changed since "
                   "2026-10-02; see data/real/rebuild_report.json")
    L.append(f"| Real corpus fingerprint | `{cf[:16]}…` ({cmatch}) |" if cf else
             "| Real corpus | not present — real/fp/clarity suites skipped |")
    L.append(f"| Code + inputs fingerprint (cache key) | `{summary['fingerprints']['code'][:16]}…` "
             "— package, harnesses, glossary, embedding manifest |")
    L.append(f"| Runs × seeds | {summary['runs']} × {summary['seeds']} |")
    L.append(f"| Python | {summary['python']} · {summary['platform']} |")
    L.append(f"| Wall time | {summary['seconds']:.0f} s |")
    L.append("")
    L.append("Values are mean ± sample standard deviation across runs. Run k uses "
             f"seed {BASE_SEED} + k; run 0 of the index suite is exactly the "
             "harness's own seed. Deterministic suites must show ± 0 on every "
             "quality metric; only latency may vary.")
    L.append("")

    head = render_headline(summary)
    if head:
        L.append("## Headline")
        L.append("")
        L.extend(head)
        L.append("")

    for name in SUITES:
        s = summary["suites"].get(name)
        if s is None:
            continue
        L.append(f"## {SUITE_TITLES[name]}")
        L.append("")
        if s["status"] != "ok":
            L.append(f"_Skipped:_ {s['reason']}")
            L.append("")
            continue
        det = s.get("kind") or ("deterministic" if s["deterministic"] else "sampled")
        L.append(f"{det} · runs {len(s['seeds'])} · seeds {s['seeds']} · "
                 f"seconds per run {s['seconds']} · params `{json.dumps(s['params'])}`")
        if s.get("nondeterminism_detected"):
            L.append("")
            L.append("**Non-determinism detected** in: "
                     + ", ".join(s["nondeterminism_detected"]))
        L.append("")
        L.append("| Metric | mean ± sd |")
        L.append("|---|---|")
        for k, v in s["metrics"].items():
            L.append(f"| {_label(k)} | {_fmt(k, v)} |")
        L.append("")
        d = s.get("detail") or {}
        if name == "index" and d.get("confused_pairs"):
            L.append("Most frequent wrong-edition answers (run 0). An exact tie means "
                     "the two book entries print the same words for that verse, so no "
                     "text comparison can tell them apart:")
            L.append("")
            L.append("| True edition | Named instead | n | exact ties |")
            L.append("|---|---|---|---|")
            for p in d["confused_pairs"]:
                L.append(f"| {p['true']} ({p['true_id']}) | {p['predicted']} "
                         f"({p['predicted_id']}) | {p['n']} | {p['exact_ties']} |")
            L.append("")
            L.append(f"`margin.*`: {d.get('margin_note', '')}.")
            L.append("")
        if name == "index" and d.get("footnoted_editions"):
            L.append("Footnoted editions (run 0): the verse text WITHOUT the commentary "
                     "the edition stores after a `____` separator, fed back to MIZAN:")
            L.append("")
            L.append("| Edition | lang | verses with commentary | sampled | → itself | "
                     "→ another edition | UNATTRIBUTED | editions in language |")
            L.append("|---|---|---|---|---|---|---|---|")
            for f in d["footnoted_editions"]:
                L.append(f"| {f['title']} ({f['book_id']}) | {f['lang']} | "
                         f"{f['verses_with_commentary']:,} | {f['sampled']} | "
                         f"{f['attributed_to_itself']} | {f['attributed_to_another_edition']} | "
                         f"{f['unattributed']} | {f['editions_in_language']} |")
            L.append("")
        if name == "clarity" and d.get("refusal_counts"):
            rc = d["refusal_counts"].get("curious", {})
            L.append("Refusal reasons at `curious`: "
                     + " · ".join(f"`{k}` {v}" for k, v in rc.items()))
            L.append("")
            L.append(f"Method: {d.get('method', '')}")
            L.append("")
        if name == "terms" and d.get("glossary"):
            g = d["glossary"]
            L.append(f"Glossary in use: source `{g.get('source')}`, {g.get('terms')} terms "
                     "(see docs/TERMS.md).")
            if d.get("dictionary_definitions"):
                L.append("")
                L.append(f"Dictionary-definitions calibration {d['dictionary_definitions']}.")
            else:
                L.append("The `calib.dictionary_definitions` rows need الجمهرة's own "
                         "definitions, which are kept local (not redistributed): a clean "
                         "clone reports this calibration as skipped.")
            L.append("")
        if name == "real" and d.get("caveats"):
            L.append("Caveats from the harness:")
            L.append("")
            for c in d["caveats"]:
                L.append(f"- {c}")
            L.append("")
    L.append("---")
    L.append("Re-derive: `make setup && make real-corpus && make eval` "
             "(see README → Evaluation).")
    return "\n".join(L) + "\n"


def render_headline(summary: dict) -> list[str]:
    rows = []
    for suite, key, nkey in HEADLINE:
        s = summary["suites"].get(suite)
        if not s or s.get("status") != "ok" or key not in s["metrics"]:
            continue
        n = ""
        if nkey and nkey in s["metrics"]:
            nv = s["metrics"][nkey]["mean"]
            n = f"{nv:,.0f}"
        rows.append(f"| {_label(key)} | {_fmt(key, s['metrics'][key])} | {n} | `{suite}` |")
    if not rows:
        return []
    return ["| Metric | mean ± sd | n | suite |", "|---|---|---|---|"] + rows


def update_readme(summary: dict) -> bool:
    if not os.path.exists(README):
        return False
    text = open(README, encoding="utf-8").read()
    if README_START not in text or README_END not in text:
        return False
    ix = summary["index"]
    block = [README_START, "",
             f"_Generated from `results/SUMMARY.md` by `{summary['command']}` "
             f"on {summary['generated_at'][:10]} — index `{ix['version']}`, "
             f"{summary['runs']} runs, Python {summary['python'].split()[0]}._", ""]
    block += render_headline(summary) or ["_No suite produced results._"]
    skipped = [n for n, s in summary["suites"].items() if s.get("status") != "ok"]
    if skipped:
        block += ["", "Skipped in this run: " + ", ".join(
            f"`{n}`" for n in skipped) + " (see `results/SUMMARY.md` for why)."]
    block += ["", README_END]
    new = (text[:text.index(README_START)] + "\n".join(block)
           + text[text.index(README_END) + len(README_END):])
    if new != text:
        with open(README, "w", encoding="utf-8") as fh:
            fh.write(new)
    return True


# ===========================================================================
# CLI
# ===========================================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m mizan.eval",
        description="Re-derive every MIZAN number with fixed seeds.")
    ap.add_argument("--suite", default="all",
                    help="all, or comma-separated: " + ", ".join(SUITES))
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--semantic-runs", type=int, default=1,
                    help="runs for the semantic suite (default 1: it re-measures "
                         "the whole corpus twice per run, with and without the "
                         "model, and is deterministic for a pinned revision)")
    ap.add_argument("--quick", action="store_true",
                    help="small samples for a smoke test; writes to results/quick/")
    ap.add_argument("--fresh", action="store_true", help="ignore cached runs")
    ap.add_argument("--out", default=None, help="output directory (default results/)")
    ap.add_argument("--update-readme", action="store_true",
                    help="splice the headline table into README.md between markers")
    ap.add_argument("--fingerprint", action="store_true",
                    help="print the index fingerprint and exit")
    ap.add_argument("--check-index", action="store_true",
                    help="exit non-zero unless the index matches the reference")
    ap.add_argument("--render", action="store_true",
                    help="re-render SUMMARY.md (and README with --update-readme) "
                         "from an existing summary.json; measures nothing")
    args = ap.parse_args(argv)

    if args.fingerprint or args.check_index:
        if not os.path.exists(DB):
            print(f"no index at {DB} — run `make setup`", file=sys.stderr)
            return 2
        fp = index_fingerprint()
        facts = index_facts()
        print(json.dumps({"fingerprint": fp, **facts,
                          "matches_reference": fp == REFERENCE_INDEX_FINGERPRINT},
                         ensure_ascii=False))
        if args.check_index and fp != REFERENCE_INDEX_FINGERPRINT:
            print("index fingerprint differs from the reference index the "
                  "published numbers were measured on", file=sys.stderr)
            return 1
        return 0

    if args.render:
        out_dir = os.path.normpath(args.out or RESULTS)
        summary = json.load(open(os.path.join(out_dir, "summary.json"), encoding="utf-8"))
        with open(os.path.join(out_dir, "SUMMARY.md"), "w", encoding="utf-8") as fh:
            fh.write(render_markdown(summary))
        if args.update_readme:
            update_readme(summary)
        _log(f"re-rendered {os.path.relpath(out_dir, ROOT)}/SUMMARY.md")
        return 0

    if not os.path.exists(DB):
        print(f"no index at {DB} — run `make setup` first", file=sys.stderr)
        return 2

    wanted = list(SUITES) if args.suite == "all" else [
        s.strip() for s in args.suite.split(",") if s.strip()]
    bad = [s for s in wanted if s not in SUITES]
    if bad:
        ap.error(f"unknown suite(s) {bad}; choose from {SUITES} or all")
    if args.runs < 1:
        ap.error("--runs must be >= 1")

    out_dir = args.out or os.path.join(RESULTS, "quick" if args.quick else "")
    out_dir = os.path.normpath(out_dir)
    cache_dir = os.path.join(out_dir, "cache")
    os.makedirs(cache_dir, exist_ok=True)

    t0 = time.time()
    _log("fingerprinting index, code and corpus ...")
    fps = {"index": index_fingerprint(), "code": code_fingerprint(),
           "corpus": corpus_fingerprint()}
    facts = index_facts()
    _log(f"index {facts['version']} · {facts['translations']} translations · "
         f"{facts['renderings']:,} renderings · fingerprint {fps['index'][:16]}"
         + ("" if fps["index"] == REFERENCE_INDEX_FINGERPRINT
            else "  (DIFFERS from the reference index)"))

    suites: dict[str, dict] = {}
    for s in wanted:
        _log(f"suite {s}")
        n = min(args.runs, args.semantic_runs) if s == "semantic" else args.runs
        suites[s] = run_suite(s, n, args.quick, args.fresh, cache_dir, fps)

    interp = "python3"
    if sys.prefix != sys.base_prefix:                 # running inside a venv
        exe = os.path.abspath(sys.executable)
        interp = os.path.relpath(exe, ROOT) if exe.startswith(ROOT) else exe
    cmd = f"{interp} -m mizan.eval " + " ".join(argv if argv is not None else sys.argv[1:])
    summary = {
        "mizan_eval": 1,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": cmd.strip(),
        "python": sys.version.split()[0] + " " + platform.python_implementation(),
        "platform": f"{platform.system()} {platform.machine()}",
        "runs": args.runs,
        "seeds": [BASE_SEED + k for k in range(args.runs)],
        "quick": args.quick,
        "index": facts,
        "fingerprints": {
            "index": fps["index"],
            "index_reference": REFERENCE_INDEX_FINGERPRINT,
            "index_matches_reference": fps["index"] == REFERENCE_INDEX_FINGERPRINT,
            "code": fps["code"], "corpus": fps["corpus"],
            "corpus_reference": REFERENCE_CORPUS_FINGERPRINT,
            "corpus_matches_reference": fps["corpus"] == REFERENCE_CORPUS_FINGERPRINT},
        "thresholds": {"MATCH": T_MATCH, "NEAR": T_NEAR},
        "seconds": round(time.time() - t0, 1),
        "suites": suites,
    }
    # Merge with a previous summary so `--suite index` refreshes one suite
    # without discarding the others' last results.
    jpath = os.path.join(out_dir, "summary.json")
    if args.suite != "all" and os.path.exists(jpath):
        try:
            prev = json.load(open(jpath, encoding="utf-8"))
            if prev.get("fingerprints", {}).get("index") == fps["index"]:
                for k, v in prev.get("suites", {}).items():
                    summary["suites"].setdefault(k, v)
        except (ValueError, OSError):
            pass
    summary["suites"] = {k: summary["suites"][k] for k in SUITES if k in summary["suites"]}

    with open(jpath, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    md = render_markdown(summary)
    with open(os.path.join(out_dir, "SUMMARY.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    print(md)
    _log(f"written {os.path.relpath(jpath, ROOT)} and "
         f"{os.path.relpath(os.path.join(out_dir, 'SUMMARY.md'), ROOT)}")
    if args.update_readme and not args.quick:
        if update_readme(summary):
            _log("README.md results table updated")
        else:
            _log("README.md has no results markers; not updated")
    nd = [n for n, s in suites.items() if s.get("nondeterminism_detected")]
    return 3 if nd else 0


if __name__ == "__main__":
    raise SystemExit(main())
