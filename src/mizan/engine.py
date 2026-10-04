#!/usr/bin/env python3
"""
MIZAN — attribution engine over the APPROVED index (quranpedia.net dumps).

One question only: which approved published translation does this rendering
come from? Never "is it correct".

States:
    MATCH                     attributed to a named approved translation
    NEAR                      derived from one, but modified
    UNATTRIBUTED              matches none indexed -> REFER (never "wrong")
    NO_APPROVED_TRANSLATION   resolved in Arabic, no approved text in this language
    UNRESOLVED                could not be resolved at all

Deterministic throughout. No model touches the verdict.
"""
import os, re, sqlite3, unicodedata
from difflib import SequenceMatcher

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.path.join(ROOT, "data", "corpus.sqlite")

# Frozen with the index. Changing these invalidates every measured number.
T_MATCH = 0.92
T_NEAR = 0.75

FOOT = re.compile(r"\[\d+\]")
LEADNUM = re.compile(r"^\s*\d+\s*[.)]\s*")
PARENS = re.compile(r"\([^)]*\)")
DIACRITICS = re.compile(r"[ؗ-ًؚ-ْٰـ]")
NONWORD = re.compile(r"[^\w\s]", re.UNICODE)


def normalize(t: str) -> str:
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


def sim(a: str, b: str) -> float:
    """Sequence ratio blended with token Jaccard — robust to reordering."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    jac = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    return 0.6 * seq + 0.4 * jac


# ---------------------------------------------------------------------------
# Verbatim check — what MATCH means.
#
# sim() ranks and locates; it does not decide identity. One changed word in a
# 15-word verse ("does not forgive" -> "does forgive") still scores ~0.98, and
# normalize() deletes parentheses, so "He (never) forgives" scores 1.0. Found by
# red-team review, 2026-10-03. The scientific package forbids attributing to a
# reference a text that is not in it, and building on an altered verse.
#
# So MATCH additionally requires the published words to BE the approved words:
# the same token sequence after removing only what cannot change wording —
# case, punctuation (except the question mark), footnote markers, a leading
# verse number, invisible format characters, Arabic diacritics and letter-form
# variants. Parenthesised text counts as words. A translator's own
# parenthetical glosses may be left out by the publisher (their bracketed
# text is the translator's explanation, not the verse), so the approved side
# is also tried with its parentheses removed — never the published side.
# Marked omissions ("...", "…") are allowed: each marked segment must be
# verbatim and in order.
# ---------------------------------------------------------------------------
_QMARK = re.compile(r"[?؟]")
# A bracketed verse number, "(56)" or "﴿٥٦﴾" — several editions end each verse
# with one; it is numbering, not wording.
_VERSE_NO = re.compile(r"[(\[﴿]\s*[0-9٠-٩۰-۹]{1,3}\s*[)\]﴾]")
# LEADNUM, but also after an opening quotation mark: '"36. It is not...' is
# the approved '36. It is not...' quoted with its verse number.
_LEADNUM_QUOTED = re.compile(r"^[\s\"'“”‘’«»„]*\d+\s*[.)]\s*")
_ELLIPSIS = re.compile(r"\.(?:\s*\.){2,}")
_LETTER_FORMS = str.maketrans({"ی": "ي", "ک": "ك", "ہ": "ه", "ۃ": "ه", "ھ": "ه"})


def _fold(t: str) -> str:
    t = unicodedata.normalize("NFKC", t)
    return "".join(ch for ch in t if unicodedata.category(ch) != "Cf")


def _latin_plain(t: str) -> str:
    """Drop Latin accents only ("Allâh" = "Allah", "Muḥammad" = "Muhammad"):
    transliteration spelling, not wording. Indic vowel signs are kept."""
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not ("̀" <= ch <= "ͯ"))
    return unicodedata.normalize("NFC", t)


def _words(t: str) -> list[str]:
    """Split on anything that is not a letter, digit or combining mark. Unlike
    NONWORD, keeps Devanagari/Bengali vowel signs inside their word, so a
    changed vowel sign is a changed word."""
    return "".join(ch if unicodedata.category(ch)[0] in "LNM" else " "
                   for ch in t).split()


def strict_tokens(t: str, strip_parens: bool = False) -> list[str]:
    """Word tokens for the verbatim check (see above). Never used for ranking."""
    if not t:
        return []
    t = _fold(t)
    t = FOOT.sub(" ", t)
    t = _LEADNUM_QUOTED.sub(" ", t)
    t = _VERSE_NO.sub(" ", t)
    if strip_parens:
        t = PARENS.sub(" ", t)
    t = DIACRITICS.sub("", t)
    t = (t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
          .replace("ة", "ه").replace("ى", "ي")).translate(_LETTER_FORMS)
    t = _QMARK.sub(" qmark ", t)
    return _words(_latin_plain(t).lower())


def _find_run(needle: list[str], hay: list[str], start: int = 0) -> int:
    """Index of the first occurrence of `needle` as a contiguous run, or -1."""
    n = len(needle)
    for i in range(start, len(hay) - n + 1):
        if hay[i:i + n] == needle:
            return i
    return -1


def verbatim(published: str, approved: str) -> str | None:
    """
    "whole" if the published words are exactly the approved words, "excerpt"
    if they are a contiguous run of them (or marked-omission segments of them,
    in order), else None.
    """
    pieces = [strict_tokens(p) for p in _ELLIPSIS.split(_fold(published or ""))]
    pieces = [p for p in pieces if p]
    if not pieces:
        return None
    for strip in (False, True):
        app = strict_tokens(approved, strip_parens=strip)
        if not app:
            continue
        if len(pieces) == 1 and pieces[0] == app:
            return "whole"
        pos, ok = 0, True
        for p in pieces:
            i = _find_run(p, app, pos)
            if i < 0:
                ok = False
                break
            pos = i + len(p)
        if ok:
            return "excerpt"
    return None


class Mizan:
    def __init__(self, db=DB):
        self.con = sqlite3.connect(db, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.books = {
            r["book_id"]: dict(r)
            for r in self.con.execute("SELECT * FROM translations")
        }
        self.version = next(iter(self.books.values()))["version"] if self.books else "?"
        self._cache = {}

    def languages(self):
        out = {}
        for b in self.books.values():
            out.setdefault(b["language"], []).append(b["book_id"])
        return out

    def editions(self, lang):
        return [b["book_id"] for b in self.books.values() if b["language"] == lang]

    def verse(self, book_id, surah, ayah):
        k = (book_id, surah, ayah)
        if k not in self._cache:
            r = self.con.execute(
                "SELECT text FROM ayat WHERE book_id=? AND surah=? AND ayah=?",
                k).fetchone()
            self._cache[k] = r["text"] if r else None
        return self._cache[k]

    def attribute(self, text, surah, ayah, lang, exclude=None):
        """Rank every approved translation of this verse in this language."""
        scored = []
        for bid in self.editions(lang):
            if bid == exclude:
                continue
            approved = self.verse(bid, surah, ayah)
            if approved:
                scored.append((bid, sim(text, approved), approved))
        if not scored:
            return {"state": "NO_APPROVED_TRANSLATION", "ref": f"{surah}:{ayah}",
                    "lang": lang, "attributed_to": None, "score": 0.0,
                    "n_compared": 0}
        scored.sort(key=lambda x: -x[1])
        bid, score, approved = scored[0]
        state = ("MATCH" if score >= T_MATCH
                 else "NEAR" if score >= T_NEAR
                 else "UNATTRIBUTED")
        return {
            "state": state, "ref": f"{surah}:{ayah}", "lang": lang,
            "attributed_to": bid if state != "UNATTRIBUTED" else None,
            "title": self.books[bid]["title"] if state != "UNATTRIBUTED" else None,
            "score": round(score, 3),
            "approved_text": approved,
            "runners_up": [(b, round(s, 3)) for b, s, _ in scored[1:4]],
            "n_compared": len(scored),
            "source": "quranpedia.net", "index_version": self.version,
        }


if __name__ == "__main__":
    m = Mizan()
    print(f"index {m.version} · {len(m.books)} approved translations")
    for lang, ids in sorted(m.languages().items()):
        print(f"  {lang}: {len(ids)}")
