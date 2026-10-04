#!/usr/bin/env python3
"""
MIZAN — rebuild the real-world test corpus from the publishers' own pages.

Why this exists
---------------
data/real/corpus.jsonl and negatives.jsonl hold text published by
islamreligion.com and islamqa.info ("Copyright © IslamReligion.com. All rights
reserved."). We use it to MEASURE MIZAN; we do not have the right to
redistribute it, so the public repository does not contain it.

What the repository does contain is data/real/corpus.manifest.json: for every
record, the source URL, the extractor's ordinal on that page, the printed
reference, and a SHA-256 of the measured text — locators and checksums, no
text. This script fetches exactly those pages (robots.txt honoured, one host
at a time, the crawl delay of scripts/collect_corpus.py, a contact address in
the User-Agent), runs the SAME extractor that built the original corpus
(collect_corpus.build_records, imported, not copied), keeps the records whose
id and checksum match, and writes them back in the original order — the order
matters, because the false-positive probe samples negatives by position.

Pages change. A record whose page was edited since 2026-10-02 will not match
its checksum and is dropped, loudly, with a count; the eval's corpus
fingerprint then differs from the reference and SUMMARY.md says so. That is
the honest cost of not redistributing someone else's text.

Usage
-----
    python3 scripts/rebuild_real_corpus.py              # fetch (cached) + rebuild
    python3 scripts/rebuild_real_corpus.py --offline    # from data/real/raw/ only
    python3 scripts/rebuild_real_corpus.py --pages 5    # smoke test: first 5 pages
    python3 scripts/rebuild_real_corpus.py --check      # verify the current files
    python3 scripts/rebuild_real_corpus.py --write-manifest   # maintainer: re-pin

Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REAL = os.path.join(ROOT, "data", "real")
CORPUS = os.path.join(REAL, "corpus.jsonl")
NEGATIVES = os.path.join(REAL, "negatives.jsonl")
MANIFEST = os.path.join(REAL, "corpus.manifest.json")
CRAWL_LOG = os.path.join(REAL, "manifest.json")
REPORT = os.path.join(REAL, "rebuild_report.json")
FORMAT = "mizan-real-corpus-manifest/1"


def _collector():
    spec = importlib.util.spec_from_file_location(
        "collect_corpus", os.path.join(HERE, "collect_corpus.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # type: ignore[union-attr]
    return mod


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def quote_sha(r: dict) -> str:
    """Checksum over every field a measurement reads."""
    return _sha(json.dumps([r.get("published_text"), r.get("context_before"),
                            r.get("context_after")], ensure_ascii=False))


def _ordinal(rid: str, url: str, published: str | None, negative: bool) -> int | None:
    """Recover the extractor's ordinal from the content-derived record id."""
    for i in range(5000):
        key = f"neg|{url}|{i}" if negative else f"{url}|{i}|{published[:200]}"
        if hashlib.sha1(key.encode()).hexdigest()[:16] == rid:
            return i
    return None


def _load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


# ---------------------------------------------------------------------------
def write_manifest() -> int:
    rows, negs = _load(CORPUS), _load(NEGATIVES)
    crawl = json.load(open(CRAWL_LOG, encoding="utf-8")) if os.path.exists(CRAWL_LOG) else {}
    pages: dict[str, dict] = {}
    for r in rows + negs:
        pages.setdefault(r["source_url"], {"url": r["source_url"], "lang": r["lang"],
                                           "host": r["source_host"]})
    quotes = [{
        "id": r["id"], "url": r["source_url"], "lang": r["lang"],
        "ordinal": _ordinal(r["id"], r["source_url"], r["published_text"], False),
        "printed_ref": r.get("printed_ref"), "markup_hint": r.get("markup_hint"),
        "scripture_marked": r.get("scripture_marked"),
        "chars": len(r["published_text"]),
        "text_sha256": _sha(r["published_text"]), "sha256": quote_sha(r),
    } for r in rows]
    negatives = [{
        "id": r["id"], "url": r["source_url"], "lang": r["lang"],
        "ordinal": _ordinal(r["id"], r["source_url"], None, True),
        "chars": len(r["text"]), "sha256": _sha(r["text"]),
    } for r in negs]
    unresolved = [q["id"] for q in quotes + negatives if q["ordinal"] is None]
    if unresolved:
        print(f"could not recover the extractor ordinal for {len(unresolved)} records",
              file=sys.stderr)
        return 1
    out = {
        "format": FORMAT,
        "what": ("Locators and SHA-256 checksums for the real-world test corpus. "
                 "No third-party text. Rebuild the text with "
                 "scripts/rebuild_real_corpus.py."),
        "retrieved_at": (rows[0].get("retrieved_at") if rows else None),
        "extractor": "scripts/collect_corpus.py build_records()",
        "extractor_sha256": hashlib.sha256(
            open(os.path.join(HERE, "collect_corpus.py"), "rb").read()).hexdigest(),
        "user_agent": crawl.get("user_agent"),
        "checksum": {
            "quotations": "sha256(json.dumps([published_text, context_before, "
                          "context_after], ensure_ascii=False))",
            "negatives": "sha256(text)",
        },
        "ordinal": ("index of the paragraph (islamreligion.com) or quoted span "
                    "(islamqa.info) in build_records() output order; the record id "
                    "is sha1(url|ordinal|text[:200])[:16]"),
        "counts": {"pages": len(pages), "quotations": len(quotes),
                   "negatives": len(negatives)},
        "pages": list(pages.values()),
        "quotations": quotes,
        "negatives": negatives,
    }
    with open(MANIFEST, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print(f"wrote {os.path.relpath(MANIFEST, ROOT)}: {len(pages)} pages · "
          f"{len(quotes)} quotations · {len(negatives)} negatives")
    return 0


# ---------------------------------------------------------------------------
def check() -> int:
    man = json.load(open(MANIFEST, encoding="utf-8"))
    rows = {r["id"]: r for r in _load(CORPUS)} if os.path.exists(CORPUS) else {}
    negs = {r["id"]: r for r in _load(NEGATIVES)} if os.path.exists(NEGATIVES) else {}
    q_ok = sum(1 for q in man["quotations"]
               if q["id"] in rows and quote_sha(rows[q["id"]]) == q["sha256"])
    n_ok = sum(1 for q in man["negatives"]
               if q["id"] in negs and _sha(negs[q["id"]]["text"]) == q["sha256"])
    print(f"quotations {q_ok}/{len(man['quotations'])} · "
          f"negatives {n_ok}/{len(man['negatives'])} match the manifest")
    return 0 if (q_ok == len(man["quotations"]) and n_ok == len(man["negatives"])) else 1


# ---------------------------------------------------------------------------
def rebuild(offline: bool, max_pages: int | None, strict: bool) -> int:
    man = json.load(open(MANIFEST, encoding="utf-8"))
    if man.get("format") != FORMAT:
        print(f"unknown manifest format {man.get('format')!r}", file=sys.stderr)
        return 2
    cc = _collector()
    cur = hashlib.sha256(open(os.path.join(HERE, "collect_corpus.py"), "rb").read()).hexdigest()
    if cur != man.get("extractor_sha256"):
        print("note: scripts/collect_corpus.py has changed since the manifest was "
              "written; records it now extracts differently will not match.")

    pages = man["pages"][:max_pages] if max_pages else man["pages"]
    wanted = {p["url"] for p in pages}
    fetcher = cc.Fetcher(offline=offline, max_pages=10 ** 6)
    retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    built_q: dict[str, dict] = {}
    built_n: dict[str, dict] = {}
    unavailable: list[str] = []
    t0 = time.time()
    print(f"rebuilding from {len(pages)} pages "
          f"({'cache only' if offline else 'network, cached under data/real/raw/'}; "
          f"robots.txt checked per URL, >= {cc.DEFAULT_DELAY}s between requests)")
    def take(p: dict, h: str) -> None:
        q, n = cc.build_records(p["url"], p["lang"], h, retrieved_at)
        for r in q:
            built_q.setdefault(r["id"], r)
        for r in n:
            built_n.setdefault(r["id"], r)

    for i, p in enumerate(pages, 1):
        cached = os.path.exists(cc.Fetcher.cache_path(p["url"]))
        h = fetcher.get(p["url"])
        if h is None:
            unavailable.append(p["url"])
            print(f"  [{i:3d}/{len(pages)}] unavailable  {p['url']}")
            continue
        take(p, h)
        if not cached or i % 25 == 0 or i == len(pages):
            print(f"  [{i:3d}/{len(pages)}] {'fetched' if not cached else 'cached '}  "
                  f"{p['url'][:90]}", flush=True)

    # One polite second attempt for pages that timed out or errored.
    if unavailable and not offline:
        print(f"retrying {len(unavailable)} unavailable page(s) once, after a pause")
        time.sleep(10)
        retry, unavailable = unavailable, []
        by_url = {p["url"]: p for p in pages}
        for u in retry:
            h = fetcher.get(u)
            if h is None:
                unavailable.append(u)
                print(f"  still unavailable  {u}")
            else:
                take(by_url[u], h)
                print(f"  fetched on retry  {u}")

    out_q, out_n, changed, missing = [], [], [], []
    for spec, built, out, digest in (
            (man["quotations"], built_q, out_q, quote_sha),
            (man["negatives"], built_n, out_n, lambda r: _sha(r["text"]))):
        for m in spec:
            if m["url"] not in wanted:
                continue
            r = built.get(m["id"])
            if r is None:
                missing.append(m["id"])
            elif digest(r) != m["sha256"]:
                changed.append(m["id"])
            else:
                out.append(r)

    exp_q = sum(1 for m in man["quotations"] if m["url"] in wanted)
    exp_n = sum(1 for m in man["negatives"] if m["url"] in wanted)
    os.makedirs(REAL, exist_ok=True)
    with open(CORPUS, "w", encoding="utf-8") as fh:
        for r in out_q:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(NEGATIVES, "w", encoding="utf-8") as fh:
        for r in out_n:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    report = {
        "rebuilt_at": retrieved_at, "offline": offline, "pages": len(pages),
        "pages_unavailable": unavailable,
        "quotations": {"expected": exp_q, "matched": len(out_q)},
        "negatives": {"expected": exp_n, "matched": len(out_n)},
        "changed_since_manifest": changed, "not_found": missing,
        "seconds": round(time.time() - t0, 1),
    }
    with open(REPORT, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)

    complete = len(out_q) == exp_q and len(out_n) == exp_n
    print(f"\nquotations  {len(out_q)}/{exp_q} match the manifest checksums")
    print(f"negatives   {len(out_n)}/{exp_n} match the manifest checksums")
    if not complete:
        print(f"WARNING: {len(changed)} records changed on the publisher's page and "
              f"{len(missing)} were not found ({len(unavailable)} pages unavailable). "
              "They are excluded, so real-corpus numbers will not reproduce exactly; "
              "see data/real/rebuild_report.json.")
    print(f"written {os.path.relpath(CORPUS, ROOT)} and "
          f"{os.path.relpath(NEGATIVES, ROOT)} in {report['seconds']}s")
    return 1 if (strict and not complete) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="use data/real/raw/ only")
    ap.add_argument("--pages", type=int, default=None, help="only the first N pages")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero unless every record matches its checksum")
    ap.add_argument("--check", action="store_true",
                    help="verify the current corpus files against the manifest")
    ap.add_argument("--write-manifest", action="store_true",
                    help="maintainer: regenerate the manifest from the current corpus")
    a = ap.parse_args()
    if a.write_manifest:
        return write_manifest()
    if not os.path.exists(MANIFEST):
        print(f"missing {MANIFEST}", file=sys.stderr)
        return 2
    if a.check:
        return check()
    return rebuild(a.offline, a.pages, a.strict)


if __name__ == "__main__":
    raise SystemExit(main())
