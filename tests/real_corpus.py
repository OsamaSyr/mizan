#!/usr/bin/env python3
"""
MIZAN — measurement against the real-world corpus.

What makes this different from every other test in the repo
-----------------------------------------------------------
The existing suites feed the index's own text back to itself. That is the
right way to prove the comparison mechanism and the calibration, and it is
how the 0.008% false-alarm figure was obtained. What it cannot tell you is
whether the thing survives contact with published prose: curly quotes,
ellipses dropped into the middle of a verse, footnote markers glued to the
last word, "God" where the index says "Allah", partial verses, and a citation
convention that changes with every publisher.

This harness runs MIZAN over text nobody involved in building MIZAN wrote, and
reports what happened.

Ground truth
------------
The publisher's own printed reference. When a page prints "(Quran 2:255)" next
to a quotation, the publisher is asserting which verse that is. We take that
assertion at face value and ask whether MIZAN resolves the same verse. Items
with no printed reference still count toward the detection rate but cannot
count toward reference accuracy, and are reported separately rather than
quietly folded in.

The number that matters most
----------------------------
The false-positive probe. A gate that fires on ordinary prose is worse than no
gate, because every spurious referral costs a reviewer's time and trains them
to ignore the tool. We run MIZAN over paragraphs of the SAME authors' prose
that contain no quotation, and count what comes back.

What this harness deliberately does NOT do
------------------------------------------
It does not report how often publishers misquote. We cannot measure that, we
did not try, and the corpus is not sampled in a way that would support it.
UNATTRIBUTED here means "matches none of the translations in our index" —
a statement about index coverage as much as about the text.

Usage
-----
    python3 tests/real_corpus.py
    python3 tests/real_corpus.py --limit 50        # quick pass
    python3 tests/real_corpus.py --negatives 30
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from mizan.engine import Mizan, sim                               # noqa: E402
from mizan.report import (MATCH, NEAR, NO_APPROVED_TRANSLATION,   # noqa: E402
                          UNATTRIBUTED, UNRESOLVED, build_finding,
                          build_report)

CORPUS = os.path.join(ROOT, "data", "real", "corpus.jsonl")
NEGATIVES = os.path.join(ROOT, "data", "real", "negatives.jsonl")
MANIFEST = os.path.join(ROOT, "data", "real", "manifest.json")
RESULTS = os.path.join(HERE, "results_real.json")

STATES = [MATCH, NEAR, UNATTRIBUTED, NO_APPROVED_TRANSLATION, UNRESOLVED]


def load(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


# ---------------------------------------------------------------------------
# Detector cache
# ---------------------------------------------------------------------------
_DETECTORS: dict[str, object] = {}


def detector_for(m: Mizan, lang: str):
    """One SpanDetector per language; building the index costs a second."""
    if lang not in _DETECTORS:
        from mizan.detect import SpanDetector
        try:
            _DETECTORS[lang] = SpanDetector(m, lang)
        except ValueError:
            _DETECTORS[lang] = None        # no approved translation indexed
    return _DETECTORS[lang]


# ---------------------------------------------------------------------------
# Pass 1 — quotations
# ---------------------------------------------------------------------------
def measure_quotations(m: Mizan, rows: list[dict],
                       with_printed_ref: bool = False) -> tuple[list[dict], dict]:
    """
    Run MIZAN over each collected quotation, in its published form.

    Two conditions, and the difference between them is itself a finding.

    `with_printed_ref=False` (the hard condition) feeds only the quoted span,
    with the publisher's citation stripped off. This is deliberately harsher
    than reality: MIZAN's detector has three tiers and the cheapest, tier (a),
    is simply reading a printed "(Quran 2:255)". Removing it measures the
    lexical tier alone, which is the part that has to work when an author
    quotes without citing.

    `with_printed_ref=True` (the realistic condition) puts the citation back,
    which is how the text actually appears on the page. A publisher running
    MIZAN pastes the paragraph as published; they do not delete their own
    footnotes first.

    Reporting only the first would understate the product; reporting only the
    second would overstate the hard part. So we report both.
    """
    out: list[dict] = []
    for r in rows:
        lang = r["lang"]
        text = r["published_text"]
        if with_printed_ref and r.get("printed_ref"):
            # Restored in a neutral, machine-readable form rather than the
            # publisher's own wording, so that one host's citation style does
            # not become the thing being measured.
            text = f"{text} (Quran {r['printed_ref']})"
        det = detector_for(m, lang)

        t0 = time.perf_counter()
        if det is None:
            # Fourth state: resolved as scripture, but we index no approved
            # translation for this language. Not a failure of detection.
            spans = []
            state, score, title, ref = NO_APPROVED_TRANSLATION, 0.0, None, None
            tier = None
            top_title = top_score = None
        else:
            spans = det.find(text)
            if spans:
                sp = max(spans, key=lambda s: getattr(s, "score", 0.0))
                f = build_finding(m, sp.text, sp.surah, sp.ayah, lang,
                                  start_word=sp.start_word,
                                  end_word=sp.end_word, tier=sp.tier)
                state, score = f.state, f.score
                title, ref, tier = f.attributed_title, f.ref, f.tier
                # For an UNATTRIBUTED item, surface the best approved
                # translation anyway: a human needs to see how close it came
                # before concluding anything about the text.
                att = m.attribute(sp.text, sp.surah, sp.ayah, lang)
                top_score = att.get("score", 0.0)
                if att.get("attributed_to"):
                    top_title = m.books[att["attributed_to"]]["title"]
                else:
                    # engine.attribute() withholds the title on UNATTRIBUTED,
                    # by design — it will not name a translation it is not
                    # attributing to. For review purposes we want it anyway:
                    # "closest was Pickthall at 0.71" is exactly what tells a
                    # human whether the index is simply missing the source.
                    bid, _ = max(
                        ((b, sim(sp.text, m.verse(b, sp.surah, sp.ayah) or ""))
                         for b in m.editions(lang)),
                        key=lambda x: x[1], default=(None, 0.0))
                    top_title = m.books[bid]["title"] if bid else None
            else:
                state, score, title, ref, tier = UNRESOLVED, 0.0, None, None, None
                top_title = top_score = None
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        printed = r.get("printed_ref")
        ref_correct = None
        if printed and ref and ref != "?":
            ref_correct = (ref == printed)

        out.append({
            "id": r["id"],
            "lang": lang,
            "source_host": r["source_host"],
            "source_url": r["source_url"],
            "detected": bool(spans) or state == NO_APPROVED_TRANSLATION,
            "state": state,
            "score": score,
            "tier": tier,
            "resolved_ref": ref,
            "printed_ref": printed,
            "ref_correct": ref_correct,
            "attributed_title": title,
            "top_approved_title": top_title,
            "top_approved_score": top_score,
            "published_text": text[:400],
            "latency_ms": round(elapsed_ms, 1),
        })

    return out, summarise(out)


def summarise(items: list[dict]) -> dict:
    """Overall and per-language rollup."""
    def roll(rows: list[dict]) -> dict:
        n = len(rows)
        det = sum(1 for r in rows if r["detected"])
        gt = [r for r in rows if r["ref_correct"] is not None]
        gt_ok = sum(1 for r in gt if r["ref_correct"])
        states = {s: sum(1 for r in rows if r["state"] == s) for s in STATES}
        lat = [r["latency_ms"] for r in rows] or [0.0]
        # Attributed = MATCH only. Until 2026-10-04 NEAR counted too; NEAR now
        # refers the document (its words differ from every approved text), so
        # it is no longer a claim that the words came from a translation.
        attributed = states[MATCH]
        return {
            "n": n,
            "detected": det,
            "detection_rate_pct": pct(det, n),
            "with_printed_ref": len(gt),
            "ref_correct": gt_ok,
            "ref_accuracy_pct": pct(gt_ok, len(gt)),
            "states": states,
            "state_pct": {s: pct(v, n) for s, v in states.items()},
            "attributed_to_approved": attributed,
            "attribution_rate_pct": pct(attributed, n),
            "latency_ms": {
                "mean": round(statistics.mean(lat), 1),
                "median": round(statistics.median(lat), 1),
                "p95": round(sorted(lat)[int(0.95 * (len(lat) - 1))], 1),
                "max": round(max(lat), 1),
            },
        }

    langs = sorted({r["lang"] for r in items})
    return {
        "overall": roll(items),
        "by_language": {lg: roll([r for r in items if r["lang"] == lg])
                        for lg in langs},
    }


# ---------------------------------------------------------------------------
# Pass 2 — the false-positive probe
# ---------------------------------------------------------------------------
def measure_negatives(m: Mizan, rows: list[dict]) -> tuple[list[dict], dict]:
    """
    Run MIZAN over prose that contains no Quranic quotation.

    This is the number that matters most. The corpus builder already filtered
    these paragraphs hard — anything carrying quote glyphs, a verse reference
    or a bare "n:m" pair was excluded — so a finding here is a genuine false
    positive, not a mislabelled quotation.

    A spurious finding is expensive twice over: it wastes a reviewer and it
    teaches them the tool cries wolf. One of those costs is recoverable.
    """
    out: list[dict] = []
    for r in rows:
        lang, text = r["lang"], r["text"]
        det = detector_for(m, lang)
        t0 = time.perf_counter()
        spans = det.find(text) if det is not None else []
        elapsed = (time.perf_counter() - t0) * 1000.0

        found = []
        for sp in spans:
            f = build_finding(m, sp.text, sp.surah, sp.ayah, lang,
                              start_word=sp.start_word, end_word=sp.end_word,
                              tier=sp.tier)
            found.append({"ref": f.ref, "state": f.state, "score": f.score,
                          "tier": f.tier, "span": sp.text[:200]})
        out.append({
            "id": r["id"], "lang": lang, "source_host": r["source_host"],
            "n_findings": len(found), "findings": found,
            "words": len(text.split()),
            "latency_ms": round(elapsed, 1),
            "text": text[:300],
        })

    n = len(out)
    dirty = [r for r in out if r["n_findings"]]

    # The severity split that the headline rate hides.
    #
    # A finding that lands in UNATTRIBUTED costs a reviewer one glance: the
    # tool is saying "this matches nothing I have indexed, look at it". A
    # finding that lands in MATCH or NEAR is a different animal entirely —
    # there the tool has NAMED an approved translation and asserted the prose
    # came from it, which is a false attribution and the one error this
    # system must never make.
    #
    # Counting both as "false positives" at one rate would obscure which kind
    # is happening, so we report them apart.
    #
    # Since 2026-10-04 only MATCH is an attribution claim: NEAR refers the
    # document and names its closest approved translation for comparison, not
    # as the source. A NEAR on prose is therefore a referral — counted apart
    # (`near_referrals`) so the change of definition stays visible.
    false_attribution = [
        {"id": r["id"], "lang": r["lang"], "finding": f, "text": r["text"]}
        for r in dirty for f in r["findings"] if f["state"] == MATCH
    ]
    near_referral = sum(1 for r in dirty for f in r["findings"] if f["state"] == NEAR)
    spurious_referral = sum(1 for r in dirty for f in r["findings"]
                            if f["state"] != MATCH)

    by_lang: dict[str, dict] = {}
    for lg in sorted({r["lang"] for r in out}):
        sub = [r for r in out if r["lang"] == lg]
        bad = [r for r in sub if r["n_findings"]]
        by_lang[lg] = {
            "paragraphs": len(sub),
            "paragraphs_with_a_finding": len(bad),
            "spurious_findings": sum(r["n_findings"] for r in bad),
            "false_positive_rate_pct": pct(len(bad), len(sub)),
        }

    # Span width of every spurious finding.
    #
    # These cluster hard against MIN_SPAN_WORDS. A six-word window of ordinary
    # religious prose ("the Books of Abraham, the Torah of Moses,") shares
    # enough function words and enough theological vocabulary with some verse
    # somewhere in 6,236 of them that a blended similarity clears threshold by
    # coincidence. The floor exists to stop exactly this and is simply set too
    # low for real prose. Reported as a distribution rather than described in
    # prose so the next person can see where to move it.
    widths = sorted(len(f["span"].split()) for r in dirty for f in r["findings"])
    width_stats = {
        "min": widths[0] if widths else None,
        "median": statistics.median(widths) if widths else None,
        "max": widths[-1] if widths else None,
        "at_or_below_8_words": sum(1 for x in widths if x <= 8),
        "total": len(widths),
    }

    lat = [r["latency_ms"] for r in out] or [0.0]
    return out, {
        "paragraphs": n,
        "paragraphs_with_a_finding": len(dirty),
        "spurious_span_widths": width_stats,
        "spurious_findings": sum(r["n_findings"] for r in dirty),
        "false_positive_rate_pct": pct(len(dirty), n),
        # Severity split — see the comment above.
        "false_attributions": len(false_attribution),
        "false_attribution_rate_pct": pct(len(false_attribution), n),
        "spurious_referrals": spurious_referral,
        "near_referrals": near_referral,
        "false_attribution_detail": false_attribution,
        "by_language": by_lang,
        "latency_ms": {"mean": round(statistics.mean(lat), 1),
                       "p95": round(sorted(lat)[int(0.95 * (len(lat) - 1))], 1)},
    }


# ---------------------------------------------------------------------------
# Pass 2b — where the printed-reference tier goes
# ---------------------------------------------------------------------------
def measure_reference_tier(m: Mizan, rows: list[dict]) -> dict:
    """
    Ask tier (a) directly, then check whether its answer survived to the end.

    This diagnostic exists because the aggregate numbers hid something. When
    a publisher prints "(Quran 2:22)" beside a quotation, tier (a) reads it
    and locates the right verse essentially every time. But `find()` scores
    that span by text similarity and `_resolve_overlaps` drops anything below
    SPAN_THRESHOLD — and a PARTIAL quotation ("…and the sky a ceiling…") is
    by construction dissimilar to the whole verse, so it scores low and is
    discarded. A lexical match on a different verse can then score higher and
    win the slot.

    The effect only shows up on real text. Feeding whole verses back from the
    index produces scores near 1.0, where the threshold never binds. Partial
    quotation is the norm in published da'wah writing and nearly absent from
    a self-referential test set, which is precisely the gap this corpus was
    built to expose.

    We measure it; we do not fix it here. detect.py is owned elsewhere, and a
    measurement harness that edits the thing it measures is worth nothing.
    """
    from mizan.detect import SPAN_THRESHOLD

    per_lang: dict[str, dict] = {}
    examples: list[dict] = []

    for r in rows:
        printed = r.get("printed_ref")
        if not printed:
            continue
        lang = r["lang"]
        det = detector_for(m, lang)
        if det is None:
            continue
        text = f"{r['published_text']} (Quran {printed})"
        words = text.split()

        try:
            raw = det._from_references(text, words)
        except Exception:                                  # noqa: BLE001
            continue
        if not raw:
            continue

        d = per_lang.setdefault(lang, {
            "cited_items": 0, "tier_a_fired": 0, "tier_a_correct": 0,
            "tier_a_correct_and_survived": 0,
            "tier_a_correct_but_dropped": 0, "scores": []})
        d["cited_items"] += 1
        d["tier_a_fired"] += 1

        sp = raw[0]
        correct = f"{sp.surah}:{sp.ayah}" == printed
        d["scores"].append(round(sp.score, 3))
        if not correct:
            continue
        d["tier_a_correct"] += 1

        final = {f"{s.surah}:{s.ayah}" for s in det.find(text)}
        if printed in final:
            d["tier_a_correct_and_survived"] += 1
        else:
            d["tier_a_correct_but_dropped"] += 1
            examples.append({
                "lang": lang, "printed_ref": printed,
                "tier_a_score": round(sp.score, 3),
                "threshold": SPAN_THRESHOLD,
                "resolved_instead": sorted(final) or None,
                "published_text": r["published_text"][:220],
                "source_url": r["source_url"],
            })

    tot = {"cited_items": 0, "tier_a_fired": 0, "tier_a_correct": 0,
           "tier_a_correct_and_survived": 0, "tier_a_correct_but_dropped": 0}
    for d in per_lang.values():
        for k in tot:
            tot[k] += d[k]
        d["median_score"] = (round(statistics.median(d["scores"]), 3)
                             if d["scores"] else None)
        d.pop("scores", None)

    tot["tier_a_precision_pct"] = pct(tot["tier_a_correct"], tot["tier_a_fired"])
    tot["survival_pct"] = pct(tot["tier_a_correct_and_survived"],
                              tot["tier_a_correct"])
    return {"overall": tot, "by_language": per_lang,
            "span_threshold": SPAN_THRESHOLD,
            "dropped_examples": examples}


# ---------------------------------------------------------------------------
# Pass 3 — whole-document check
# ---------------------------------------------------------------------------
def measure_documents(m: Mizan, rows: list[dict], limit: int = 12) -> dict:
    """
    Latency for a realistic unit of work: one article, checked end to end.

    Per-quotation latency understates the cost a publisher actually pays, who
    submits a whole page. Reconstructed here by concatenating everything we
    collected from one URL in document order.
    """
    from mizan.report import check_document

    by_url: dict[str, list[dict]] = {}
    for r in rows:
        by_url.setdefault(r["source_url"], []).append(r)

    picked = sorted(by_url.items(), key=lambda kv: -len(kv[1]))[:limit]
    docs = []
    for url, items in picked:
        lang = items[0]["lang"]
        if detector_for(m, lang) is None:
            continue
        parts = []
        for it in items:
            parts.append(it.get("context_before", ""))
            parts.append(it["published_text"])
            parts.append(it.get("context_after", ""))
        text = "\n\n".join(p for p in parts if p)

        t0 = time.perf_counter()
        rep = check_document(m, text, lang, detector=detector_for(m, lang))
        elapsed = (time.perf_counter() - t0) * 1000.0
        docs.append({
            "source_url": url, "lang": lang,
            "words": len(text.split()),
            "verdict": rep.verdict, "counts": rep.counts,
            "n_findings": len(rep.findings),
            "latency_ms": round(elapsed, 1),
        })

    lat = [d["latency_ms"] for d in docs] or [0.0]
    wps = [d["words"] for d in docs] or [0]
    return {
        "documents": len(docs),
        "mean_words": round(statistics.mean(wps), 1),
        "latency_ms": {"mean": round(statistics.mean(lat), 1),
                       "median": round(statistics.median(lat), 1),
                       "max": round(max(lat), 1)},
        "verdicts": {v: sum(1 for d in docs if d["verdict"] == v)
                     for v in {d["verdict"] for d in docs}},
        "detail": docs,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def bar(v: float, width: int = 28) -> str:
    return "#" * int(round(v / 100 * width))


def print_summary(res: dict) -> None:
    q = res["quotations"]
    qr = res["quotations_with_printed_ref"]
    neg = res["false_positive_probe"]
    o, orf = q["overall"], qr["overall"]
    w = 64

    print("\n" + "=" * w)
    print("MIZAN — measured against published prose")
    print("=" * w)
    src = res["corpus"]
    print(f"corpus          {src['quotations']} quotations · "
          f"{src['negative_paragraphs']} no-quotation paragraphs")
    print(f"languages       {', '.join(f'{k}={v}' for k, v in sorted(src['by_language'].items()))}")
    print(f"hosts           {', '.join(sorted(src['by_host']))}")
    print(f"index           {res['index']['translations']} approved "
          f"translations · version {res['index']['version']}")

    print("\n" + "-" * w)
    print("DETECTION — did MIZAN locate a quotation at all")
    print("  (a) citation STRIPPED  — lexical tier only, harder than reality")
    print("  (b) citation IN PLACE  — the text as actually published")
    print("-" * w)
    print(f"  {'lang':<8}{'(a) stripped':>18}{'(b) as published':>20}")
    print(f"  {'ALL':<8}{o['detected']:>8}/{o['n']:<3}"
          f"{o['detection_rate_pct']:>5.1f}%"
          f"{orf['detected']:>9}/{orf['n']:<3}{orf['detection_rate_pct']:>6.1f}%")
    for lg in sorted(q["by_language"]):
        a, b = q["by_language"][lg], qr["by_language"][lg]
        print(f"  {lg:<8}{a['detected']:>8}/{a['n']:<3}"
              f"{a['detection_rate_pct']:>5.1f}%"
              f"{b['detected']:>9}/{b['n']:<3}{b['detection_rate_pct']:>6.1f}%")

    print("\n" + "-" * w)
    print("REFERENCE ACCURACY — same verse the publisher cited")
    print("ground truth = the publisher's own printed citation")
    print("of the items MIZAN located AND the publisher had cited")
    print("-" * w)
    print(f"  {'lang':<8}{'(a) stripped':>18}{'(b) as published':>20}")
    print(f"  {'ALL':<8}{o['ref_correct']:>8}/{o['with_printed_ref']:<3}"
          f"{o['ref_accuracy_pct']:>5.1f}%"
          f"{orf['ref_correct']:>9}/{orf['with_printed_ref']:<3}"
          f"{orf['ref_accuracy_pct']:>6.1f}%")
    for lg in sorted(q["by_language"]):
        a, b = q["by_language"][lg], qr["by_language"][lg]
        if a["with_printed_ref"] or b["with_printed_ref"]:
            print(f"  {lg:<8}{a['ref_correct']:>8}/{a['with_printed_ref']:<3}"
                  f"{a['ref_accuracy_pct']:>5.1f}%"
                  f"{b['ref_correct']:>9}/{b['with_printed_ref']:<3}"
                  f"{b['ref_accuracy_pct']:>6.1f}%")

    print("\n" + "-" * w)
    print("STATE DISTRIBUTION — citation in place (the realistic condition)")
    print("UNATTRIBUTED = matches none of the indexed approved translations.")
    print("It is a statement about index coverage, not about the text.")
    print("-" * w)
    print(f"  {'lang':<8}{'n':>5}" + "".join(f"{s[:6]:>8}" for s in STATES))
    print(f"  {'ALL':<8}{orf['n']:>5}" +
          "".join(f"{orf['states'][s]:>8}" for s in STATES))
    for lg, r in sorted(qr["by_language"].items()):
        print(f"  {lg:<8}{r['n']:>5}" +
              "".join(f"{r['states'][s]:>8}" for s in STATES))

    rt = res["reference_tier_diagnostic"]
    t = rt["overall"]
    if t["tier_a_fired"]:
        print("\n" + "-" * w)
        print("PRINTED-REFERENCE TIER — where the publisher's own citation goes")
        print("-" * w)
        print(f"  items where the publisher printed a citation   "
              f"{t['tier_a_fired']:>4}")
        print(f"  tier (a) located the cited verse               "
              f"{t['tier_a_correct']:>4}   "
              f"{t['tier_a_precision_pct']:>5.1f}%")
        print(f"  ...and survived to the final report            "
              f"{t['tier_a_correct_and_survived']:>4}   "
              f"{t['survival_pct']:>5.1f}%")
        print(f"  ...dropped below SPAN_THRESHOLD={rt['span_threshold']}        "
              f"       {t['tier_a_correct_but_dropped']:>4}")
        if rt["dropped_examples"]:
            print("  Dropped because a PARTIAL quotation scores low against the")
            print("  whole verse, even though the citation named it correctly:")
            for ex in rt["dropped_examples"][:5]:
                print(f"    [{ex['lang']}] cited {ex['printed_ref']:<8} "
                      f"score {ex['tier_a_score']:.3f} -> "
                      f"reported {ex['resolved_instead'] or 'nothing'}")
                print(f"          {ex['published_text'][:80]}")

    print("\n" + "-" * w)
    print("*** FALSE-POSITIVE PROBE — prose with NO quotation in it ***")
    print("-" * w)
    print(f"  paragraphs tested            {neg['paragraphs']}")
    print(f"  paragraphs with a finding    {neg['paragraphs_with_a_finding']}")
    print(f"  spurious findings            {neg['spurious_findings']}")
    print(f"  false-positive rate          {neg['false_positive_rate_pct']}%")
    print()
    print("  Severity split — these are not the same error:")
    print(f"    flagged for review (NEAR/UNATTR)    "
          f"{neg['spurious_referrals']:>4}   costs a reviewer a glance "
          f"({neg.get('near_referrals', 0)} of them NEAR)")
    print(f"    FALSE ATTRIBUTION (MATCH)           "
          f"{neg['false_attributions']:>4}   "
          f"{neg['false_attribution_rate_pct']}%  <- the error that matters")
    sw = neg.get("spurious_span_widths") or {}
    if sw.get("total"):
        from mizan.detect import MIN_SPAN_WORDS
        print()
        print(f"  Spurious spans cluster at the MIN_SPAN_WORDS={MIN_SPAN_WORDS} floor:")
        print(f"    span width  min {sw['min']}  median {sw['median']}  "
              f"max {sw['max']}")
        print(f"    {sw['at_or_below_8_words']}/{sw['total']} are 8 words or "
              f"fewer — short windows of ordinary religious prose")
        print(f"    collide with some verse among 6,236 by coincidence.")
    for lg, r in sorted(neg["by_language"].items()):
        print(f"    {lg:<6} {r['paragraphs_with_a_finding']:>3}/"
              f"{r['paragraphs']:<4} {r['false_positive_rate_pct']:>5.1f}%")

    print("\n" + "-" * w)
    print("LATENCY")
    print("-" * w)
    print(f"  per quotation   mean {orf['latency_ms']['mean']:>8.1f} ms   "
          f"p95 {orf['latency_ms']['p95']:>8.1f} ms")
    d = res["documents"]
    if d["documents"]:
        print(f"  per document    mean {d['latency_ms']['mean']:>8.1f} ms   "
              f"max {d['latency_ms']['max']:>8.1f} ms "
              f"({d['mean_words']:.0f} words avg, n={d['documents']})")

    ua = res["unattributed_review"]
    if ua:
        print("\n" + "-" * w)
        print(f"UNATTRIBUTED — closest approved translation ({len(ua)} items, "
              f"showing up to 12)")
        print("Shown so a human can judge how near the miss was.")
        print("-" * w)
        for r in ua[:12]:
            print(f"  [{r['lang']}] {str(r['resolved_ref']):<9} "
                  f"score {r['top_approved_score'] or 0:.3f}  "
                  f"closest: {(r['top_approved_title'] or '-')[:38]}")
            print(f"        {r['published_text'][:92]}")
    print("=" * w)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0,
                    help="cap quotations measured (0 = all)")
    ap.add_argument("--negatives", type=int, default=120,
                    help="paragraphs for the false-positive probe. The brief "
                         "asks for 30; we run more by default because this is "
                         "the number that matters most and 30 gives a "
                         "confidence interval too wide to act on.")
    args = ap.parse_args()

    rows = load(CORPUS)
    negs = load(NEGATIVES)
    if not rows:
        print(f"No corpus at {CORPUS}. Run scripts/collect_corpus.py first.",
              file=sys.stderr)
        return 1
    if args.limit:
        rows = rows[:args.limit]

    # Spread the negative sample over languages and hosts rather than taking
    # the first N, which would all come from one or two articles.
    by_lang: dict[str, list[dict]] = {}
    for r in negs:
        by_lang.setdefault(r["lang"], []).append(r)
    picked: list[dict] = []
    i = 0
    while len(picked) < args.negatives and any(
            i < len(v) for v in by_lang.values()):
        for lg in sorted(by_lang):
            if i < len(by_lang[lg]) and len(picked) < args.negatives:
                picked.append(by_lang[lg][i])
        i += 1

    m = Mizan()
    print(f"index {m.version} · {len(m.books)} approved translations",
          file=sys.stderr)
    print(f"measuring {len(rows)} quotations, {len(picked)} negative "
          f"paragraphs...", file=sys.stderr)

    items, qsum = measure_quotations(m, rows, with_printed_ref=False)
    items_ref, qsum_ref = measure_quotations(m, rows, with_printed_ref=True)
    negitems, nsum = measure_negatives(m, picked)
    reftier = measure_reference_tier(m, rows)
    docs = measure_documents(m, rows)

    manifest = {}
    if os.path.exists(MANIFEST):
        manifest = json.load(open(MANIFEST, encoding="utf-8"))

    res = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "corpus": {
            "quotations": len(rows),
            "negative_paragraphs": len(picked),
            "by_language": {lg: sum(1 for r in rows if r["lang"] == lg)
                            for lg in sorted({r["lang"] for r in rows})},
            "by_host": {h: sum(1 for r in rows if r["source_host"] == h)
                        for h in sorted({r["source_host"] for r in rows})},
            "retrieved_at": manifest.get("retrieved_at"),
        },
        "index": {"version": m.version, "translations": len(m.books),
                  "languages": {k: len(v) for k, v in m.languages().items()}},
        # The hard condition: citation stripped, lexical tier only.
        "quotations": qsum,
        # The realistic condition: the citation the publisher actually printed
        # is left in place, so the printed-reference tier can fire.
        "quotations_with_printed_ref": qsum_ref,
        "reference_tier_diagnostic": reftier,
        "false_positive_probe": nsum,
        "documents": docs,
        "unattributed_review": [r for r in items_ref
                                if r["state"] == UNATTRIBUTED],
        "items": items,
        "items_with_printed_ref": items_ref,
        "negative_items": negitems,
        "caveats": [
            "UNATTRIBUTED means the rendering matches none of the approved "
            "translations in this index. It is not a claim that the text is "
            "wrong, and a narrow index will produce more of them.",
            "These numbers describe MIZAN's behaviour on this corpus. They "
            "say nothing about how often publishers misquote; that was not "
            "measured and this corpus could not support such a claim.",
            "Ground truth for reference accuracy is the publisher's own "
            "printed citation, not an independent adjudication.",
            "The corpus is drawn from a small number of hosts and is not a "
            "random sample of da'wah publishing.",
        ],
    }

    with open(RESULTS, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=2)
    print_summary(res)
    print(f"\nwritten {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
