#!/usr/bin/env python3
"""
Add the canonical Arabic Quran text to data/corpus.sqlite.

Source: Tanzil Project (https://tanzil.net/pub/download/) — CC BY 3.0, and the
licence explicitly FORBIDS modification of the text. That constraint is the
reason this script exists as a separate one-shot step: the Arabic is loaded
verbatim, byte for byte, and nothing in the runtime engine ever writes to it.

Two editions are stored side by side because they serve different jobs:
  text_uthmani  — the canonical rendering shown to a human ("this is the verse")
  text_simple   — undiacritised, used only as a sanity cross-check
  text_norm     — engine.normalize() of the simple text; this is what matching
                  actually runs against

Run once, offline afterwards:
    python3 scripts/fetch_arabic.py

Verification is not optional here: the script refuses to write unless BOTH
editions parse to exactly 6236 ayat across 114 surahs with identical keys.
A silently truncated download would poison every detection downstream.
"""
import os
import sqlite3
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.path.join(ROOT, "data", "corpus.sqlite")
CACHE = os.path.join(ROOT, "data", "raw", "tanzil")

TANZIL_URL = "https://tanzil.net/pub/download/index.php"
EDITIONS = ("uthmani", "simple-clean")

EXPECTED_AYAT = 6236
EXPECTED_SURAHS = 114

sys.path.insert(0, os.path.join(ROOT, "src"))
from mizan.engine import normalize  # noqa: E402  (path must be set first)


def fetch(edition: str) -> str:
    """Download one Tanzil edition, caching it so reruns need no network."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{edition}.txt")
    if os.path.exists(path) and os.path.getsize(path) > 100_000:
        print(f"  {edition}: cached")
        return open(path, encoding="utf-8").read()

    body = urllib.parse.urlencode(
        {"quranType": edition, "outType": "txt-2", "agree": "true"}
    ).encode()
    req = urllib.request.Request(
        TANZIL_URL, data=body,
        headers={"User-Agent": "mizan-corpus-builder/1.0 (research; one-shot)"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read().decode("utf-8")
    with open(path, "w", encoding="utf-8") as f:
        f.write(raw)
    print(f"  {edition}: downloaded {len(raw):,} chars")
    return raw


def parse(raw: str) -> dict:
    """Parse Tanzil's `surah|ayah|text` format into {(surah, ayah): text}."""
    out = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        out[(int(parts[0]), int(parts[1]))] = parts[2].strip()
    return out


def check(name: str, verses: dict) -> None:
    """Refuse a partial download loudly rather than indexing a broken Quran."""
    if len(verses) != EXPECTED_AYAT:
        sys.exit(f"{name}: got {len(verses)} ayat, expected {EXPECTED_AYAT}")
    surahs = {s for s, _ in verses}
    if len(surahs) != EXPECTED_SURAHS:
        sys.exit(f"{name}: got {len(surahs)} surahs, expected {EXPECTED_SURAHS}")
    print(f"  {name}: {len(verses):,} ayat · {len(surahs)} surahs  OK")


def main() -> None:
    if not os.path.exists(DB):
        sys.exit(f"missing {DB} — run scripts/build_index.py first")

    print("fetching Tanzil editions")
    texts = {e: parse(fetch(e)) for e in EDITIONS}

    print("verifying")
    for e in EDITIONS:
        check(e, texts[e])
    if set(texts["uthmani"]) != set(texts["simple-clean"]):
        sys.exit("editions disagree on which ayat exist — refusing to write")

    con = sqlite3.connect(DB)
    con.executescript("""
        DROP TABLE IF EXISTS arabic_ayat;
        CREATE TABLE arabic_ayat(
            surah INTEGER, ayah INTEGER,
            text_uthmani TEXT, text_simple TEXT, text_norm TEXT,
            PRIMARY KEY(surah, ayah));
    """)
    rows = []
    for key in sorted(texts["uthmani"]):
        uth = texts["uthmani"][key]
        simple = texts["simple-clean"][key]
        # Normalise the SIMPLE text: engine.normalize strips diacritics anyway,
        # but starting from simple-clean avoids any Uthmani-only orthography
        # (superscript alef, small waw) surviving as stray codepoints.
        rows.append((key[0], key[1], uth, simple, normalize(simple)))
    con.executemany("INSERT INTO arabic_ayat VALUES (?,?,?,?,?)", rows)

    # Provenance goes in its OWN table, not in `translations`.
    # `translations` means "approved TRANSLATION we can attribute to"; the Arabic
    # is the source text, not a candidate attribution. Putting it there also
    # silently changed Mizan.version and inflated the translation count, because
    # engine.py reads the first row of that table.
    con.executescript("""
        DROP TABLE IF EXISTS arabic_source;
        CREATE TABLE arabic_source(
            name TEXT, source TEXT, version TEXT, licence TEXT, n_ayat INTEGER);
    """)
    con.execute("INSERT INTO arabic_source VALUES (?,?,?,?,?)",
                ("Tanzil — القرآن الكريم", "tanzil.net", "tanzil-1.1",
                 "CC BY 3.0 (no derivatives of the text)", len(rows)))
    con.execute("DELETE FROM translations WHERE book_id = 0")
    con.commit()

    n = con.execute("SELECT COUNT(*) FROM arabic_ayat").fetchone()[0]
    empty = con.execute("SELECT COUNT(*) FROM arabic_ayat WHERE text_norm=''").fetchone()[0]
    con.close()
    print(f"\narabic_ayat: {n:,} rows · {empty} empty normalisations")
    print("source: Tanzil Project · CC BY 3.0 · text not modified")


if __name__ == "__main__":
    main()
