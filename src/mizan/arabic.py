#!/usr/bin/env python3
"""
MIZAN — Arabic-side quote detection.

Answers step (1) of the pipeline: given a paragraph of Arabic prose, which
Quranic verses are quoted inside it, and where?

This layer is purely deterministic. It never generates, corrects or completes
scripture; it only reports that a run of words in the input is byte-identical
(after normalisation) to a run of words in the Tanzil text.

Why n-gram matching and not similarity search
---------------------------------------------
The Arabic Quran is a FIXED string. An author quoting it either reproduces it
or does not. So detection here is a lookup, not an inference — which is also
why this layer needs no model and cannot hallucinate a reference.

Two orthographies, one index
----------------------------
Tanzil ships the same verse in two spellings:

    uthmani       وَمَا خَلَقْتُ ٱلْجِنَّ ... (ٱ alef wasla, ٰ superscript alef)
    simple-clean  وما خلقت الجن ...

`engine.normalize()` strips combining diacritics but leaves alef wasla and the
small waw/yeh alone, and it cannot restore the long alef that Uthmani writes as
a superscript mark. Measured on all 6236 verses, the two spellings agree on only
71% of words after normalisation — far too low to hang a 4-word match on.

Rather than "fix" either text (forbidden: Tanzil is CC BY 3.0 no-derivatives),
BOTH normalised forms are indexed. An author who pastes Uthmani from a mushaf
app and an author who types plain Arabic both hit the same verse id.
"""
from __future__ import annotations

import os
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from .engine import DB, normalize

# Minimum run of matching words before we call something a quotation.
# Below 4, common formulae ("الحمد لله رب العالمين" fragments, "من الله") fire
# on ordinary pious prose and the precision collapses.
MIN_WORDS = 4

# Ornamental Uthmani codepoints that carry no consonantal value. Folding them to
# nothing (not to a letter) is what makes the Uthmani n-grams line up with
# themselves consistently across sources that copy the mushaf imperfectly.
_UTHMANI_FOLD = str.maketrans({
    "ٱ": "ا",  # ALEF WASLA  ٱ -> ا
    "ٰ": "",        # SUPERSCRIPT ALEF ٰ (Uthmani writes the long vowel here)
    "ۥ": "",        # SMALL WAW   ۥ
    "ۦ": "",        # SMALL YEH   ۦ
    "ـ": "",        # TATWEEL     ـ
})

# ﴿ ... ﴾ — the conventional Arabic quotation marks for scripture. When an
# author uses them they are a stronger signal than any statistic, so they are
# honoured directly.
QUOTE_BRACKETS = re.compile(r"﴿\s*(.+?)\s*﴾", re.DOTALL)

# Arabic-Indic digits, used in references like [البقرة: ٢٥٥].
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# [السورة: رقم] / (البقرة: 255) / {النور: ٣٥}
CITATION = re.compile(
    r"[\[\(\{]\s*(?:سورة\s+)?([ء-ي\s]{2,25}?)\s*[:：]\s*([0-9٠-٩]{1,3})\s*[\]\)\}]"
)


def fold_arabic(text: str) -> str:
    """
    Normalise Arabic for matching, on top of the frozen engine.normalize().

    Composed, not replaced: engine.normalize() stays the single source of truth
    for diacritics, hamza and ta-marbuta (other code depends on it). This only
    adds the Uthmani-specific ornaments it does not know about.
    """
    return normalize(text.translate(_UTHMANI_FOLD))


@dataclass
class ArabicQuote:
    """One detected Quranic quotation in Arabic prose."""

    surah: int
    ayah: int
    start_word: int          # inclusive index into the input's word list
    end_word: int            # exclusive
    text: str                # the words as the AUTHOR wrote them
    approved_text: str       # the Uthmani text, retrieved verbatim — never generated
    n_words: int
    evidence: str            # "ngram" | "brackets" | "citation"
    partial: bool = False    # True when only part of the verse was quoted

    @property
    def ref(self) -> str:
        return f"{self.surah}:{self.ayah}"

    def as_dict(self) -> dict:
        d = {
            "ref": self.ref, "surah": self.surah, "ayah": self.ayah,
            "start_word": self.start_word, "end_word": self.end_word,
            "text": self.text, "approved_text": self.approved_text,
            "n_words": self.n_words, "evidence": self.evidence,
            "partial": self.partial,
        }
        return d


@dataclass
class _Index:
    """n-gram -> set of (surah, ayah). Built once, held in memory (~2 MB)."""

    grams: dict = field(default_factory=lambda: defaultdict(set))
    verses: dict = field(default_factory=dict)      # (s,a) -> uthmani text
    norm_words: dict = field(default_factory=dict)  # (s,a) -> folded word list


class ArabicDetector:
    """
    Finds Quranic quotations in Arabic prose and resolves them to surah:ayah.

    Usage:
        det = ArabicDetector()
        for q in det.detect(paragraph):
            print(q.ref, q.text)
    """

    def __init__(self, db: str = DB, min_words: int = MIN_WORDS):
        if not os.path.exists(db):
            raise FileNotFoundError(f"corpus not found: {db}")
        self.min_words = min_words
        self.con = sqlite3.connect(db, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self._check_table()
        self.idx = self._build()
        self._surah_names = self._load_surah_names()

    # ---------------------------------------------------------------- setup

    def _check_table(self) -> None:
        got = self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='arabic_ayat'"
        ).fetchone()
        if not got:
            raise RuntimeError(
                "arabic_ayat table missing — run scripts/fetch_arabic.py once"
            )

    def _build(self) -> _Index:
        """
        Index every MIN_WORDS-length window of every verse, in BOTH spellings.

        Storing only the window start would be smaller, but storing the full
        gram string lets a lookup be a single dict hit with no verification
        pass over candidates that merely share a prefix.
        """
        idx = _Index()
        n = self.min_words
        rows = self.con.execute(
            "SELECT surah, ayah, text_uthmani, text_simple FROM arabic_ayat"
        ).fetchall()
        if not rows:
            raise RuntimeError("arabic_ayat is empty — run scripts/fetch_arabic.py")

        for r in rows:
            key = (r["surah"], r["ayah"])
            idx.verses[key] = r["text_uthmani"]
            # The simple spelling is the canonical comparison form: it is what a
            # person typing Arabic normally produces.
            idx.norm_words[key] = fold_arabic(r["text_simple"]).split()
            for spelling in (r["text_uthmani"], r["text_simple"]):
                words = fold_arabic(spelling).split()
                for i in range(len(words) - n + 1):
                    idx.grams[" ".join(words[i:i + n])].add(key)
        return idx

    def _load_surah_names(self) -> dict:
        """
        Map normalised surah name -> number, for `[البقرة: 255]` style refs.

        Built from a static list because surah names are not in the corpus and
        there is no network at runtime. Only the names are hardcoded, never
        verse text.
        """
        names = (
            "الفاتحة البقرة آل عمران|النساء المائدة الأنعام الأعراف الأنفال التوبة يونس هود "
            "يوسف الرعد إبراهيم الحجر النحل الإسراء الكهف مريم طه الأنبياء الحج المؤمنون "
            "النور الفرقان الشعراء النمل القصص العنكبوت الروم لقمان السجدة الأحزاب سبأ فاطر "
            "يس الصافات ص الزمر غافر فصلت الشورى الزخرف الدخان الجاثية الأحقاف محمد الفتح "
            "الحجرات ق الذاريات الطور النجم القمر الرحمن الواقعة الحديد المجادلة الحشر "
            "الممتحنة الصف الجمعة المنافقون التغابن الطلاق التحريم الملك القلم الحاقة المعارج "
            "نوح الجن المزمل المدثر القيامة الإنسان المرسلات النبأ النازعات عبس التكوير "
            "الانفطار المطففين الانشقاق البروج الطارق الأعلى الغاشية الفجر البلد الشمس الليل "
            "الضحى الشرح التين العلق القدر البينة الزلزلة العاديات القارعة التكاثر العصر "
            "الهمزة الفيل قريش الماعون الكوثر الكافرون النصر المسد الإخلاص الفلق الناس"
        )
        # "آل عمران" contains a space; the pipe marks where it ends.
        flat = names.replace("آل عمران|", "آل_عمران ").split()
        out = {}
        for i, nm in enumerate(flat, start=1):
            out[fold_arabic(nm.replace("_", " "))] = i
        # A few common alternates writers use.
        out[fold_arabic("ال عمران")] = 3
        out[fold_arabic("الاسراء")] = 17
        out[fold_arabic("بني إسرائيل")] = 17
        return out

    # ------------------------------------------------------------ detection

    def detect(self, text: str) -> list[ArabicQuote]:
        """
        Return non-overlapping Quranic quotations found in `text`.

        Order of evidence, strongest first: explicit ﴿ ﴾ brackets, then n-gram
        runs. Whichever fires, the span is extended greedily so a long quotation
        is reported once rather than as a chain of 4-word fragments.
        """
        if not text or not text.strip():
            return []

        words = text.split()
        folded = [fold_arabic(w) for w in words]

        found = self._scan_ngrams(words, folded)
        found += self._scan_brackets(text, words, folded)
        return self._resolve_overlaps(found)

    def _scan_ngrams(self, words: list[str], folded: list[str]) -> list[ArabicQuote]:
        """
        Slide a MIN_WORDS window; on a hit, extend as far as the verse allows.

        Extension is what keeps precision high: a 4-word coincidence usually
        cannot grow, while a genuine quotation grows to its full length.
        """
        n = self.min_words
        out: list[ArabicQuote] = []
        i = 0
        # Empty folded tokens (pure punctuation) would misalign the window, so
        # work over the indices of the words that survive folding.
        live = [k for k, w in enumerate(folded) if w]

        while i + n <= len(live):
            window_idx = live[i:i + n]
            gram = " ".join(folded[k] for k in window_idx)
            hits = self.idx.grams.get(gram)
            if not hits:
                i += 1
                continue

            best = self._extend(live, i, folded, hits)
            if best is None:
                i += 1
                continue

            key, start_i, end_i = best
            s_word, e_word = live[start_i], live[end_i - 1] + 1
            quoted = " ".join(words[s_word:e_word])
            verse_len = len(self.idx.norm_words[key])
            matched_len = end_i - start_i
            out.append(ArabicQuote(
                surah=key[0], ayah=key[1],
                start_word=s_word, end_word=e_word,
                text=quoted, approved_text=self.idx.verses[key],
                n_words=matched_len, evidence="ngram",
                partial=matched_len < verse_len,
            ))
            i = end_i   # do not re-detect inside a span we just consumed
        return out

    def _extend(self, live, start_i, folded, hits):
        """
        Grow the match rightward and pick the verse that sustains it longest.

        Ties go to the SHORTER verse: if the quoted run is the whole of a short
        verse and also a prefix of a long one, the short verse is the better
        reading of what the author quoted.
        """
        best = None
        for key in hits:
            for verse_words in self._spellings(key):
                pos = self._find_sublist(verse_words,
                                         [folded[k] for k in live[start_i:start_i + self.min_words]])
                if pos < 0:
                    continue
                end_i = start_i + self.min_words
                vp = pos + self.min_words
                while (end_i < len(live) and vp < len(verse_words)
                       and folded[live[end_i]] == verse_words[vp]):
                    end_i += 1
                    vp += 1
                length = end_i - start_i
                cand = (length, -len(verse_words), key, start_i, end_i)
                if best is None or cand > best:
                    best = cand
        if best is None:
            return None
        return best[2], best[3], best[4]

    def _spellings(self, key):
        """Both normalised word lists for one verse (simple first — commoner)."""
        row = self.con.execute(
            "SELECT text_uthmani, text_simple FROM arabic_ayat WHERE surah=? AND ayah=?",
            key).fetchone()
        return [fold_arabic(row["text_simple"]).split(),
                fold_arabic(row["text_uthmani"]).split()]

    @staticmethod
    def _find_sublist(haystack: list[str], needle: list[str]) -> int:
        """Index of `needle` inside `haystack`, or -1. Small lists; plain scan."""
        n = len(needle)
        for i in range(len(haystack) - n + 1):
            if haystack[i:i + n] == needle:
                return i
        return -1

    def _scan_brackets(self, text, words, folded) -> list[ArabicQuote]:
        """
        Honour ﴿ ... ﴾ even when the enclosed text is too short or too loosely
        copied for the n-gram index to fire.

        An author who brackets a phrase is explicitly asserting it is scripture,
        so a near match is accepted here where it would not be otherwise.
        """
        out = []
        for m in QUOTE_BRACKETS.finditer(text):
            inner = m.group(1).strip()
            inner_folded = fold_arabic(inner).split()
            if not inner_folded:
                continue
            key = self._best_verse_for(inner_folded)
            if key is None:
                continue
            # Locate the bracketed words inside the word list, so the span is
            # expressed in the same coordinates as an n-gram hit.
            span = self._find_sublist(folded, inner_folded)
            if span < 0:
                # Punctuation split the words differently; fall back to a
                # character offset converted to an approximate word index.
                span = len(text[:m.start()].split())
            out.append(ArabicQuote(
                surah=key[0], ayah=key[1],
                start_word=span, end_word=span + len(inner_folded),
                text=inner,
                approved_text=self.idx.verses[key],
                n_words=len(inner_folded), evidence="brackets",
                partial=len(inner_folded) < len(self.idx.norm_words[key]),
            ))
        return out

    def _best_verse_for(self, folded_words: list[str]):
        """
        Resolve a bracketed phrase by its rarest n-gram, falling back to the
        verse sharing the most words. Returns None when nothing plausible.
        """
        n = min(self.min_words, len(folded_words))
        for i in range(len(folded_words) - n + 1):
            hits = self.idx.grams.get(" ".join(folded_words[i:i + n]))
            if hits:
                return min(hits, key=lambda k: len(self.idx.norm_words[k]))
        # Short bracketed phrase (< MIN_WORDS). Compare against verses that
        # share its rarest word rather than scanning all 6236.
        target = set(folded_words)
        if not target:
            return None
        best, best_score = None, 0.0
        for key, vw in self.idx.norm_words.items():
            vs = set(vw)
            if not vs & target:
                continue
            score = len(vs & target) / len(vs | target)
            if score > best_score:
                best, best_score = key, score
        return best if best_score >= 0.5 else None

    def _resolve_overlaps(self, quotes: list[ArabicQuote]) -> list[ArabicQuote]:
        """
        Keep the strongest reading of each stretch of text.

        Bracketed evidence outranks an n-gram hit; among equals, the longer
        match wins, because a longer run of verbatim scripture is the less
        likely coincidence.
        """
        rank = {"brackets": 2, "citation": 1, "ngram": 0}
        ordered = sorted(quotes, key=lambda q: (-rank[q.evidence], -q.n_words, q.start_word))
        chosen: list[ArabicQuote] = []
        for q in ordered:
            if all(q.end_word <= c.start_word or q.start_word >= c.end_word
                   for c in chosen):
                chosen.append(q)
        return sorted(chosen, key=lambda q: q.start_word)

    # ------------------------------------------------------------- markers

    def citations(self, text: str) -> list[tuple[int, int]]:
        """
        Parse explicit `[السورة: رقم]` markers into (surah, ayah) pairs.

        Reported separately from `detect` because a citation says WHICH verse
        the author means, not WHERE its words sit — the two are combined by the
        caller when both are available.
        """
        out = []
        for m in CITATION.finditer(text):
            name = fold_arabic(m.group(1))
            num = int(m.group(2).translate(_ARABIC_DIGITS))
            surah = self._surah_names.get(name)
            if surah is None:
                continue
            if self.con.execute(
                "SELECT 1 FROM arabic_ayat WHERE surah=? AND ayah=?", (surah, num)
            ).fetchone():
                out.append((surah, num))
        return out

    def verse(self, surah: int, ayah: int) -> str | None:
        """Retrieve the approved Uthmani text. Retrieval only — never generation."""
        return self.idx.verses.get((surah, ayah))


if __name__ == "__main__":
    det = ArabicDetector()
    demo = (
        "قال الله تعالى في محكم التنزيل: وما خلقت الجن والإنس إلا ليعبدون. "
        "وهذه الآية أصل عظيم في بيان الغاية من الخلق."
    )
    for q in det.detect(demo):
        print(f"{q.ref:>8}  {q.evidence:9s} words={q.n_words}  {q.text}")
