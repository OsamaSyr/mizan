#!/usr/bin/env python3
"""
MIZAN — cross-language span detection.

Answers steps (2) and (3): given a TRANSLATION, where does each Quranic
quotation sit in it, and which verse is it?

The Arabic side (arabic.py) is a lookup against a fixed string. This side is
genuinely harder, because a translation is one of many legitimate renderings —
the words are not fixed, only the meaning is.

Three tiers, cheapest first
---------------------------
    (a) printed_references   "(Quran 51:56)" — a regex. Free, and when present
                             it is the author telling us the answer outright.
    (b) lexical_candidates   rare-token inverted index over the approved
                             renderings. Catches verbatim and lightly edited
                             quotations. Free.
    (c) semantic_candidates  BAAI/bge-m3, run locally (semantic.py). Proposes
                             verse ids for sentences whose MEANING is close to
                             a verse even when the WORDS are not — a quotation
                             taken from a translation we do not index.

Tier (c) exists because (b) is structurally blind to a quotation whose wording
differs from every approved rendering: such a passage shares little rare
vocabulary with them, so lexical retrieval either misses it or pins it to the
wrong verse. Tier (c) only ever PROPOSES ids. Every proposal is still scored by
engine.sim() against the approved published string, and the attribution state
(MATCH / NEAR / UNATTRIBUTED) is decided by engine.attribute() exactly as
before, so the model has no authority over the verdict and never emits text.

When the ML dependencies, the model, or the precomputed index are absent, tier
(c) is skipped and detection runs the deterministic path unchanged. That is the
documented fallback for this critical dependency (`semantic.available()`).

`find()` runs in phases: the deterministic tiers first (exactly the plain-python
path), then tier (c) on what they left unexplained, within a per-request token
budget spent most-promising-first (see the SEM_SELECT constants). Texts within
the budget get the full semantic pass; on a 4-vCPU CPU server this took a
1,000-word article from ~14 s to ~4.5 s with identical findings on every
measured set (docs/SEMANTIC.md §12).

Boundary refinement
-------------------
A sliding window never lands on the verse boundary: it clips the quotation and
swallows surrounding prose, which drags similarity down and makes genuinely
APPROVED text look UNATTRIBUTED. In the prototype this one step moved
attribution accuracy from 64% to 100%. `refine_span` is that step; treat it as
load-bearing, not polish.

Performance
-----------
Similarity is engine.sim(): 0.6 * SequenceMatcher ratio + 0.4 * token Jaccard.
The hot loop computes the SAME number faster, never an approximation of it:
approved renderings are normalised once (the corpus stores `text_norm`, equal
to engine.normalize(text) for all 137,180 rows), each rendering keeps a
SequenceMatcher with its character index already built, and a comparison is
skipped only when a provable upper bound on sim() says it cannot win (see
`_sim_upper_bound`). The one deliberate approximation is in SEEDING, where only
the few editions closest by Jaccard are compared exactly (SEED_EDITIONS); the
reported span score is always recomputed exactly over every edition.
"""
from __future__ import annotations

import heapq
import logging
import math
import os
import re
import sqlite3
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from operator import itemgetter

# `sim` is re-exported: detection scores ARE engine.sim(), and callers that
# compare against a span's score should use the same function.
from .engine import DB, Mizan, normalize, sim  # noqa: F401

log = logging.getLogger("mizan.detect")

# A window must reach this blended similarity to be reported at all.
SPAN_THRESHOLD = 0.55

# A publisher quoting part of a verse ("...and the sky a ceiling...") is by
# construction dissimilar to the WHOLE verse, so whole-verse sim() scores it
# ~0.4 and the span is discarded -- taking a CORRECT tier-(a) answer with it.
# Measured on 473 real quotations: tier (a) was right 262/262 times, but only
# 26% of those answers survived this threshold.
#
# containment() asks the question that actually matters for a partial quote:
# of the words in the quoted span, how many appear in the approved rendering?
# A faithful excerpt scores near 1.0 no matter how short it is; unrelated
# prose still scores low, so the gate keeps its teeth.
PARTIAL_THRESHOLD = 0.80


# Tier (a) partial-quotation support (see SpanDetector._place_cited).
# A contained run must hold at least this many of the verse's tokens...
CONTAINED_MIN_TOKENS = 5
# ...containment is measured only against renderings at most this many times
# the verse's median length (tafsir editions carry commentary)...
PLAIN_LENGTH_FACTOR = 1.6
# ...and the text AFTER a citation must beat the text before it by this much.
CITED_AFTER_MARGIN = 0.10
# Two non-overlapping spans of one verse closer than this are one quotation
# split by refinement; further apart they are two quotations, each checked.
SAME_VERSE_GAP = 5

_FOOTNOTE_SEP = re.compile(r"_{4,}")


def containment(span_text: str, approved: str) -> float:
    """Fraction of the span's words that occur in the approved rendering.

    Asymmetric on purpose: a short faithful excerpt of a long verse should
    score high, while the reverse (a long span loosely echoing a short verse)
    should not.
    """
    a = normalize(span_text).split()
    b = set(normalize(approved).split())
    if not a or not b:
        return 0.0
    return sum(1 for w in a if w in b) / len(a)

# ...but seeding is deliberately permissive: a clipped window scores low, and
# refinement is what rescues it. Judging before refinement is the mistake that
# cost the prototype 36 points of accuracy.
SEED_THRESHOLD = 0.42

# Tokens appearing in a large share of renderings ("the", "allah", "و") carry no
# positional signal. Only tokens above this IDF join the inverted index.
MIN_IDF = 2.0

# A span shorter than this is never reportable from the lexical tier. Verses
# this short are still detectable when the AUTHOR cites them (tier a) or the
# Arabic side resolved them — both paths bypass this floor deliberately.
MIN_SPAN_WORDS = 6

# --- The short-span gate (false positives on ordinary prose) -----------------
#
# Measured on 120 no-quotation paragraphs of real da'wah prose, 38.3% produced
# a spurious finding, and the spurious spans clustered hard at the old 6-word
# floor (median width 7). Short windows of religious prose share function
# words plus one or two theological terms ("the Books of Abraham, the Torah of
# Moses") with SOME short verse among 6,236, and sim() clears 0.55 by chance.
#
# A flat higher word floor would also delete every genuine short quotation
# ("Say: He is Allah, the One"). What separates the two is how CLOSE the short
# span is: on the dev split, chance collisions sit in the 0.55-0.75 band, while
# short spans scoring higher are almost all real Quranic wording — including
# formula verses repeated verbatim across surahs ("Thus do We reward the doers
# of good" is 37:80, 37:105, 37:110 and 37:121). So a lexical span shorter than
# SHORT_SPAN_WORDS must reach SHORT_SPAN_THRESHOLD — engine.T_NEAR: a short span
# is reported only if it is itself at least NEAR-quality to an approved
# rendering. Longer spans are judged by SPAN_THRESHOLD as before.
#
# Chosen on the DEV split (440 negatives not in the 120-paragraph probe, plus
# approved / partial / held-out renderings of dev verses inserted into prose):
# 12 words / 0.75 kept 91.5% of correct spans and cut spurious spans in the
# dev negatives from 139 to 8. Rare-token requirements were tried and kept
# fewer true spans for no gain (short Hindi verses use common words).
SHORT_SPAN_WORDS = 12
SHORT_SPAN_THRESHOLD = 0.75

# How many retrieved candidates get an exact similarity comparison per window.
# Retrieval returns up to 40; scoring all of them dominates runtime on long
# documents while almost never changing the outcome, because the IDF ranking
# puts the true verse at the top. 8 -> 5 on the dev split: identical recall,
# 17% less time (with SEED_REL_MIN pruning the tail anyway).
SEED_CANDIDATES = 5

# During seeding only, compare each candidate verse against its SEED_EDITIONS
# renderings closest by token Jaccard (cheap set arithmetic) instead of all of
# them — English has 15 editions. The seed only chooses WHICH verse to refine
# toward; the refined span is then scored exactly against every edition.
SEED_EDITIONS = 2

# Seeding economics (latency). Each lever was measured on the dev split (560
# inserted quotations + 440 negatives) and on the 12 real ~1,000-word
# documents; together they took dev latency from 359 to 225 ms per 100 words
# and the real-document median from 2.5 s to 1.6 s (max 4.9 s -> 2.7 s), with
# dev recall 0.721 -> 0.720 and an unchanged false-positive rate.
#
# A lexical candidate is compared exactly only if its retrieval score is at
# least SEED_REL_MIN of the window's best (semantic proposals always are).
SEED_REL_MIN = 0.4
# Window stride = size // WINDOW_STRIDE_DIV for windows up to 50 words (was 4).
# Refinement moves edges anyway, so a slightly coarser grid loses nothing.
WINDOW_STRIDE_DIV = 3
# Once a span has been accepted, later windows lying entirely inside it are not
# seeded again: refinement from any seed inside a quotation climbs to the same
# quotation, so re-seeding it only repeats work.
SKIP_INSIDE_ACCEPTED = True

# Upper bound on hill-climbing moves per span. Bounds worst-case latency on
# pathologically long documents; normal spans converge far below it.
MAX_REFINE_STEPS = 400

# Base window sizes in words, spread geometrically.
#
# These are a FLOOR, not the whole story. Several indexed editions are
# footnoted or tafsir renderings whose "verses" run far longer than the verse
# itself — measured p95 lengths: Bengali 1967 = 504 words, Urdu 1966 = 361,
# Hindi 1986 = 119, against ~60 for a plain English edition. A fixed 50-word
# ceiling cannot cover those at all, so the detector silently missed every long
# rendering in those languages. `window_sizes_for` extends the ladder from the
# corpus instead of guessing.
BASE_WINDOW_SIZES = (8, 12, 18, 26, 36, 50)

# Longest window we will ever slide. Beyond this the O(n·window) scoring cost
# stops being worth it, and a quotation that long is better found by its
# printed reference or from the Arabic side.
MAX_WINDOW = 320

# --- Tier (c) settings --------------------------------------------------------
#
# Sentence chunks are what the model reads. A chunk is a sentence, a pair of
# adjacent sentences (verses often span two), or a 32-word slice of a very
# long sentence. ~120 chunks per 1,000 words -> one batched forward pass.
SEM_CHUNK_MIN_WORDS = 5
SEM_CHUNK_MAX_WORDS = 48
SEM_CHUNK_SLICE = 32
SEM_PAIR_MAX_WORDS = 80

# (1) Candidate augmentation. A chunk's top SEM_AUG_PER_CHUNK verses scoring at
# least SEM_AUG_MIN are offered to every lexical window centred in the chunk.
# They then face exactly the same sim()-based seeding, refinement and gate as
# lexical candidates, so augmentation can change WHICH verse a span is tied to
# but cannot lower the bar a span must clear. Dev split: +9 located quotations
# of 560 over semantic-only spans alone, with no false-positive change; top-1
# per chunk gave the same gain as top-3 at ~10% less detection time.
SEM_AUG_MIN = 0.60
SEM_AUG_PER_CHUNK = 1

# (2) Semantic-only spans: a chunk the lexical tier did not claim, whose top
# verse is proposed confidently, is reported as a located quotation with tier
# "semantic". This is the paraphrase / unindexed-translation case. Its
# attribution state still comes from engine.attribute() — in practice NEAR or
# UNATTRIBUTED, i.e. a referral — never from the model.
#
# "Confidently" means either a high score, or a good score that clearly beats
# the runner-up verse. The margin is what separates a quotation from prose
# that is merely ABOUT a verse's topic: a quotation points at one verse, while
# topical prose resembles several verses about equally (dev negatives scoring
# >= 0.72 had margins of 0.001-0.066). Calibrated on the dev split so that the
# false-positive rate on 440 dev negatives did not move (10 -> 10 paragraphs)
# while located quotations rose 403 -> 426 of 560 (held-out 47 -> 61 of 140).
SEM_SPAN_MIN = 0.82
SEM_SPAN_MARGIN_FLOOR = 0.75
SEM_SPAN_MARGIN = 0.08
# Optional floor on ordinary textual similarity for semantic-only spans. 0 by
# measurement: at the gate above it removed true paraphrases and no false
# positives. Kept as a knob for languages calibrated later.
SEM_SPAN_MIN_SIM = 0.0
SEM_SPAN_MIN_WORDS = 8

# --- Which sentences tier (c) embeds, and in what order (latency) -----------
#
# Embedding is the whole cost of tier (c): ~2.3 ms per token on 4 Linux vCPUs
# (batch 4), and embedding every sentence and adjacent pair of a 1,000-word
# article is 4,384 tokens. Two things keep that bounded without changing what
# short texts get:
#
#   explained   a sentence at least half covered by a span the deterministic
#               tiers already reported is not embedded: tier (c) cannot report
#               a span there (_from_semantic skips it) and the lexical window
#               there is already settled.
#   budget      at most `token_budget()` tokens per request (default
#               SEM_TOKEN_BUDGET, env MIZAN_SEM_TOKEN_BUDGET), spent in four
#               rounds of decreasing priority:
#                 1. sentences that are lexically VERSE-LIKE (`scripture_
#                    likeness` >= SEM_GATE_MIN), most verse-like first;
#                 2. adjacent pairs one of whose sentences the model already
#                    placed with score >= SEM_PAIR_GATE;
#                 3. every other sentence;
#                 4. every other pair.
#               A text that fits the budget gets all four rounds, i.e. the full
#               semantic pass. A longer one gets the most promising chunks;
#               whatever did not fit is listed in last_semantic["dropped"] and
#               logged — never silently.
#
# Why priority and not a filter: as a hard filter (embed only verse-like
# sentences) the gate was measured lossless on the English/Hindi/Tagalog DEV
# split but NOT on held-out Urdu, Bengali and Tagalog quotations, whose wording
# shares little vocabulary with our approved renderings: real-corpus Urdu
# detections fell 59 -> 42. As an ordering it costs nothing on any text within
# the budget, and the budget (1,024 tokens) exceeds every real-corpus item
# (max 955) and all but a handful of held-out items (max 1,091).
#
# SEM_SELECT = "all" embeds every chunk with no budget (the pre-2026-10-04
# plan), for measurement.
SEM_SELECT = "budgeted"
SEM_GATE_MIN = 0.35
SEM_GATE_CANDIDATES = 40
SEM_PAIR_GATE = 0.70
SEM_TOKEN_BUDGET = 1024
# Tier of a lexical span whose verse was proposed by the model (tier c).
TIER_LEXICAL_SEEDED = "lexical+semantic"


def token_budget() -> int:
    """Tokens tier (c) may embed per request (a latency knob: ~2.3 ms each on
    4 vCPUs). Raise it on bigger servers with MIZAN_SEM_TOKEN_BUDGET."""
    try:
        return int(os.environ.get("MIZAN_SEM_TOKEN_BUDGET", SEM_TOKEN_BUDGET))
    except ValueError:
        return SEM_TOKEN_BUDGET


# The candidate count the semantic contract promises.
SEMANTIC_TOP_K = 20

# ---------------------------------------------------------------- tier (a)

# (Quran 51:56) · [Qur'an 2:255] · Surah 2:255 · Q 2:255 · القرآن ٥١:٥٦
_LATIN_REF = re.compile(
    r"""[\[\(]?\s*
        (?:qur\s?[’'`]?\s?an|quran|koran|surah|surat|sura|q\.?)\s*
        [\s.:,-]*?
        (\d{1,3})\s*[:：.\-]\s*(\d{1,3})
        \s*[\]\)]?""",
    re.IGNORECASE | re.VERBOSE,
)

# القرآن ٥١:٥٦ · سورة البقرة: ٢٥٥ · [٢:٢٥٥]
_ARABIC_REF = re.compile(
    r"(?:القرآن|القران|سورة|الآية|اية)\s*[^\d٠-٩]{0,20}?"
    r"([0-9٠-٩]{1,3})\s*[:：]\s*([0-9٠-٩]{1,3})"
)

# A bare bracketed pair, e.g. "(2:255)". Weakest form — only trusted when the
# numbers are a valid verse id.
_BARE_REF = re.compile(r"[\[\(]\s*([0-9٠-٩]{1,3})\s*[:：]\s*([0-9٠-٩]{1,3})\s*[\]\)]")
# A bracketed reference led by a surah NAME, as most da'wah sites print it:
# "(an-Nahl 16:127)", "[al-Baqarah 2:183]", "( Sad 38:44)", "(Al-Imran3:146)",
# "(al-Baqarah 2:155-7)". Before 2026-10-04 these were not read at all, so
# they were not walls and the next quotation's span swallowed them (found on
# a never-seen article). The name is not validated against a surah list —
# transliterations vary too much — so a typo'd name ("Lugman 31:31") still
# reads as a reference; the numbers must close the bracket.
_NAMED_REF = re.compile(
    r"[\(\[]\s*[A-Za-z][^()\[\]\n]{0,40}?([0-9]{1,3})\s*[:：]\s*([0-9]{1,3})"
    r"(?:\s*[-–—]\s*[0-9]{1,3})?\s*[\)\]]")
# The same in Arabic script, as Urdu sermons print it: "الکہف18:28",
# "طہ20 : 132", "(البقرة، 2/ 153)". Found on a never-seen Urdu article.
_ARABIC_NAMED_REF = re.compile(
    r"(?<![\w])[\u0621-\u064A\u0671-\u06D3ۃ]{2,15}\s*[،,]?\s*"
    r"([0-9٠-٩۰-۹]{1,3})\s*[:：/]\s*([0-9٠-٩۰-۹]{1,3})")
# A reference to a hadith collection is not a verse reference, whatever its
# numbers look like: "(Bukhari 1:23)", "(البخاری، 1/23)".
_HADITH_SOURCE = re.compile(
    r"bukh[aā]r|muslim|tirmidh|ab[uū]\s*d[aā]w|n[aā]s[aā]'?[iī]|ibn\s*m[aā]j|ahmad|musnad|"
    r"muwat|bayhaq|tabar[aā]n|h[aā]kim|d[aā]rim|"
    r"بخار|مسلم|ترمذ|داؤد|داود|نسائ|ماج[ہه]|احمد|أحمد|مسند|موط|بيهق|بیہق|طبران|حاکم|حاكم|دارم",
    re.IGNORECASE)

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

# A word that ends a sentence: terminal punctuation in Latin, Devanagari /
# Bengali (danda), Urdu (full stop U+06D4) or Arabic, optionally followed by
# closing quotes or brackets.
_SENTENCE_END = re.compile(r"[.!?।॥۔؟]+[\"'”’»)\]]*$")
_WORD = re.compile(r"\S+")
_HAS_WORD_CHAR = re.compile(r"[^\W\d_]")


@dataclass
class Span:
    """A located quotation inside a translation."""

    surah: int
    ayah: int
    start_word: int        # inclusive index into text.split()
    end_word: int          # exclusive
    text: str              # the published words, exactly as written
    score: float           # blended similarity to the best approved rendering
    tier: str              # "reference" | "arabic" | "lexical" | "lexical+semantic" | "semantic"
    best_book: int | None = None   # book_id whose rendering scored highest
    # Retrieval confidence from tier (c), when the model proposed this verse.
    # Informational: it never feeds the attribution state.
    semantic_score: float | None = None
    # For a span tied to a PRINTED or ARABIC reference: the share of its words
    # found in the closest approved rendering, and that rendering's book_id.
    # A faithful partial quotation ("...the Alive, the Eternal") scores low on
    # whole-verse similarity but ~1.0 here. Exposed so the report layer can
    # say "partial quotation of <edition>" instead of only "unattributed".
    coverage: float | None = None
    coverage_book: int | None = None
    # Exact engine.sim() of the span against its closest approved rendering.
    # Equals `score` except for printed-reference spans, whose `score` may be
    # lifted by `coverage` (a faithful partial quotation). Used to choose
    # between overlapping spans of the same verse: the one whose words best
    # match a whole approved rendering is the one attribution should judge.
    similarity: float | None = None

    @property
    def whole_similarity(self) -> float:
        return self.score if self.similarity is None else self.similarity

    @property
    def ref(self) -> str:
        return f"{self.surah}:{self.ayah}"

    def as_dict(self) -> dict:
        return {
            "ref": self.ref, "surah": self.surah, "ayah": self.ayah,
            "start_word": self.start_word, "end_word": self.end_word,
            "text": self.text, "score": round(self.score, 3),
            "tier": self.tier, "best_book": self.best_book,
            "semantic_score": (None if self.semantic_score is None
                               else round(self.semantic_score, 3)),
            "coverage": None if self.coverage is None else round(self.coverage, 3),
            "coverage_book": self.coverage_book,
        }


def printed_reference_spans(text: str, max_surah: int = 114,
                            max_ayah: int = 286) -> list[tuple[int, int, int, int]]:
    """
    Tier (a) with extents: (surah, ayah, start_char, end_char) per citation.

    The extent matters as much as the id. A citation is a hard boundary in the
    text — a quotation ends where its "(Quran 3:59)" begins, or starts where it
    ends — and knowing exactly which characters belong to the citation is what
    stops a span from running across it into the author's next sentence.
    """
    out: list[tuple[int, int, int, int]] = []
    seen: set[tuple[int, int, int]] = set()
    covered: list[tuple[int, int]] = []
    for pattern in (_LATIN_REF, _ARABIC_REF, _BARE_REF, _NAMED_REF, _ARABIC_NAMED_REF):
        for m in pattern.finditer(text):
            try:
                s = int(m.group(1).translate(_ARABIC_DIGITS))
                a = int(m.group(2).translate(_ARABIC_DIGITS))
            except ValueError:
                continue
            if not (1 <= s <= max_surah and 1 <= a <= max_ayah):
                continue
            named = pattern is _NAMED_REF or pattern is _ARABIC_NAMED_REF
            if named and _HADITH_SOURCE.search(m.group(0)):
                continue
            # The named patterns also match "(Quran 2:255)"; one citation,
            # one entry.
            if named and any(c0 <= m.end() - 1 and m.start() <= c1 - 1
                             for c0, c1 in covered):
                continue
            covered.append((m.start(), m.end()))
            key = (s, a, m.start())
            if key in seen:
                continue
            seen.add(key)
            out.append((s, a, m.start(), m.end()))
    return sorted(out, key=lambda t: t[2])


def printed_references(text: str, max_surah: int = 114) -> list[tuple[int, int, int]]:
    """
    Tier (a): verse ids the author printed explicitly.

    Returns (surah, ayah, char_offset) so the caller knows roughly where in the
    document the claim was made — a quotation normally sits just before its
    citation.

    Validity is checked structurally (1 <= surah <= 114, ayah >= 1) rather than
    against the corpus, so this stays usable without a database handle.
    """
    return [(s, a, start) for s, a, start, _ in printed_reference_spans(text, max_surah)]


def citation_word_ranges(text: str) -> list[tuple[int, int, int, int]]:
    """
    Printed citations as WORD ranges: (surah, ayah, first_word, end_word).

    A word belongs to the citation if it STARTS inside it. So in
    'Adam."(Quran 3:59).' the glued word 'Adam."(Quran' stays with the quotation
    and only '3:59).' is the citation; in 'Adam." (Quran 3:59).' both '(Quran'
    and '3:59).' are.
    """
    starts = [m.start() for m in _WORD.finditer(text)]
    out = []
    for s, a, c0, c1 in printed_reference_spans(text):
        idx = [i for i, st in enumerate(starts) if c0 <= st < c1]
        if idx:
            out.append((s, a, idx[0], idx[-1] + 1))
        else:
            # The citation began mid-word and ended in it; block that word.
            j = max(0, sum(1 for st in starts if st < c0) - 1)
            out.append((s, a, j, j + 1))
    return out


# ---------------------------------------------------------------- tier (c)

def semantic_available(lang: str | None = None) -> bool:
    """True if tier (c) can run here (ML deps + pinned model + built index).

    Cheap: imports no ML code. `MIZAN_SEMANTIC=0` forces False.
    """
    from . import semantic
    return semantic.available(lang)


def semantic_candidates(text: str, lang: str) -> list[tuple[int, int, float]]:
    """
    Tier (c): verses whose MEANING is closest to `text`, by bge-m3 (local).

    CONTRACT — this implementation satisfies all of these:

    Parameters
    ----------
    text : str
        One span of translated prose, typically 8-60 words. Passed exactly as
        the author wrote it: not normalised, not lowercased, punctuation intact.
    lang : str
        ISO 639-1 code matching `translations.language` in corpus.sqlite —
        currently one of {"en", "ur", "bn", "hi", "tl"}.

    Returns
    -------
    list[tuple[int, int, float]]
        Up to 20 candidates, as (surah, ayah, score), sorted by score
        DESCENDING (ties broken by verse id). `score` is in [0.0, 1.0] and is a
        RETRIEVAL CONFIDENCE, not a verdict: 0.8 * cosine to the centroid of
        the approved renderings of that verse in `lang` + 0.2 * cosine to the
        Arabic verse. surah is 1..114; ayah is valid for that surah.

    Guarantees the caller relies on
    -------------------------------
    1. PURE RETRIEVAL. It proposes which verse to look up. It never decides
       MATCH/NEAR/UNATTRIBUTED — that stays with engine.sim() against the
       approved string, so the model has no authority over the verdict.
    2. NO TEXT GENERATION. It returns no string at all — only ids and scores.
       This is what keeps "no model touches scripture" structurally true.
    3. DETERMINISTIC for a given model revision (pinned in semantic.py): the
       model runs in inference mode with no sampling, and scores are rounded
       to 4 decimals before ranking so backend float noise cannot reorder them.
    4. OFFLINE-SAFE. Never downloads anything at call time. If the ML packages,
       the model snapshot or the index are missing it RAISES
       semantic.SemanticUnavailable; it never returns a guess. The detector
       catches that and runs the deterministic tiers — the documented fallback.
    """
    from . import semantic
    return semantic.candidates(text, lang, top_k=SEMANTIC_TOP_K)


def semantic_span_confident(score: float, margin: float) -> bool:
    """The tier-(c) gate for reporting a span on the model's word alone.

    `score` is the top verse's retrieval score, `margin` its lead over the
    second verse. See SEM_SPAN_MIN / SEM_SPAN_MARGIN for the measurement.
    """
    return score >= SEM_SPAN_MIN or (score >= SEM_SPAN_MARGIN_FLOOR
                                     and margin >= SEM_SPAN_MARGIN)


def sentence_units(text: str) -> list[tuple[int, int]]:
    """
    Sentence boundaries as word ranges [start, end) over text.split().

    A sentence ends at a word carrying terminal punctuation, or at a line
    break. Fragments shorter than SEM_CHUNK_MIN_WORDS (a heading, "Allah says:")
    are joined to the following sentence, because on their own they carry too
    little meaning to place, and a lead-in usually belongs to what follows.
    """
    bounds: list[int] = [0]
    prev_end = 0
    for i, m in enumerate(_WORD.finditer(text)):
        if i and "\n" in text[prev_end:m.start()] and bounds[-1] != i:
            bounds.append(i)
        if _SENTENCE_END.search(m.group()):
            bounds.append(i + 1)
        prev_end = m.end()
    n = len(text.split())
    if bounds[-1] != n:
        bounds.append(n)
    raw = [(a, b) for a, b in zip(bounds, bounds[1:]) if b > a]

    units: list[tuple[int, int]] = []
    carry = None
    for a, b in raw:
        if carry is not None:
            a = carry
            carry = None
        if b - a < SEM_CHUNK_MIN_WORDS:
            carry = a
            continue
        units.append((a, b))
    if carry is not None:
        if units:
            units[-1] = (units[-1][0], n)
        else:
            units.append((carry, n))
    return units


def semantic_chunks(text: str) -> list[tuple[int, int]]:
    """
    The word ranges tier (c) embeds: sentences, adjacent pairs, long-sentence slices.
    """
    chunks: list[tuple[int, int]] = []
    units = sentence_units(text)
    for a, b in units:
        if b - a <= SEM_CHUNK_MAX_WORDS:
            chunks.append((a, b))
        else:
            step = SEM_CHUNK_SLICE // 2
            s = a
            while True:
                e = min(b, s + SEM_CHUNK_SLICE)
                chunks.append((s, e))
                if e >= b:
                    break
                s += step
    for (a, b), (c, d) in zip(units, units[1:]):
        if d - a <= SEM_PAIR_MAX_WORDS:
            chunks.append((a, d))
    return sorted(set(chunks))


# ---------------------------------------------------------------- the index

@dataclass
class _LangIndex:
    """
    Everything SpanDetector needs for one language, built once per process.

    Building it means reading ~94k rows for English, so it is cached at module
    level (`_INDEX_CACHE`): report.check_document() constructs a fresh
    SpanDetector per call when the caller passes none, and without the cache
    every request would pay the build again.
    """

    editions: tuple[int, ...]
    docs: list[tuple[int, int, int, str]]       # book, surah, ayah, text
    norm: list[str]                             # engine.normalize(text), per doc
    norm_len: list[int]                         # words in norm
    raw_len: list[int]                          # words in text (raw split)
    by_ref: dict[tuple[int, int], list[tuple[int, str]]]
    ref_docs: dict[tuple[int, int], list[int]]
    df: dict[str, int]
    n_docs: int
    postings: dict[str, list[tuple[tuple[int, int], float]]]
    ref_len: dict[tuple[int, int], int]
    window_sizes: tuple[int, ...]
    toks: dict[int, frozenset] = field(default_factory=dict)   # lazily filled
    chars: dict[int, Counter] = field(default_factory=dict)    # lazily filled
    plain: dict[tuple[int, int], list] = field(default_factory=dict)   # lazily filled


_INDEX_CACHE: dict[tuple[str, str], _LangIndex] = {}
_INDEX_LOCK = threading.Lock()

# Per-thread cache of SequenceMatchers keyed by doc index. A SequenceMatcher
# holding an approved rendering as its second sequence has already paid for
# the character index (b2j); comparing a new window against it only needs
# set_seq1(). Thread-local because set_seq1 mutates the matcher.
_TLS = threading.local()
_MATCHER_CACHE_MAX = 6000


def _upper_bound(len_a: int, len_b: int, jac: float) -> float:
    """
    Provable ceiling on engine.sim() from character lengths and exact Jaccard.

    SequenceMatcher.ratio() = 2M / (len_a + len_b) with M <= min(len_a, len_b),
    so ratio <= 2*min/(len_a+len_b) — difflib's own real_quick_ratio(). The
    Jaccard term is computed exactly. A pair whose ceiling cannot beat the
    current best is skipped without running SequenceMatcher; anything that
    could win is scored exactly, so the bound never changes a result.
    """
    total = len_a + len_b
    rqr = (2.0 * min(len_a, len_b) / total) if total else 0.0
    return 0.6 * rqr + 0.4 * jac


def _free_region(start: int, end: int, blocked, n_words: int) -> tuple[int, int, int, int]:
    """
    Cut [start, end) back to its largest piece free of `blocked` ranges, and
    return (start, end, lo, hi) where [lo, hi) is the wall-to-wall region an
    edge may move in. Earlier piece wins a tie (a quotation precedes its
    citation far more often than it follows it).
    """
    pieces = [(start, end)]
    for bs, be in sorted(blocked):
        nxt = []
        for a, b in pieces:
            if be <= a or bs >= b:
                nxt.append((a, b))
                continue
            if bs > a:
                nxt.append((a, bs))
            if be < b:
                nxt.append((be, b))
        pieces = nxt
    if not pieces:
        return start, start, start, start
    a, b = max(pieces, key=lambda p: (p[1] - p[0], -p[0]))
    lo = max([be for bs, be in blocked if be <= a], default=0)
    hi = min([bs for bs, be in blocked if bs >= b], default=n_words)
    return a, b, lo, hi


# Urdu prints quotations as ’’…‘‘ (opening ’’, closing ‘‘).
_OPEN_QUOTE = re.compile(r'^[\[(]*(?:["“«„]|’’)')
_CLOSE_QUOTE = re.compile(r'(?:["”»]|‘‘)[.,;:!?)\]۔،]*$')
QUOTE_SNAP_WORDS = 3
# Snapping is kept only if the span's whole-verse similarity does not fall by
# more than this. A pair of quotation marks can enclose TWO verses ("...no
# idle talk..." 56:25 followed by "...only 'Peace, peace'" 56:26); extending
# 56:25's span to the closing mark then drags its similarity from 0.857 to
# 0.734 and turns a NEAR into a referral. Measured on the real corpus.
QUOTE_SNAP_TOLERANCE = 0.03


def snap_to_quotes(words: list[str], start: int, end: int, lo: int = 0,
                   hi: int | None = None) -> tuple[int, int]:
    """
    Extend a span to the quotation marks the author put around it.

    Similarity-driven refinement stops where the score peaks, which can be a
    word or two short of what the author actually quoted: '"...except to serve
    Me."' peaks before "serve Me" because the closest approved edition says
    "worship Me". When the span opens at (or just after) an opening quotation
    mark and the matching closing mark is at most QUOTE_SNAP_WORDS words away,
    the published quotation is the better boundary — the reviewer should see
    everything the author put in quotes, and attribution should judge all of it.
    Never crosses `lo`/`hi` (citation walls). Prose with no quotation marks is
    untouched.
    """
    hi = len(words) if hi is None else hi
    s, e = start, end
    if not (0 <= s < e <= len(words)):
        return start, end
    opened = None
    for k in range(s, max(lo, s - QUOTE_SNAP_WORDS) - 1, -1):
        if _OPEN_QUOTE.search(words[k]):
            opened = k
            break
        if k < s and _CLOSE_QUOTE.search(words[k]):
            break
    if opened is None:
        return start, end
    if any(_CLOSE_QUOTE.search(w) for w in words[opened:s]):
        return start, end
    s = opened
    if not _CLOSE_QUOTE.search(words[e - 1]):
        for k in range(e, min(hi, e + QUOTE_SNAP_WORDS)):
            if _OPEN_QUOTE.search(words[k]) and k > e:
                break
            if _CLOSE_QUOTE.search(words[k]):
                e = k + 1
                break
    return s, e


class SpanDetector:
    """
    Locates Quranic quotations inside a translation, in one language.

    One instance per language. The underlying index is cached per process, so
    constructing a second detector for the same language is nearly free.

        det = SpanDetector(mizan, "en")
        spans = det.find(paragraph)

    `semantic`: None (default) uses tier (c) whenever `semantic.available(lang)`
    — so a machine with the ML extras gets it and plain python3 does not;
    True/False force it on/off. The `MIZAN_SEMANTIC` environment variable
    (0/1) overrides the default for every detector, which is how the
    deterministic path is measured on a machine that has the model.
    """

    def __init__(self, mizan: Mizan, lang: str, db: str = DB,
                 semantic: bool | None = None):
        self.m = mizan
        self.lang = lang
        self.editions = mizan.editions(lang)
        if not self.editions:
            raise ValueError(f"no approved translations indexed for language {lang!r}")
        self._ix = self._get_index(db)
        ix = self._ix
        self.docs = ix.docs
        self.by_ref = ix.by_ref
        self.n_docs = ix.n_docs
        self._df = ix.df
        self._ref_len = ix.ref_len
        self.window_sizes = ix.window_sizes
        self._semantic_pref = semantic
        # Why the last find() did or did not use tier (c) — for UI and tests.
        self.last_semantic: dict = {"used": False, "reason": "not run yet"}

    # ---------------------------------------------------------------- setup

    def _get_index(self, db: str) -> _LangIndex:
        key = (os.path.abspath(db), self.lang)
        ix = _INDEX_CACHE.get(key)
        if ix is not None and ix.editions == tuple(self.editions):
            return ix
        with _INDEX_LOCK:
            ix = _INDEX_CACHE.get(key)
            if ix is None or ix.editions != tuple(self.editions):
                ix = self._build_index(db)
                _INDEX_CACHE[key] = ix
        return ix

    def _build_index(self, db: str) -> _LangIndex:
        """
        Build the rare-token inverted index over every approved rendering.

        Postings are aggregated to VERSE level at build time: each entry holds
        idf * (editions of this verse containing the token) / (editions of this
        verse) / sqrt(verse length) — exactly the per-edition sum the retrieval
        score used to accumulate at query time, with the edition averaging and
        length normalisation (see lexical_candidates) folded in. English has 15
        editions, so this shortens every posting list by up to 15x.
        """
        con = sqlite3.connect(db, check_same_thread=False)
        placeholders = ",".join("?" * len(self.editions))
        rows = con.execute(
            f"SELECT book_id, surah, ayah, text, text_norm FROM ayat "
            f"WHERE book_id IN ({placeholders})", self.editions
        ).fetchall()
        con.close()

        docs: list[tuple[int, int, int, str]] = []
        norms: list[str] = []
        df: dict[str, int] = defaultdict(int)
        token_sets: list[set[str]] = []
        for book_id, surah, ayah, text, text_norm in rows:
            # text_norm is engine.normalize(text), verified equal for every row
            # of the 2026-10-01 dump; recompute only if a row lacks it.
            nt = text_norm if text_norm is not None else normalize(text)
            toks = set(nt.split())
            if not toks:
                continue
            docs.append((book_id, surah, ayah, text))
            norms.append(nt)
            token_sets.append(toks)
            for t in toks:
                df[t] += 1
        n_docs = max(1, len(docs))

        by_ref: dict[tuple[int, int], list[tuple[int, str]]] = defaultdict(list)
        ref_docs: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i, (book_id, surah, ayah, text) in enumerate(docs):
            by_ref[(surah, ayah)].append((book_id, text))
            ref_docs[(surah, ayah)].append(i)
        norm_len = [len(n.split()) for n in norms]
        ref_len = {key: min(norm_len[i] for i in idxs) for key, idxs in ref_docs.items()}

        # verse-level postings
        counts: dict[str, dict[tuple[int, int], int]] = defaultdict(lambda: defaultdict(int))
        for i, toks in enumerate(token_sets):
            key = (docs[i][1], docs[i][2])
            for t in toks:
                counts[t][key] += 1
        postings: dict[str, list[tuple[tuple[int, int], float]]] = {}
        for t, per_ref in counts.items():
            idf = math.log(n_docs / (1 + df[t]))
            if idf < MIN_IDF:
                continue
            postings[t] = [
                (key, idf * c / max(1, len(ref_docs[key]))
                 / math.sqrt(max(1, ref_len.get(key, 1))))
                for key, c in per_ref.items()
            ]

        ix = _LangIndex(
            editions=tuple(self.editions), docs=docs, norm=norms, norm_len=norm_len,
            raw_len=[len(d[3].split()) for d in docs], by_ref=dict(by_ref),
            ref_docs=dict(ref_docs), df=dict(df), n_docs=n_docs, postings=postings,
            ref_len=ref_len, window_sizes=(),
        )
        ix.window_sizes = self._window_sizes_from(ix.raw_len)
        return ix

    def idf(self, token: str) -> float:
        """Inverse document frequency over renderings. High = discriminative."""
        return math.log(self.n_docs / (1 + self._df.get(token, 0)))

    @staticmethod
    def _window_sizes_from(lengths_raw: list[int]) -> tuple[int, ...]:
        """
        Window ladder sized from the corpus actually indexed for this language.

        Taking the 95th percentile rendering length (rather than the max) keeps
        the ladder short while still covering the footnoted editions; the few
        extreme outliers are reachable through refinement, which can grow a
        span well past its seed.
        """
        lengths = sorted(lengths_raw)
        if not lengths:
            return BASE_WINDOW_SIZES
        p95 = lengths[int(0.95 * (len(lengths) - 1))]
        sizes = list(BASE_WINDOW_SIZES)
        size = sizes[-1]
        while size < min(p95, MAX_WINDOW):
            size = int(size * 1.8)
            sizes.append(min(size, MAX_WINDOW))
        return tuple(sorted(set(sizes)))

    def _window_sizes(self) -> tuple[int, ...]:
        """Kept for callers of the old name."""
        return self._window_sizes_from(self._ix.raw_len)

    # ----------------------------------------------------- exact fast sim()

    def _toks(self, i: int) -> frozenset:
        t = self._ix.toks.get(i)
        if t is None:
            t = frozenset(self._ix.norm[i].split())
            self._ix.toks[i] = t
        return t

    def _matcher(self, i: int) -> SequenceMatcher:
        cache = getattr(_TLS, "sm", None)
        if cache is None or getattr(_TLS, "ix", None) is not self._ix:
            cache = _TLS.sm = {}
            _TLS.ix = self._ix
        sm = cache.get(i)
        if sm is None:
            if len(cache) >= _MATCHER_CACHE_MAX:
                cache.clear()
            # Same construction as engine.sim(): isjunk=None, autojunk default.
            sm = SequenceMatcher(None, "", self._ix.norm[i])
            cache[i] = sm
        return sm

    def _chars(self, i: int) -> Counter:
        c = self._ix.chars.get(i)
        if c is None:
            c = Counter(self._ix.norm[i])
            self._ix.chars[i] = c
        return c

    def _sim_doc(self, na: str, ta: frozenset, i: int, floor: float = -1.0,
                 ca: Counter | None = None) -> float | None:
        """
        engine.sim(text, docs[i].text) given text's normalised form `na` and
        token set `ta` — bit-for-bit the same value, computed faster.

        Returns None (without running SequenceMatcher) when the exact value
        provably cannot exceed `floor`. Two bounds, cheapest first:

        1. `_upper_bound`: character lengths + exact Jaccard.
        2. difflib's own quick_ratio argument: SequenceMatcher can never match
           more characters than sum over c of min(count_a(c), count_b(c)), so
           0.6 * 2*that/(len_a+len_b) + 0.4 * Jaccard bounds sim() from above.
           `ca` (the Counter of `na`) is built once per window by the caller
           and each rendering's Counter is cached, so the bound costs a few
           microseconds against ~150 for a SequenceMatcher.ratio().
        """
        nb = self._ix.norm[i]
        if not na or not nb:
            return 0.0 if floor < 0.0 else None
        tb = self._toks(i)
        union = len(ta | tb)
        jac = len(ta & tb) / union if union else 0.0
        if floor >= 0.0:
            total = len(na) + len(nb)
            if _upper_bound(len(na), len(nb), jac) <= floor:
                return None
            if ca is None:
                ca = Counter(na)
            cb = self._chars(i)
            m = 0
            for ch, k in ca.items():
                kb = cb.get(ch)
                if kb:
                    m += k if k < kb else kb
            if 0.6 * (2.0 * m / total) + 0.4 * jac <= floor:
                return None
        sm = self._matcher(i)
        sm.set_seq1(na)
        return 0.6 * sm.ratio() + 0.4 * jac

    # ------------------------------------------------------------- tier (b)

    def lexical_candidates(self, window: str, top_k: int = 40) -> list[tuple[int, int, float]]:
        """
        Tier (b): verses sharing rare vocabulary with this window.

        Scores by summed IDF of shared rare tokens, averaged across the editions
        of a verse (so a verse indexed in ten editions does not outrank one
        indexed in two) and divided by sqrt(verse length). Raw IDF mass favours
        LONG verses, which simply contain more rare tokens — so a short verse
        built from common words (112:1 "Say: He is Allah, the One!") never
        surfaced at all. sqrt rather than linear: full length normalisation
        over-corrects and floods the list with 2-word verses. Both factors are
        folded into the postings at build time.

        Tokens are visited in sorted order and ties are broken by verse id, so
        the ranking is identical across processes (Python's per-process string
        hashing would otherwise reorder exact ties).
        """
        postings = self._ix.postings
        toks = sorted(t for t in set(normalize(window).split()) if t in postings)
        if not toks:
            return []
        hits: dict[tuple[int, int], float] = defaultdict(float)
        for t in toks:
            for key, w in postings[t]:
                hits[key] += w
        # nlargest on the bare score is C-fast; ties are then ordered by verse id.
        # Deterministic either way, because tokens were visited in sorted order.
        ranked = heapq.nlargest(top_k, hits.items(), key=itemgetter(1))
        ranked.sort(key=lambda kv: (-kv[1], kv[0]))
        top = ranked[0][1] or 1.0
        return [(s, a, score / top) for (s, a), score in ranked]

    # ----------------------------------------------------------- refinement

    @staticmethod
    def _length_ceiling(a_words: int, b_words: int) -> float:
        """
        Word-count ratio. Kept for callers, but NO LONGER USED FOR PRUNING.

        It was documented as an upper bound on sim(), and it is not one:
        SequenceMatcher works on characters, so an 8-word window against a
        20-word verse has a word ratio of 0.40 but a true ceiling near 0.50
        (2*min/(sum) on characters, plus the Jaccard term). Pruning with it
        could discard the best edition of a verse. `_upper_bound` replaced it.
        """
        if a_words == 0 or b_words == 0:
            return 0.0
        return min(a_words, b_words) / max(a_words, b_words)

    def _best_doc(self, surah: int, ayah: int, text: str) -> tuple[int | None, float]:
        """(doc index, exact sim) of the approved rendering closest to `text`."""
        idxs = self._ix.ref_docs.get((surah, ayah))
        if not idxs:
            return None, 0.0
        na = normalize(text) if text else ""
        ta = frozenset(na.split())
        ca = Counter(na)
        best_i, best_s = None, -1.0
        for i in idxs:
            # Pruned only by the provable bound inside _sim_doc, so this is the
            # true argmax over every edition (first edition wins exact ties).
            s = self._sim_doc(na, ta, i, floor=best_s, ca=ca)
            if s is not None and s > best_s:
                best_i, best_s = i, s
        return best_i, best_s

    def best_rendering(self, surah: int, ayah: int, text: str) -> tuple[int | None, str, float]:
        """
        Closest approved rendering of this verse to `text`: (book_id, text, score).

        Renderings whose length makes a good score arithmetically impossible are
        skipped via `_upper_bound` — this is what keeps detection tractable on
        the footnoted editions, whose renderings run to hundreds of words. The
        bound is provable, so the result is the true best edition and the score
        is exactly engine.sim().
        """
        i, s = self._best_doc(surah, ayah, text)
        if i is None:
            return None, "", -1.0
        return self.docs[i][0], self.docs[i][3], s

    def _seed_score(self, na: str, ta: frozenset, n_text: int,
                    cands: list[tuple[int, int, float]],
                    floor: float | None = None) -> tuple[float, int, int] | None:
        """
        Best (score, surah, ayah) among candidate verses for one window, or None
        if no candidate can reach SEED_THRESHOLD.

        For each verse only the SEED_EDITIONS renderings closest by Jaccard are
        compared exactly (see the constant). Everything else is exact pruning.
        """
        best: tuple[float, int, int] | None = None
        # `floor` lets the augmented pass score only NEW candidates against
        # the best seed phase 1 already found for this window.
        floor = SEED_THRESHOLD - 1e-12 if floor is None else floor
        ca = Counter(na)
        for surah, ayah, _ in cands:
            idxs = self._ix.ref_docs.get((surah, ayah))
            if not idxs:
                continue
            if len(idxs) > SEED_EDITIONS:
                scored = []
                for i in idxs:
                    tb = self._toks(i)
                    u = len(ta | tb)
                    scored.append((-(len(ta & tb) / u if u else 0.0), i))
                scored.sort()
                idxs = [i for _, i in scored[:SEED_EDITIONS]]
            for i in idxs:
                s = self._sim_doc(na, ta, i, floor=floor, ca=ca)
                if s is not None and s > floor:
                    floor = s
                    best = (s, surah, ayah)
        return best

    def refine_span(self, words: list[str], start: int, end: int,
                    surah: int, ayah: int, lo: int | None = None,
                    hi: int | None = None,
                    blocked: list[tuple[int, int]] | tuple = ()
                    ) -> tuple[int, int, float, int | None]:
        """
        Hill-climb both span edges against the best-matching approved rendering.

        THIS IS THE STEP THAT MATTERS. A coarse window clips the quotation and
        swallows prose; similarity drops; approved text gets flagged as
        UNATTRIBUTED. Moving each edge one or two words at a time, keeping any
        move that raises similarity, recovers the true boundary.

        Measured in the prototype: attribution 64% -> 100%, falsely flagged
        approved texts 4/9 -> 0/8.

        Edges move independently and the search stops at the first local
        maximum, which is correct here because similarity is single-peaked in
        span length around the true quotation. `lo`/`hi` optionally confine the
        search (tier c uses this to stay inside the sentence it proposed).

        `blocked` word ranges (printed citations) are walls: the seed is first
        cut back to the larger side of any wall inside it, and neither edge may
        cross one. A quotation ends where "(Quran 3:59)" begins; without the
        wall the climb happily absorbs the author's next sentence whenever its
        vocabulary echoes the rest of the verse.

        Returns (start, end, score, book_id).
        """
        if blocked:
            start, end, wall_lo, wall_hi = _free_region(start, end, blocked, len(words))
            if end - start < 3:
                return start, end, 0.0, None
            lo = wall_lo if lo is None else max(lo, wall_lo)
            hi = wall_hi if hi is None else min(hi, wall_hi)
        doc_i, _ = self._best_doc(surah, ayah, " ".join(words[start:end]))
        if doc_i is None:
            return start, end, 0.0, None

        # Never search further than one verse-length beyond the seed: the
        # quotation cannot be longer than the verse plus slack.
        reach = max(4, self._ix.norm_len[doc_i])
        lo = max(0, start - reach) if lo is None else max(0, lo, start - reach)
        hi = min(len(words), end + reach) if hi is None else min(len(words), hi, end + reach)

        memo: dict[tuple[int, int], float | None] = {}

        def score(s: int, e: int, floor: float = -1.0) -> float | None:
            """Exact sim of words[s:e], or None if it provably cannot beat
            `floor`. The climb's floor (the current score) only rises, so a
            pruned move stays prunable and may be memoised as None."""
            if e <= s:
                return -1.0
            if (s, e) in memo:
                return memo[(s, e)]
            na = normalize(" ".join(words[s:e]))
            v = self._sim_doc(na, frozenset(na.split()), doc_i, floor=floor)
            memo[(s, e)] = v
            return v

        cur_s, cur_e, cur_score = start, end, score(start, end)
        improved = True
        # Hard iteration cap. The climb normally converges in a few dozen steps;
        # the cap only bites on pathological inputs (a very long footnoted
        # rendering inside a very long document) where it bounds worst-case
        # latency without affecting any normal result.
        steps = 0
        while improved and steps < MAX_REFINE_STEPS:
            improved = False
            steps += 1
            # Both edges, both directions, step 1 then 2. Step 2 escapes the
            # plateau created by a one-word function word ("and", "the").
            for ns, ne in ((cur_s - 1, cur_e), (cur_s + 1, cur_e),
                           (cur_s, cur_e - 1), (cur_s, cur_e + 1),
                           (cur_s - 2, cur_e), (cur_s + 2, cur_e),
                           (cur_s, cur_e - 2), (cur_s, cur_e + 2)):
                if ns < lo or ne > hi or ne - ns < 3:
                    continue
                v = score(ns, ne, cur_score + 1e-9)
                if v is not None and v > cur_score + 1e-9:
                    cur_s, cur_e, cur_score = ns, ne, v
                    improved = True
                    break

        # The winning edition can change once the boundary is right.
        book_id, _, cur_score = self.best_rendering(surah, ayah,
                                                    " ".join(words[cur_s:cur_e]))
        return cur_s, cur_e, cur_score, book_id

    # ------------------------------------------------------------- the gate

    def short_span_ok(self, span_text: str, score: float, surah: int = 0,
                      ayah: int = 0) -> bool:
        """
        The width-dependent bar a LEXICAL span must clear (see SHORT_SPAN_WORDS).

        `surah`/`ayah` are accepted for interface stability; the rule needs
        only the span's width and its exact similarity score.
        """
        # Count words, not punctuation tokens: '‘‘ ہے۔ عربی زبان میں ’’' is
        # four words of prose, not six (found on a never-seen Urdu article).
        n = sum(1 for w in span_text.split() if _HAS_WORD_CHAR.search(w))
        if n < MIN_SPAN_WORDS:
            return False
        if n >= SHORT_SPAN_WORDS:
            return True
        return score >= SHORT_SPAN_THRESHOLD

    # ------------------------------------------------------------- the pass

    def semantic_enabled(self, use_semantic: bool | None = None) -> bool:
        """Resolve the per-call / per-detector / environment preference."""
        pref = use_semantic if use_semantic is not None else self._semantic_pref
        env = os.environ.get("MIZAN_SEMANTIC", "").strip().lower()
        if env in ("0", "off", "false", "no"):
            return False
        if pref is None:
            return semantic_available(self.lang)
        return bool(pref)

    def find(self, text: str, arabic_refs: list[tuple[int, int]] | None = None,
             threshold: float = SPAN_THRESHOLD,
             use_semantic: bool | None = None) -> list[Span]:
        """
        Locate quotations in `text`.

        Parameters
        ----------
        arabic_refs:
            Verse ids already resolved on the Arabic side. When supplied they
            are searched for FIRST and with a lower bar, because the Arabic
            side has already established that the document quotes that verse —
            the only open question is where the translation puts it. This is
            the main reason to run the Arabic detector first.
        use_semantic:
            None = the detector's default (tier c whenever it is available).
            True/False force it. If tier (c) is wanted but cannot run, detection
            falls back to the deterministic tiers and `last_semantic` records
            why — it never fails the request.
        """
        words = text.split()
        if len(words) < 3:
            self.last_semantic = {"used": False, "reason": "text too short"}
            return []

        cites = citation_word_ranges(text)
        blocked = [(ws, we) for _, _, ws, we in cites]

        # Phase 1: the deterministic tiers, exactly as on the plain-python path.
        found: list[Span] = []
        found += self._from_references(text, words, cites)
        if arabic_refs:
            found += self._from_known_refs(words, arabic_refs, blocked)
        window_cands: dict[tuple[int, int], list[tuple[int, int, float]]] = {}
        accepted: list[tuple[int, int]] = []
        found += self._from_windows(words, threshold, None, blocked,
                                    cand_cache=window_cands, accepted=accepted)

        # Phase 2: tier (c), on what phase 1 left unexplained.
        if not self.semantic_enabled(use_semantic):
            self.last_semantic = {"used": False, "reason": "disabled or unavailable"}
            return self._resolve_overlaps(found, threshold)
        try:
            proposals, plan = self._semantic_plan(text, words, found, threshold)
        except Exception as exc:                      # noqa: BLE001
            # SemanticUnavailable, or any model/runtime failure: degrade to
            # the deterministic result rather than failing the check.
            self.last_semantic = {"used": False,
                                  "reason": f"{type(exc).__name__}: {exc}"}
            return self._resolve_overlaps(found, threshold)
        self.last_semantic = {"used": True, **plan}
        if proposals:
            # Phase 3: lexical windows re-seeded with the model's proposals
            # (only windows where a proposal adds a candidate), then spans
            # reported on the model's confidence alone.
            found += self._from_windows(words, threshold, proposals, blocked,
                                        cand_cache=window_cands, accepted=accepted,
                                        only_augmented=True)
            found += self._from_semantic(words, proposals, found, threshold, blocked)
        return self._resolve_overlaps(found, threshold)

    def _snapped(self, words: list[str], rs: int, re_: int, sc: float,
                 book: int | None, surah: int, ayah: int,
                 blocked, lo: int | None = None,
                 hi: int | None = None) -> tuple[int, int, float, int | None]:
        """
        snap_to_quotes, rescored exactly; kept only if whole-verse similarity
        falls by at most QUOTE_SNAP_TOLERANCE (see there).
        """
        if lo is None or hi is None:
            lo, hi = 0, len(words)
            if blocked:
                _, _, lo, hi = _free_region(rs, re_, blocked, len(words))
        ns, ne = snap_to_quotes(words, rs, re_, lo, hi)
        if (ns, ne) == (rs, re_):
            return rs, re_, sc, book
        nbook, _, nsc = self.best_rendering(surah, ayah, " ".join(words[ns:ne]))
        if nsc < sc - QUOTE_SNAP_TOLERANCE:
            return rs, re_, sc, book
        return ns, ne, nsc, nbook

    def _from_references(self, text: str, words: list[str],
                         cites: list[tuple[int, int, int, int]] | None = None) -> list[Span]:
        """
        Tier (a): for each printed reference, find the quotation it labels.

        A quotation sits immediately BEFORE its citation (the usual style) or
        immediately AFTER it ("Allah says (2:255): ..."). Each side is searched
        separately, and each search is confined between this citation and the
        neighbouring ones: the citation is a wall the span may not cross. The
        better-supported side wins; the earlier side wins a tie.
        """
        if cites is None:
            cites = citation_word_ranges(text)
        out = []
        for surah, ayah, ws, we in cites:
            if (surah, ayah) not in self.by_ref:
                continue
            reach = self._cited_reach(surah, ayah)
            prev_end = max([e for _, _, s0, e in cites if e <= ws], default=0)
            next_start = min([s0 for _, _, s0, e in cites if s0 >= we], default=len(words))
            before = after = None
            lo, hi = max(prev_end, ws - reach), ws
            if hi - lo >= 3:
                before = self._place_cited(words, lo, hi, surah, ayah, "reference")
            lo, hi = we, min(next_start, we + reach)
            if hi - lo >= 3:
                after = self._place_cited(words, lo, hi, surah, ayah, "reference")
            # The text before a citation is the default reading; the text after
            # it must be clearly better to take over, because the sentence
            # after a citation is usually the author resuming their own prose.
            best = before
            if after is not None and (before is None or
                                      after.score > before.score + CITED_AFTER_MARGIN):
                best = after
            if best is not None:
                out.append(best)
        return out

    def _cited_reach(self, surah: int, ayah: int) -> int:
        """How far from a citation its quotation may extend: 1.5x the median
        plain rendering of that verse, plus slack. The median, not the max,
        because tafsir editions store commentary under the verse."""
        lens = sorted(self._ix.norm_len[i] for i in self._ix.ref_docs[(surah, ayah)])
        return min(MAX_WINDOW, int(1.5 * lens[len(lens) // 2]) + 4)

    def _place_cited(self, words: list[str], lo: int, hi: int, surah: int,
                     ayah: int, tier: str) -> Span | None:
        """
        Best placement of a KNOWN verse inside [lo, hi), by two routes.

        1. Whole-verse similarity, hill-climbed inside the region — right for a
           complete quotation.
        2. The run of words the verse's renderings CONTAIN (max-sum segment,
           +1 per contained token, -2 per foreign one) — right for a partial
           quotation, which whole-verse similarity scores low by construction
           ("...the Alive, the Eternal" is a tenth of 2:255).

        The span's score is max(similarity, coverage) when coverage clears
        PARTIAL_THRESHOLD, else similarity: the publisher named the verse, so a
        faithful excerpt of it is strong evidence even though it is short.
        """
        routes = []
        rs, re_, sc, book = self.refine_span(words, lo, hi, surah, ayah, lo=lo, hi=hi)
        if re_ - rs >= 3:
            rs, re_, _, _ = self._snapped(words, rs, re_, sc, book, surah, ayah,
                                          (), lo, hi)
            routes.append((rs, re_))
        seg = self._contained_segment(words, lo, hi, surah, ayah)
        if seg is not None:
            _, _, ssc = self.best_rendering(surah, ayah, " ".join(words[seg[0]:seg[1]]))
            a, b, _, _ = self._snapped(words, seg[0], seg[1], ssc, None, surah, ayah,
                                       (), lo, hi)
            if (a, b) not in routes:
                routes.append((a, b))
        cands: list[Span] = []
        for a, b in routes:
            text = " ".join(words[a:b])
            book, _, sc = self.best_rendering(surah, ayah, text)
            cov, cov_book = self.coverage(text, surah, ayah)
            eff = max(sc, cov) if cov >= PARTIAL_THRESHOLD else sc
            cands.append(Span(surah, ayah, a, b, text, eff, tier, book,
                              coverage=cov, coverage_book=cov_book, similarity=sc))
        if not cands:
            return None
        # A route that matches a WHOLE approved rendering well is direct
        # evidence and is preferred; coverage only decides when no route does.
        # (Coverage alone would prefer any short sub-segment whose every word
        # happens to occur in the verse — measured: it turned 7 correct real
        # MATCH/NEAR quotations into UNATTRIBUTED sub-spans.)
        whole = [c for c in cands if c.whole_similarity >= SPAN_THRESHOLD]
        if whole:
            return max(whole, key=lambda c: c.whole_similarity)
        return max(cands, key=lambda c: c.score)

    def _plain_renderings(self, surah: int, ayah: int) -> list[tuple[int, frozenset]]:
        """
        (doc index, token set) of the renderings of a verse fit for CONTAINMENT.

        Containment asks "are these words in the verse?", so commentary must not
        count as verse. Footnotes after the "____" separator are cut, and
        renderings longer than PLAIN_LENGTH_FACTOR x the verse's median length
        are left out: those are tafsir editions whose commentary would make any
        nearby prose look "contained" (27824 renders 3:59 in 71 words and
        includes "without a father", which is the author's next sentence in a
        real sample, not the verse).
        """
        key = (surah, ayah)
        cache = self._ix.plain
        hit = cache.get(key)
        if hit is not None:
            return hit
        rows = []
        for i in self._ix.ref_docs.get(key, ()):
            body = _FOOTNOTE_SEP.split(self.docs[i][3], maxsplit=1)[0]
            rows.append((i, frozenset(normalize(body).split())))
        lens = sorted(len(t) for _, t in rows) or [0]
        cap = PLAIN_LENGTH_FACTOR * lens[len(lens) // 2]
        out = [(i, t) for i, t in rows if t and len(t) <= cap]
        cache[key] = out
        return out

    def coverage(self, text: str, surah: int, ayah: int) -> tuple[float, int | None]:
        """Best `containment` of `text` in a plain approved rendering of the verse."""
        a = normalize(text).split()
        if not a:
            return 0.0, None
        best, book = 0.0, None
        for i, toks in self._plain_renderings(surah, ayah):
            c = sum(1 for w in a if w in toks) / len(a)
            if c > best:
                best, book = c, self.docs[i][0]
        return best, book

    def _contained_segment(self, words: list[str], lo: int, hi: int,
                           surah: int, ayah: int) -> tuple[int, int] | None:
        """Longest-supported run of words in [lo, hi) contained in the verse."""
        wtoks = [normalize(words[w]).split() for w in range(lo, hi)]
        best = None
        for i, toks in self._plain_renderings(surah, ayah):
            run, run_start, run_hits = 0, lo, 0
            for k, tk in enumerate(wtoks):
                hits = sum(1 for t in tk if t in toks)
                val = hits - 2 * (len(tk) - hits)
                if run <= 0 and val > 0:
                    run, run_start, run_hits = 0, lo + k, 0
                run += val
                run_hits += hits
                if (run > 0 and run_hits >= CONTAINED_MIN_TOKENS
                        and (best is None or run > best[0])):
                    best = (run, run_start, lo + k + 1)
                if run <= 0:
                    run, run_hits = 0, 0
        return None if best is None else (best[1], best[2])

    def _from_known_refs(self, words: list[str], refs: list[tuple[int, int]],
                         blocked: list[tuple[int, int]] | tuple = ()) -> list[Span]:
        """
        Place verses the Arabic side already resolved. Tier label: "arabic".

        Scans every window for the best starting point for THIS verse, then
        refines. Cheap, because the verse is known — no retrieval needed.
        """
        out = []
        for surah, ayah in refs:
            if (surah, ayah) not in self.by_ref:
                continue
            doc_i, _ = self._best_doc(surah, ayah, " ".join(words))
            if doc_i is None:
                continue
            size = max(4, self._ix.norm_len[doc_i])
            best = None
            for start in range(0, max(1, len(words) - 3)):
                end = min(len(words), start + size)
                na = normalize(" ".join(words[start:end]))
                s = self._sim_doc(na, frozenset(na.split()), doc_i)
                if best is None or s > best[0]:
                    best = (s, start, end)
            if best is None:
                continue
            rs, re_, sc, book = self.refine_span(words, best[1], best[2], surah, ayah,
                                                 blocked=blocked)
            rs, re_, sc, book = self._snapped(words, rs, re_, sc, book, surah, ayah, blocked)
            text = " ".join(words[rs:re_])
            cov, cov_book = self.coverage(text, surah, ayah)
            out.append(Span(surah, ayah, rs, re_, text, sc, "arabic", book,
                            coverage=cov, coverage_book=cov_book))
        return out

    def _from_windows(self, words: list[str], threshold: float,
                      proposals: list[tuple[int, int, list[tuple[int, int, float]]]] | None,
                      blocked: list[tuple[int, int]] | tuple = (), *,
                      cand_cache: dict | None = None,
                      accepted: list[tuple[int, int]] | None = None,
                      only_augmented: bool = False) -> list[Span]:
        """
        Tiers (b)/(c): slide windows, retrieve candidates, refine, then judge.

        Seeding uses SEED_THRESHOLD (permissive) and the verdict uses
        `threshold` AFTER refinement — judging a clipped window is the error
        this ordering exists to avoid.

        `cand_cache` keeps each window's lexical candidates so the augmented
        pass (`only_augmented=True`) does not retrieve them again; that pass
        visits only windows to which a tier-(c) proposal adds a NEW candidate,
        because every other window would reproduce its phase-1 result.
        `accepted` is shared between the passes (see SKIP_INSIDE_ACCEPTED).
        """
        out = []
        if accepted is None:
            accepted = []
        # Tier (c) augmentation: which semantic candidates each word may offer.
        sem_at: list[list[tuple[int, int, float]]] | None = None
        if proposals:
            sem_at = [[] for _ in words]
            for a, b, cands in proposals:
                keep = [c for c in cands[:SEM_AUG_PER_CHUNK] if c[2] >= SEM_AUG_MIN]
                for w in range(a, b):
                    sem_at[w].extend(keep)

        if only_augmented and sem_at is None:
            return out
        refined: dict[tuple[int, int, int, int], tuple[int, int, float, int | None]] = {}
        for size in self.window_sizes:
            if size > len(words):
                continue
            # Stride grows with the window. A quarter-window stride on the
            # small sizes gives dense coverage where quotations are short; on
            # the large sizes (only reached for footnoted editions) a half-
            # window stride is plenty, because refinement can move a span far
            # from its seed.
            step = max(1, size // WINDOW_STRIDE_DIV if size <= 50 else size // 2)
            for start in range(0, len(words) - size + 1, step):
                if only_augmented and not sem_at[start + size // 2]:
                    continue
                if SKIP_INSIDE_ACCEPTED and any(a <= start and start + size <= b
                                                for a, b in accepted):
                    continue
                window = " ".join(words[start:start + size])
                na = normalize(window)
                ta = frozenset(na.split())
                cached = cand_cache.get((size, start)) if cand_cache is not None else None
                if cached is None:
                    lex = self.lexical_candidates(window, top_k=SEED_CANDIDATES)
                    if SEED_REL_MIN > 0.0:
                        lex = [c for c in lex if c[2] >= SEED_REL_MIN]
                    cached = (lex, self._seed_score(na, ta, len(na.split()), lex))
                    if cand_cache is not None:
                        cand_cache[(size, start)] = cached
                lex, lex_best = cached
                sem_score: dict[tuple[int, int], float] = {}
                if sem_at is None:
                    best = lex_best
                else:
                    # Candidates proposed for the sentence at the window's
                    # centre — the window is mostly inside that sentence.
                    have = {(s, a) for s, a, _ in lex}
                    new = []
                    for s, a, sc in sem_at[start + size // 2]:
                        sem_score[(s, a)] = max(sc, sem_score.get((s, a), 0.0))
                        if (s, a) not in have:
                            new.append((s, a, sc))
                            have.add((s, a))
                    if only_augmented and not new:
                        continue
                    # Same argmax as scoring lexical + new together (lexical
                    # candidates come first, so they win exact ties): a new
                    # candidate takes over only if it beats phase 1's best.
                    best = lex_best
                    if new:
                        nb = self._seed_score(na, ta, len(na.split()), new,
                                              floor=None if lex_best is None else lex_best[0])
                        if nb is not None:
                            best = nb
                    if only_augmented and best is lex_best:
                        continue          # phase 1 already reported this window
                if best is None:
                    continue

                key = (best[1], best[2], start, start + size)
                if key not in refined:
                    refined[key] = self.refine_span(words, start, start + size,
                                                    best[1], best[2], blocked=blocked)
                rs, re_, sc, book = refined[key]
                rs, re_, sc, book = self._snapped(words, rs, re_, sc, book,
                                                  best[1], best[2], blocked)
                span_text = " ".join(words[rs:re_])
                if sc >= threshold and self.short_span_ok(span_text, sc, best[1], best[2]):
                    accepted.append((rs, re_))
                    # A window whose winning verse came from the model's
                    # proposals was found WITH the model: label it so, or the
                    # AI disclosure would be missing (red-team, 2026-10-04).
                    seeded = sem_at is not None and best is not lex_best
                    out.append(Span(best[1], best[2], rs, re_, span_text, sc,
                                    TIER_LEXICAL_SEEDED if seeded else "lexical", book,
                                    semantic_score=sem_score.get((best[1], best[2]))))
        return out

    # ------------------------------------------------------------ tier (c)

    def _semantic_proposals(self, text: str) -> list[tuple[int, int, list[tuple[int, int, float]]]]:
        """
        Embed EVERY sentence chunk (the pre-2026-10-04 plan); kept for
        measurement and for callers that want the full proposal list.

        Raises semantic.SemanticUnavailable when the tier cannot run.
        """
        from . import semantic
        chunks = semantic_chunks(text)
        if not chunks:
            return []
        words = text.split()
        texts = [" ".join(words[a:b]) for a, b in chunks]
        cands = semantic.candidates_batch(texts, self.lang, top_k=SEMANTIC_TOP_K)
        return [(a, b, c) for (a, b), c in zip(chunks, cands)]

    def scripture_likeness(self, text: str) -> float:
        """
        Cheap, model-free: how much of `text` reads like ONE verse's wording.

        The share of the text's IDF-weighted vocabulary found in a single
        approved rendering of one of its SEM_GATE_CANDIDATES lexical candidate
        verses (max over verse and edition). IDF over every token, so function
        words weigh little and a name or rare noun weighs a lot. Used only to
        decide what tier (c) embeds; it never reports anything.
        """
        toks = set(normalize(text).split())
        if not toks:
            return 0.0
        idf = self.idf
        den = sum(idf(t) for t in toks)
        if den <= 0:
            return 0.0
        best = 0.0
        for s, a, _ in self.lexical_candidates(text, top_k=SEM_GATE_CANDIDATES):
            for i in self._ix.ref_docs.get((s, a), ()):
                shared = toks & self._toks(i)
                if not shared:
                    continue
                v = sum(idf(t) for t in shared) / den
                if v > best:
                    best = v
        return best

    def _semantic_plan(self, text: str, words: list[str], found: list[Span],
                       threshold: float) -> tuple[list, dict]:
        """
        Decide what tier (c) embeds, embed it, return (proposals, log).

        See the SEM_SELECT block of constants for the rules and the
        measurement behind them. The log goes to `last_semantic`: how many
        sentences were explained, below the gate, embedded, and exactly which
        chunks the token budget dropped.
        """
        from . import semantic
        if SEM_SELECT == "all":
            props = self._semantic_proposals(text)
            return props, {"plan": "all", "embedded": len(props),
                           "chunks_considered": len(props), "dropped": []}

        claimed = [(sp.start_word, sp.end_word) for sp in found if sp.score >= threshold]

        def explained(a: int, b: int) -> bool:
            return any(min(b, e) - max(a, s) >= max(1, (b - a) // 2) for s, e in claimed)

        units = sentence_units(text)
        above: list[tuple[float, int, int]] = []           # (likeness, a, b)
        below: list[tuple[float, int, int]] = []
        n_explained = 0
        for a, b in units:
            pieces = ([(a, b)] if b - a <= SEM_CHUNK_MAX_WORDS else
                      [(x, min(b, x + SEM_CHUNK_SLICE))
                       for x in range(a, b, SEM_CHUNK_SLICE // 2)
                       if x == a or x + SEM_CHUNK_SLICE // 2 < b])
            for x, y in pieces:
                if explained(x, y):
                    n_explained += 1
                    continue
                like = self.scripture_likeness(" ".join(words[x:y]))
                (above if like >= SEM_GATE_MIN else below).append((like, x, y))
        pairs = [(a, d) for (a, b), (c, d) in zip(units, units[1:])
                 if d - a <= SEM_PAIR_MAX_WORDS and not explained(a, d)]

        budget = token_budget()
        dropped: list[dict] = []
        props: dict[tuple[int, int], list] = {}
        rounds: list[dict] = []

        def embed(chunks: list[tuple[float, int, int]], stage: str) -> None:
            nonlocal budget
            chunks = [c for c in chunks if (c[1], c[2]) not in props]
            if not chunks:
                return
            chunks = sorted(chunks, key=lambda c: (-c[0], c[1]))
            texts = [" ".join(words[x:y]) for _, x, y in chunks]
            sizes = semantic.count_tokens(texts)
            take = []
            for (prio, x, y), t, n in zip(chunks, texts, sizes):
                if n <= budget:
                    budget -= n
                    take.append((x, y, t, n))
                else:
                    dropped.append({"start_word": x, "end_word": y, "stage": stage,
                                    "priority": round(prio, 3), "tokens": n})
            if take:
                res = semantic.candidates_batch([t for _, _, t, _ in take], self.lang,
                                                top_k=SEMANTIC_TOP_K)
                for (x, y, _, _), c in zip(take, res):
                    props[(x, y)] = c
            rounds.append({"stage": stage, "embedded": len(take),
                           "tokens": sum(n for *_, n in take)})

        def member_score(a: int, d: int) -> float:
            return max((c[0][2] for (x, y), c in props.items()
                        if c and a <= x and y <= d), default=0.0)

        # 1. verse-like sentences; 2. pairs the model already placed confidently;
        # 3. every other sentence; 4. every other pair. A document that fits
        # the budget gets all four rounds — exactly the full semantic pass.
        embed(above, "verse-like sentence")
        embed([(member_score(a, d), a, d) for a, d in pairs
               if member_score(a, d) >= SEM_PAIR_GATE], "confident pair")
        embed(below, "other sentence")
        embed([(member_score(a, d), a, d) for a, d in pairs], "other pair")

        if dropped:
            log.info("tier (c) token budget %d reached: %d chunk(s) not embedded: %s",
                     token_budget(), len(dropped),
                     ", ".join(f"words {d['start_word']}-{d['end_word']} ({d['stage']})"
                               for d in dropped))
        plan = {
            "plan": "budgeted", "sentences": len(units),
            "explained_by_cheaper_tiers": n_explained,
            "verse_like_sentences": len(above), "other_sentences": len(below),
            "pairs": len(pairs), "embedded": len(props),
            "tokens_embedded": token_budget() - budget,
            "token_budget": token_budget(), "complete": not dropped,
            "rounds": rounds, "dropped": dropped,
        }
        return [(x, y, c) for (x, y), c in sorted(props.items())], plan

    def _from_semantic(self, words: list[str],
                       proposals: list[tuple[int, int, list[tuple[int, int, float]]]],
                       found: list[Span], threshold: float,
                       blocked: list[tuple[int, int]] | tuple = ()) -> list[Span]:
        """
        Tier (c) on its own: report a sentence whose meaning places it on a
        verse confidently, even though its wording matched no approved edition.

        The span is the proposed sentence, tightened by sim() refinement that
        is not allowed to leave it. The span's score is the ordinary textual
        similarity to the closest approved rendering — honest, and usually low
        for exactly the passages this tier exists to catch; the model's
        confidence is carried separately in `semantic_score`.
        """
        out = []
        claimed = [(sp.start_word, sp.end_word) for sp in found
                   if sp.score >= threshold]
        for a, b, cands in proposals:
            if b - a < SEM_SPAN_MIN_WORDS or not cands:
                continue
            surah, ayah, conf = cands[0]
            margin = conf - (cands[1][2] if len(cands) > 1 else 0.0)
            if not semantic_span_confident(conf, margin):
                continue
            # Already explained by a lexical/reference span: nothing to add.
            if any(not (b <= s or a >= e) and min(b, e) - max(a, s) >= (b - a) // 2
                   for s, e in claimed):
                continue
            if blocked:
                a, b, _, _ = _free_region(a, b, blocked, len(words))
                if b - a < SEM_SPAN_MIN_WORDS:
                    continue
            rs, re_, sc, book = self.refine_span(words, a, b, surah, ayah, lo=a, hi=b)
            if re_ - rs < SEM_SPAN_MIN_WORDS:
                rs, re_ = a, b
                book, _, sc = self.best_rendering(surah, ayah, " ".join(words[a:b]))
            if sc < SEM_SPAN_MIN_SIM:
                continue
            out.append(Span(surah, ayah, rs, re_, " ".join(words[rs:re_]), sc,
                            "semantic", book, semantic_score=conf))
        return out

    @staticmethod
    def _resolve_overlaps(spans: list[Span], threshold: float) -> list[Span]:
        """
        Greedy: strongest span first, drop anything overlapping it.

        CONFIRMED verses go first regardless of score: first one named by a
        printed citation whose span clears the threshold (by similarity, or by
        coverage for a partial quotation), then one resolved from the Arabic
        and placed above the threshold. Two verses can share their opening words — "Allah! There is
        no deity save Him, the Alive, the Eternal" is the whole of 3:2 and the
        start of 2:255 — and when the publisher printed 2:255, their stated id
        is better evidence than which of two near-identical strings scores a
        hair higher.

        Semantic-tier spans passed their own gate in _from_semantic and are not
        held to `threshold` (their textual similarity is low by definition);
        they rank by that similarity, so any overlapping lexical or reference
        span — which has direct textual evidence — wins the slot.
        """
        tier_rank = {"reference": 2, "arabic": 1, "semantic": 0, "lexical": 0,
                     TIER_LEXICAL_SEEDED: 0}
        cited = {(s.surah, s.ayah) for s in spans
                 if s.tier == "reference" and s.score >= threshold}
        from_arabic = {(s.surah, s.ayah) for s in spans
                       if s.tier == "arabic" and s.score >= threshold}

        def precedence(s: Span) -> int:
            v = (s.surah, s.ayah)
            return 0 if v in cited else 1 if v in from_arabic else 2

        # The publisher's printed id first, then a verse the Arabic source
        # established, then everything else; within each, the span of that
        # verse with the strongest whole-verse similarity, whichever tier found
        # it. So if the lexical tier placed the cited verse with better
        # boundaries than tier (a) did, that placement is the one reported.
        ordered = sorted(spans, key=lambda s: (precedence(s), -s.whole_similarity,
                                               -s.score, -tier_rank[s.tier],
                                               s.start_word, s.surah, s.ayah))
        chosen: list[Span] = []
        for s in ordered:
            if s.score < threshold and s.tier != "semantic":
                continue
            if any(not (s.end_word <= c.start_word or s.start_word >= c.end_word)
                   for c in chosen):
                continue
            # The same verse twice is two quotations to check, not one: a
            # verbatim copy followed by an altered copy must not hide the
            # altered one (red-team, 2026-10-04). Only a fragment touching an
            # already-chosen span of the same verse is the same quotation.
            if any(c.surah == s.surah and c.ayah == s.ayah
                   and (abs(s.start_word - c.end_word) <= SAME_VERSE_GAP
                        or abs(c.start_word - s.end_word) <= SAME_VERSE_GAP)
                   for c in chosen):
                continue
            chosen.append(s)
        return sorted(chosen, key=lambda s: s.start_word)
