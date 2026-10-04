#!/usr/bin/env python3
"""
Build the approved-translation index from the OFFICIAL Quranpedia dumps.

Source: https://quranpedia.net/dumps  (version recorded in the DB)
NOT scraped — the API usage policy forbids bulk scraping and points to these
versioned dumps instead. Every file's SHA-256 was verified against manifest.json.

Output: data/corpus.sqlite
  translations(book_id, title, language, locale, direction, source, version)
  ayat(book_id, surah, ayah, text, text_norm)
  + FTS5 index over text_norm for candidate retrieval
"""
import json, os, re, sqlite3, sys, unicodedata, glob

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW = os.path.join(ROOT, "data", "raw", "quranpedia")
MANIFEST = os.path.join(ROOT, "data", "raw", "manifest.json")
DB = os.path.join(ROOT, "data", "corpus.sqlite")

# Languages we index. Names are as they appear in the Quranpedia manifest.
LANGS = {
    "English": "en",
    "Tagalog": "tl",
    "اردو": "ur",
    "বাংলা": "bn",
    "हिन्दी": "hi",
}

TAG = re.compile(r"<[^>]+>")
FOOT = re.compile(r"\[\d+\]")
LEADNUM = re.compile(r"^\s*\d+\s*[.)]\s*")
PARENS = re.compile(r"\([^)]*\)")
DIACRITICS = re.compile(r"[ؗ-ًؚ-ْٰـ]")
NONWORD = re.compile(r"[^\w\s]", re.UNICODE)


def clean(t: str) -> str:
    """Strip the dump's HTML wrapper. Keeps the human-readable text intact."""
    if not t:
        return ""
    t = TAG.sub(" ", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&")
    return " ".join(t.split())


def normalize(t: str) -> str:
    """
    Comparison form. Frozen in NORMALIZATION.md — any change here invalidates
    every measured number, so it is versioned with the index.
    """
    if not t:
        return ""
    t = unicodedata.normalize("NFKC", t)
    t = FOOT.sub(" ", t)
    t = LEADNUM.sub(" ", t)
    t = PARENS.sub(" ", t)
    t = DIACRITICS.sub("", t)
    t = (t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
          .replace("ة", "ه").replace("ى", "ي"))
    t = NONWORD.sub(" ", t)
    return " ".join(t.lower().split())


def main():
    if not os.path.isdir(RAW):
        sys.exit(f"missing {RAW} — run fetch_dumps.sh first")

    version = "unknown"
    if os.path.exists(MANIFEST):
        version = json.load(open(MANIFEST, encoding="utf-8")).get("version", "unknown")

    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    con.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE translations(
            book_id INTEGER PRIMARY KEY, title TEXT, language TEXT,
            locale TEXT, direction TEXT, source TEXT, version TEXT, n_ayat INTEGER);
        CREATE TABLE ayat(
            book_id INTEGER, surah INTEGER, ayah INTEGER,
            text TEXT, text_norm TEXT,
            PRIMARY KEY(book_id, surah, ayah));
        CREATE INDEX idx_ref ON ayat(surah, ayah);
    """)

    files = sorted(glob.glob(os.path.join(RAW, "*.json")))
    print(f"indexing {len(files)} approved translations (dump {version})\n")
    total = 0
    for f in files:
        d = json.load(open(f, encoding="utf-8"))
        lang = d.get("language")
        code = LANGS.get(lang, (d.get("locale_code") or "?")[:5])
        rows = []
        for a in d.get("ayahs", []):
            txt = clean(a.get("translated_text", ""))
            if not txt:
                continue
            rows.append((d["id"], a["surah_number"], a["ayah_number"], txt, normalize(txt)))
        con.execute(
            "INSERT INTO translations VALUES (?,?,?,?,?,?,?,?)",
            (d["id"], d.get("name") or d.get("short_name"), code,
             d.get("locale_code"), d.get("direction"), "quranpedia.net", version, len(rows)))
        con.executemany("INSERT OR REPLACE INTO ayat VALUES (?,?,?,?,?)", rows)
        total += len(rows)
        print(f"  {code:3s} {str(d.get('name'))[:52]:54s} {len(rows):5d}")

    # FTS over the normalized text — candidate retrieval before exact scoring
    con.executescript("""
        CREATE VIRTUAL TABLE ayat_fts USING fts5(
            text_norm, content='ayat', content_rowid='rowid', tokenize='unicode61');
        INSERT INTO ayat_fts(rowid, text_norm) SELECT rowid, text_norm FROM ayat;
    """)
    con.commit()

    langs = con.execute(
        "SELECT language, COUNT(*) FROM translations GROUP BY language ORDER BY 2 DESC"
    ).fetchall()
    con.execute("VACUUM")
    con.close()

    print(f"\n{total:,} verse renderings · {os.path.getsize(DB)/1e6:.1f} MB")
    print("by language: " + " · ".join(f"{l}:{n}" for l, n in langs))
    print(f"source: quranpedia.net dumps, version {version}")


if __name__ == "__main__":
    main()
