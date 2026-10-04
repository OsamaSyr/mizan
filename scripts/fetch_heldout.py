#!/usr/bin/env python3
"""
Fetch the held-out "unknown translation" editions used to measure the semantic tier.

    python3 scripts/fetch_heldout.py            # download what is missing (16 requests at most)
    python3 scripts/fetch_heldout.py --verify   # check cached files against the manifest
    python3 scripts/fetch_heldout.py --from-dir ../prototype/data/full   # offline import

Why this exists
---------------
`tests/test_semantic.py --measure` asks how MIZAN behaves on published Qur'an
translations that are NOT in its approved index. Those translations belong to
their translators and publishers, so their text is never committed. This script
re-downloads exactly the editions the measurement used — one request per
edition to the public AlQuran.cloud API — into `data/heldout/`, which carries
its own `.gitignore` so that only `manifest.json` is ever committed.

The manifest records, for every edition, the SHA-256 of the derived file the
measurement reads (`{"surah:ayah": text}`, canonical JSON). `--verify` and the
test suite compare against it, so a re-fetch that returned different text would
be caught instead of silently changing the numbers. On 2026-10-04 the API text
was byte-identical to the copies the original measurement used (6,236 / 6,236
verses for en.asad, checked verse by verse).

Politeness: one request per edition, sequential, 2 s apart, a descriptive
User-Agent, and nothing re-fetched that is already cached and verified.
`api.alquran.cloud` serves no robots.txt (HTTP 404); the website host
`alquran.cloud` disallows `/api/` for crawlers of its own pages, which this
script does not touch. The API is published for programmatic use.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "data", "heldout")
MANIFEST = os.path.join(OUT, "manifest.json")
API = "https://api.alquran.cloud/v1/quran/{edition}"
UA = ("Mizan-Research/0.1 (Islamic AI Challenge 2026; held-out evaluation; "
      "contact osamaabdullh2002@gmail.com)")
DELAY_S = 2.0

# Every edition the measurement touches. TEST is what results_semantic.json
# reports; DEV was used to choose thresholds and the index design. None of
# them is in corpus.sqlite (checked by title and by text; docs/SEMANTIC.md §6).
TEST_EDITIONS = ["en.asad", "en.arberry", "en.daryabadi", "en.itani", "en.wahiduddin",
                 "en.ahmedali", "hi.farooq", "ur.ahmedali", "ur.jawadi", "ur.kanzuliman",
                 "bn.bengali"]
DEV_EDITIONS = ["en.shakir", "en.qaribullah", "ur.jalandhry", "bn.hoque", "hi.hindi"]
EDITIONS = TEST_EDITIONS + DEV_EDITIONS

GITIGNORE = ("# Third-party translation text: fetched by scripts/fetch_heldout.py,\n"
             "# never committed. Only the manifest (SHA-256s, no text) is tracked.\n"
             "*\n!.gitignore\n!manifest.json\n")


def file_name(edition: str) -> str:
    """Same naming as the prototype copies, so either directory can be read."""
    return edition.replace(".", "_") + ".json"


def canonical_bytes(verses: dict[str, str]) -> bytes:
    """The derived file: verses in Mushaf order, compact UTF-8 JSON."""
    def key(k: str) -> tuple[int, int]:
        s, a = k.split(":")
        return int(s), int(a)
    ordered = {k: verses[k] for k in sorted(verses, key=key)}
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def load_manifest() -> dict:
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding="utf-8") as fh:
            return json.load(fh)
    return {"source": "https://api.alquran.cloud/v1/quran/{edition}",
            "note": ("Text is NOT redistributed. Run scripts/fetch_heldout.py to "
                     "download it into data/heldout/ (gitignored)."),
            "editions": {}}


def save_manifest(man: dict) -> None:
    os.makedirs(OUT, exist_ok=True)
    with open(MANIFEST, "w", encoding="utf-8") as fh:
        json.dump(man, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")


def ensure_gitignore() -> None:
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, ".gitignore")
    if not os.path.exists(path) or open(path, encoding="utf-8").read() != GITIGNORE:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(GITIGNORE)


def cached_ok(edition: str, man: dict) -> bool:
    path = os.path.join(OUT, file_name(edition))
    entry = man["editions"].get(edition)
    if not entry or not os.path.exists(path):
        return False
    return sha256(open(path, "rb").read()) == entry["sha256_file"]


def fetch(edition: str) -> tuple[dict[str, str], dict]:
    req = urllib.request.Request(API.format(edition=edition), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
        status = r.status
    d = json.loads(raw)
    if status != 200 or d.get("code") != 200:
        raise RuntimeError(f"{edition}: HTTP {status}, API code {d.get('code')}")
    verses = {f"{s['number']}:{a['numberInSurah']}": a["text"]
              for s in d["data"]["surahs"] for a in s["ayahs"]}
    ed = d["data"].get("edition", {})
    meta = {"url": API.format(edition=edition),
            "retrieved_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sha256_response": sha256(raw),
            "name": ed.get("englishName"), "identifier": ed.get("identifier"),
            "language": ed.get("language")}
    return verses, meta


def store(edition: str, verses: dict[str, str], meta: dict, man: dict) -> None:
    data = canonical_bytes(verses)
    with open(os.path.join(OUT, file_name(edition)), "wb") as fh:
        fh.write(data)
    man["editions"][edition] = {**meta, "file": f"data/heldout/{file_name(edition)}",
                                "verses": len(verses), "sha256_file": sha256(data),
                                "split": "test" if edition in TEST_EDITIONS else "dev"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verify", action="store_true", help="check cached files, fetch nothing")
    ap.add_argument("--force", action="store_true", help="re-fetch even if cached and verified")
    ap.add_argument("--from-dir", help="import {edition_file}.json copies instead of fetching")
    ap.add_argument("--editions", default="", help="comma list (default: all)")
    a = ap.parse_args()
    eds = [e for e in a.editions.split(",") if e] or EDITIONS
    ensure_gitignore()
    man = load_manifest()

    if a.verify:
        bad = [e for e in eds if not cached_ok(e, man)]
        for e in eds:
            print(f"  {'ok  ' if e not in bad else 'FAIL'}  {e}")
        return 1 if bad else 0

    first = True
    for e in eds:
        if not a.force and cached_ok(e, man):
            print(f"  cached  {e}")
            continue
        if a.from_dir:
            src = os.path.join(a.from_dir, file_name(e))
            raw = open(src, "rb").read()
            verses = json.loads(raw)
            meta = {"url": API.format(edition=e), "retrieved_at": None,
                    "imported_from": os.path.basename(src), "sha256_response": None,
                    "name": None, "identifier": e, "language": e.split(".")[0]}
        else:
            if not first:
                time.sleep(DELAY_S)
            first = False
            verses, meta = fetch(e)
        expected = man["editions"].get(e, {}).get("sha256_file")
        if expected and sha256(canonical_bytes(verses)) != expected:
            print(f"  CHANGED {e}: text differs from the manifest; not overwriting "
                  f"(use --force after reviewing)", file=sys.stderr)
            return 1
        store(e, verses, meta, man)
        print(f"  fetched {e}  {len(verses)} verses")
    save_manifest(man)
    print(f"manifest: {os.path.relpath(MANIFEST, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
