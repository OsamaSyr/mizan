#!/usr/bin/env python3
"""
MIZAN — document-level verdict.

Step (6): fold per-quotation attributions into one answer a publisher can act
on, with every finding traceable back to a named approved translation.

The governing rule, from the spec: ONE unattributed item pulls the whole
document to REFER. A gate whose result can be diluted is not a gate.

Vocabulary discipline
---------------------
Nothing here ever says "wrong", "error" or "incorrect". The system compares a
published rendering against an index of approved translations; a non-match
means OUR INDEX does not contain it, which is a statement about the index as
much as about the text. The prototype proved why: passages we assumed were
re-translated turned out to be Pickthall, which was simply missing from the
index at the time. A narrow index accuses the innocent.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

from .engine import Mizan, normalize, strict_tokens, verbatim

# The five output states. MATCH/NEAR/UNATTRIBUTED come from engine.attribute;
# the other two are decided here, where document context is available.
MATCH = "MATCH"
NEAR = "NEAR"
UNATTRIBUTED = "UNATTRIBUTED"
NO_APPROVED_TRANSLATION = "NO_APPROVED_TRANSLATION"
UNRESOLVED = "UNRESOLVED"

# Document verdicts.
VERDICT_CLEAR = "CLEAR"    # every finding attributed to an approved translation
VERDICT_REFER = "REFER"    # at least one finding a human must look at

# States that do NOT force a referral.
#
# NO_APPROVED_TRANSLATION is deliberately included. It means the Arabic
# resolved fine but we index no approved translation for that language — a gap
# in OUR coverage, not a defect in the document. The spec is explicit that
# without this carve-out every quotation in an uncovered language would drag
# its whole document to referral ("تُسحب كل وثيقة إلى الإحالة"), which would
# make the tool useless exactly where coverage is thinnest. It is surfaced as
# advice ("use the Arabic with a translator's note"), and still shows up in
# `counts` so a UI can display it.
#
# UNATTRIBUTED and UNRESOLVED do force a referral: there we have approved text
# to compare against and the comparison did not land.
#
# NEAR forces a referral too (since 2026-10-04). NEAR means "resembles an
# approved rendering, but the words differ" — and one differing word can be a
# dropped "not" or a changed number. The scientific package's own test case
# for a misquoted verse requires «عدم البناء على النص المحرف», so a document
# whose quotation differs from every approved wording does not pass the gate;
# a human decides. The NEAR state is kept so the reviewer sees WHICH approved
# translation it is closest to and exactly which words differ.
_CLEARING_STATES = {MATCH, NO_APPROVED_TRANSLATION}

# Human-facing wording for each state. Descriptive, never a judgement.
STATE_MESSAGE = {
    MATCH: "Matches an approved published translation.",
    NEAR: ("Close to an approved translation, but the words differ — "
           "referred for review."),
    UNATTRIBUTED: "Matches no translation in our index — referred for review.",
    NO_APPROVED_TRANSLATION: (
        "Resolved in Arabic, but no approved translation is indexed for this "
        "language. Consider using the Arabic with a translator's note."
    ),
    UNRESOLVED: "Could not be resolved to a verse.",
}

_OPEN_Q = re.compile(r'^[\[(]*(?:["“«„]|’’)')         # Urdu: ’’…‘‘
_CLOSE_Q = re.compile(r'(?:["”»]|‘‘)[.,;:!?)\]۔،]*$')

# Document-context checks that refer a finding whatever its state (added
# 2026-10-04 after red-team review). Each is a statement about what the author
# printed around the quotation, never about the verse.
FLAG_EXTRA_WORDS = "extra_words_in_quote"
FLAG_CITATION_MISMATCH = "citation_mismatch"
FLAG_CITED_NOT_FOUND = "cited_text_not_found"
FLAG_NO_SUCH_VERSE = "no_such_verse"
FLAG_MESSAGE = {
    FLAG_EXTRA_WORDS: ("The quotation marks also enclose words that are not "
                       "part of the matched verse — referred for review."),
    FLAG_CITATION_MISMATCH: ("The printed reference names a different verse from "
                             "the one whose text was found — referred for review."),
    FLAG_CITED_NOT_FOUND: ("Quoted text labelled with a verse reference, but it "
                           "resembles no approved translation of that verse — "
                           "referred for review."),
    FLAG_NO_SUCH_VERSE: ("The printed reference names a verse that does not "
                         "exist — referred for review."),
}
# Words inside the author's quotation marks, not covered by any located verse
# and not part of a citation, from which the quotation is referred. Below it:
# "Say:", "Allah says", an ellipsis word — the author's framing.
EXTRA_WORDS_MIN = 3


@dataclass
class DiffOp:
    """One word-level edit between the published text and the approved one."""

    op: str                    # "equal" | "insert" | "delete" | "replace"
    published: list[str] = field(default_factory=list)
    approved: list[str] = field(default_factory=list)


@dataclass
class Finding:
    """
    One quotation, and what we can say about where it came from.

    Carries everything a UI or API needs to render the row without a second
    lookup — including both texts, so a reviewer can see the comparison that
    produced the verdict rather than trusting a number.
    """

    ref: str
    surah: int
    ayah: int
    state: str
    lang: str
    score: float
    published_text: str
    approved_text: str | None = None
    attributed_to: int | None = None          # book_id
    attributed_title: str | None = None
    arabic_text: str | None = None            # the Uthmani verse, when known
    start_word: int | None = None
    end_word: int | None = None
    tier: str | None = None                   # which detection tier found it
    n_compared: int = 0
    runners_up: list = field(default_factory=list)
    diff: list[DiffOp] = field(default_factory=list)
    n_edits: int = 0
    message: str = ""
    # Partial quotation: the publisher quoted one clause of the verse. The
    # verdict was then decided against the matching clause of the approved
    # text (`approved_excerpt`), not the whole verse. `approved_text` still
    # carries the full verse so a reviewer sees the context.
    partial: bool = False
    approved_excerpt: str | None = None
    # Document-context checks (FLAG_*). Any flag refers the finding.
    flags: list[str] = field(default_factory=list)
    flag_detail: dict = field(default_factory=dict)

    @property
    def needs_referral(self) -> bool:
        return self.state not in _CLEARING_STATES or bool(self.flags)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["diff"] = [asdict(x) for x in self.diff]
        return d


@dataclass
class DocumentReport:
    """
    The whole answer for one document. Render this directly.

    `verdict` is the gate; `findings` is the evidence; `counts` lets a UI show
    a summary line without walking the list.
    """

    verdict: str
    lang: str
    findings: list[Finding] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    index_version: str = ""
    index_source: str = "quranpedia.net"
    n_translations_indexed: int = 0
    summary: str = ""

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "lang": self.lang,
            "summary": self.summary,
            "counts": self.counts,
            "index": {
                "source": self.index_source,
                "version": self.index_version,
                "translations": self.n_translations_indexed,
            },
            "findings": [f.as_dict() for f in self.findings],
        }


def _diff_units(text: str) -> tuple[list[str], list[str]]:
    words, norms = [], []
    pending = ""
    for w in text.split():
        n = " ".join(strict_tokens(w))
        if not n:
            if words:
                words[-1] += " " + w
            else:
                pending = (pending + " " + w).strip()
            continue
        words.append((pending + " " + w).strip() if pending else w)
        norms.append(n)
        pending = ""
    if pending and words:
        words[-1] += " " + pending
    return words, norms


def word_diff(published: str, approved: str) -> tuple[list[DiffOp], int]:
    """
    Word-level diff, computed on NORMALISED tokens but reported with the
    original words.

    Normalised for alignment so that punctuation and casing do not show up as
    edits, original for display so the reviewer sees what is actually printed.
    Returns (ops, n_changed_regions).
    """
    # Aligned on the verbatim-check tokens, so every difference that stops a
    # MATCH (a parenthesised word, a question mark) is also visible here — and
    # nothing that does not stop one: a word with no tokens of its own (a
    # verse number "(56)", a dash) rides along with its neighbour instead of
    # showing up as an edit.
    pub_words, pub_norm = _diff_units(published)
    app_words, app_norm = _diff_units(approved or "")

    ops: list[DiffOp] = []
    edits = 0
    matcher = SequenceMatcher(None, pub_norm, app_norm, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        ops.append(DiffOp(op=tag,
                          published=pub_words[i1:i2],
                          approved=app_words[j1:j2]))
        if tag != "equal":
            edits += 1
    return ops, edits


# Partial-quotation upgrade rule, calibrated 2026-10-03 on clauses cut from
# approved editions (should be recognised) versus clauses cut from six
# published translations that are NOT in the index (Asad, Arberry, Daryabadi,
# Itani, Wahiduddin, Ahmed Ali — should stay referred):
#
#   rule                                   approved kept   non-approved upgraded
#   window score >= 0.75 (NEAR allowed)        98.0%             52.0%   <- rejected
#   score >= 0.75 and containment >= 0.85      96.8%             32.3%   <- rejected
#   MATCH only (score >= 0.92), 6-15 words     91.3%             14.5%
#   MATCH + containment >= 0.95, 8-20 words    88.1%              6.1%   <- chosen
#
# At clause level, "an approved translator's wording with a small edit" cannot
# be told apart from "a different translator who phrases it similarly" —
# translators borrow heavily from each other. So a clause may only be upgraded
# to MATCH, never to NEAR: near-verbatim identity is evidence, resemblance is
# not. The residual 6% are clauses ≥95% word-identical to an approved
# rendering, i.e. the published words really are that approved wording.
PARTIAL_MIN_WORDS = 8
PARTIAL_MIN_CONTAINMENT = 0.95
# Below this share of the verse, the span is treated as a partial quotation and
# compared clause-to-clause. At or above it, whole-verse comparison is fair.
PARTIAL_MAX_SHARE = 0.85

def _containment(published: str, excerpt: str) -> float:
    """Share of the published words that occur in the approved clause."""
    pub = normalize(published).split()
    app = set(normalize(excerpt).split())
    return sum(w in app for w in pub) / len(pub) if pub else 0.0


def best_excerpt(published_text: str, approved: str) -> tuple[float, str] | None:
    """
    Find the clause of `approved` that best matches `published_text`.

    Why this exists: the index stores whole verses, but writers quote clauses.
    Comparing "And We made the sky a ceiling" against all of 2:22 scores low
    even when every word is the approved translator's, which turns a faithful
    excerpt into a referral. Measured on real articles, this was the main reason
    correct publisher-cited quotations ended UNATTRIBUTED.

    Method: anchor on the longest common run of normalised words, then score a
    small neighbourhood of windows around it with the engine's own sim(). The
    window is cut from the approved text verbatim — nothing is generated — and
    the score uses the frozen similarity, so the MATCH/NEAR thresholds keep
    their measured meaning.

    Returns (score, excerpt) or None when the comparison does not apply.
    """
    from .engine import sim

    pub_norm = normalize(published_text).split()
    n = len(pub_norm)
    if n < PARTIAL_MIN_WORDS:
        return None
    app_words = (approved or "").split()
    app_norm = [normalize(w) for w in app_words]
    n_app = sum(1 for w in app_norm if w)
    if not n_app or n >= PARTIAL_MAX_SHARE * n_app:
        return None

    m = SequenceMatcher(None, pub_norm, app_norm, autojunk=False)
    a, b, size = m.find_longest_match(0, n, 0, len(app_norm))
    if size == 0:
        return None
    # Where the published span would start in the approved text if the
    # longest common run sits at the same offset in both.
    anchor = max(0, b - a)

    best: tuple[float, str] | None = None
    for start in range(max(0, anchor - 3), min(len(app_words), anchor + 4)):
        for length in range(max(1, int(n * 0.8)), int(n * 1.25) + 2):
            end = min(len(app_words), start + length)
            if end - start < PARTIAL_MIN_WORDS:
                continue
            excerpt = " ".join(app_words[start:end])
            s = sim(published_text, excerpt)
            if best is None or s > best[0]:
                best = (s, excerpt)
    return best


def _partial_attribution(mizan: Mizan, published_text: str, surah: int,
                         ayah: int, lang: str, exclude: int | None):
    """Best clause-level match across every approved edition of this verse."""
    best = None   # (score, book_id, excerpt, full_verse)
    for bid in mizan.editions(lang):
        if bid == exclude:
            continue
        full = mizan.verse(bid, surah, ayah)
        if not full:
            continue
        hit = best_excerpt(published_text, full)
        if hit and (best is None or hit[0] > best[0]):
            best = (hit[0], bid, hit[1], full)
    return best


def quoted_core(published_text: str) -> str:
    """The span cut to the author's own quotation marks, when it has them.

    A detected span can carry a prose word across the opening mark
    ('article. "…transgressed against…'). Words outside the marks are the
    author's prose, not part of the claimed quotation, so the verbatim check
    judges what is inside them.
    """
    w = published_text.split()
    if len(w) < 2:
        return published_text
    s, e = 0, len(w)
    for k in range(min(4, len(w))):
        if _OPEN_Q.search(w[k]):
            s = k
            break
    for k in range(len(w) - 1, max(s, len(w) - 5) - 1, -1):
        if _CLOSE_Q.search(w[k]):
            e = k + 1
            break
    return " ".join(w[s:e]) if e > s else published_text


def _verbatim_edition(mizan: Mizan, published_text: str, surah: int, ayah: int,
                      lang: str, prefer: int | None, exclude: int | None):
    """(book_id, full_text, "whole"|"excerpt") of an approved edition whose
    words the published text IS (engine.verbatim), the engine's pick first."""
    order = [prefer] if prefer else []
    order += [b for b in mizan.editions(lang) if b != prefer]
    for bid in order:
        if bid == exclude:
            continue
        full = mizan.verse(bid, surah, ayah)
        if full:
            kind = verbatim(published_text, full)
            if kind:
                return bid, full, kind
    return None


def build_finding(mizan: Mizan, published_text: str, surah: int, ayah: int,
                  lang: str, *, arabic_text: str | None = None,
                  start_word: int | None = None, end_word: int | None = None,
                  tier: str | None = None, exclude: int | None = None) -> Finding:
    """
    Attribute one quotation and package it for display.

    engine.attribute() ranks the approved renderings; it does not decide
    identity. MATCH additionally requires the published words to be an
    approved translation's words (engine.verbatim): a similarity-MATCH whose
    words differ is reported as NEAR — close, with the differing words shown —
    which refers the document. When the publisher quoted only part of the
    verse, a clause-level comparison may upgrade it to MATCH — never to NEAR —
    only when the clause is verbatim approved wording of at least
    PARTIAL_MIN_WORDS words. The finding is then marked `partial` and carries
    the matching clause, so the claim on screen is exactly "this clause is that
    translator's wording".
    """
    result = mizan.attribute(published_text, surah, ayah, lang, exclude=exclude)
    state = result["state"]
    approved = result.get("approved_text")
    score = result.get("score", 0.0)
    attributed_to = result.get("attributed_to")
    title = result.get("title")
    partial = False
    excerpt = None

    core = quoted_core(published_text)
    if state == MATCH:
        hit = _verbatim_edition(mizan, core, surah, ayah, lang,
                                attributed_to, exclude)
        if hit is None:
            state = NEAR
        else:
            bid, full, kind = hit
            if bid != attributed_to:
                attributed_to, title, approved = bid, mizan.books[bid]["title"], full
            if kind == "excerpt":
                partial = True
                shown = best_excerpt(core, full)
                excerpt = shown[1] if shown else None

    if state in (NEAR, UNATTRIBUTED) and not partial:
        from .engine import T_MATCH, T_NEAR
        hit = _partial_attribution(mizan, core, surah, ayah, lang, exclude)
        if hit:
            p_score, p_book, p_excerpt, p_full = hit
            if (p_score >= T_MATCH
                    and _containment(core, p_excerpt) >= PARTIAL_MIN_CONTAINMENT
                    and verbatim(core, p_full)):
                state, score = MATCH, round(p_score, 3)
                attributed_to = p_book
                title = mizan.books[p_book]["title"]
                approved = p_full
                excerpt = p_excerpt
                partial = True
            elif state == UNATTRIBUTED and p_score >= T_NEAR:
                # Not attributed — a clause that merely resembles approved
                # wording is not evidence (calibration above) — but for the
                # reviewer the fair comparison is the closest approved CLAUSE,
                # not a whole verse the author never quoted. Display only.
                approved, excerpt = p_full, p_excerpt

    diff, n_edits = ([], 0)
    compare_to = excerpt or approved
    if compare_to and state in (MATCH, NEAR, UNATTRIBUTED):
        # Show the comparison even when it failed: a reviewer deciding whether
        # our index is simply missing the source needs to see how far off it is.
        diff, n_edits = word_diff(published_text, compare_to)

    message = STATE_MESSAGE[state]
    if partial:
        message = ("Partial quotation: this clause matches an approved "
                   "translation's wording.")

    return Finding(
        ref=f"{surah}:{ayah}", surah=surah, ayah=ayah, state=state, lang=lang,
        score=score,
        published_text=published_text,
        approved_text=approved,
        attributed_to=attributed_to,
        attributed_title=title,
        arabic_text=arabic_text,
        start_word=start_word, end_word=end_word, tier=tier,
        n_compared=result.get("n_compared", 0),
        runners_up=result.get("runners_up", []),
        diff=diff, n_edits=n_edits,
        message=message,
        partial=partial,
        approved_excerpt=excerpt,
    )


def unresolved_finding(published_text: str, lang: str,
                       start_word: int | None = None,
                       end_word: int | None = None) -> Finding:
    """A span we believe is a quotation but could not tie to a verse."""
    return Finding(
        ref="?", surah=0, ayah=0, state=UNRESOLVED, lang=lang, score=0.0,
        published_text=published_text, start_word=start_word, end_word=end_word,
        message=STATE_MESSAGE[UNRESOLVED],
    )


def build_report(mizan: Mizan, findings: list[Finding], lang: str) -> DocumentReport:
    """
    Apply the document rule: one referral-worthy item refers the document.

    A document with NO findings is CLEAR. That is deliberate — Mizan checks
    quotations, not the author's own prose. Prose has no approved text to
    compare against, so flagging it would mark every original sentence
    "unattributed", which is meaningless.
    """
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.state] = counts.get(f.state, 0) + 1

    needs_referral = [f for f in findings if f.needs_referral]
    verdict = VERDICT_REFER if needs_referral else VERDICT_CLEAR

    if not findings:
        summary = "No Quranic quotation detected. Nothing to attribute."
    elif verdict == VERDICT_CLEAR:
        bits = []
        if counts.get(MATCH):
            bits.append(f"{counts[MATCH]} matching an approved translation")
        if counts.get(NO_APPROVED_TRANSLATION):
            bits.append(f"{counts[NO_APPROVED_TRANSLATION]} with no approved "
                        f"translation indexed for this language")
        summary = f"{len(findings)} quotation(s): " + ", ".join(bits) + "."
    else:
        refs = ", ".join(f.ref for f in needs_referral[:5])
        summary = (f"{len(needs_referral)} of {len(findings)} quotation(s) "
                   f"referred for review ({refs}).")

    return DocumentReport(
        verdict=verdict, lang=lang, findings=findings, counts=counts,
        index_version=getattr(mizan, "version", "?"),
        n_translations_indexed=len(getattr(mizan, "books", {})),
        summary=summary,
    )


def _is_alias_of_placed(arabic_detector, quoted: str | None,
                        placed: set[tuple[int, int]]) -> bool:
    """
    Is this Arabic quotation just another reading of a verse we already placed?

    Several verses open with identical words — «اللَّهُ لَا إِلَٰهَ إِلَّا هُوَ
    الْحَيُّ الْقَيُّومُ» begins both 2:255 and 3:2. The Arabic matcher can only
    name one; when the author's own citation and the translation agree on the
    other, reporting "3:2 quoted but not located" would refer a correct
    document over an ambiguity in the source text, not a fault in the
    translation. So: if the quoted words occur inside a verse that was placed,
    the quotation is accounted for.
    """
    if not quoted or arabic_detector is None:
        return False
    from .arabic import fold_arabic
    q = fold_arabic(quoted)
    if len(q.split()) < 3:
        return False
    for s, a in placed:
        verse = arabic_detector.verse(s, a)
        if verse and q in fold_arabic(verse):
            return True
    return False


_RANGE = re.compile(r"(\d{1,3})\s*[:：]\s*(\d{1,3})\s*[-–—]\s*(\d{1,3})")
_HAS_LETTER = re.compile(r"\w")
QUOTE_REGION_MAX_WORDS = 400
# How far back from a reference a quotation's closing mark may sit from the end
# of its located verse before the words in between stop counting as quoted.
TAIL_MAX_WORDS = 60
# How close (in words) a citation must sit to a quotation to be its label.
CITE_ADJACENT_WORDS = 2


def quote_regions(words: list[str], walls: set[int] | None = None) -> list[tuple[int, int]]:
    """Word ranges [start, end) enclosed by the author's double quotation
    marks or guillemets. Unclosed or implausibly long regions are ignored.

    An author who forgets a closing mark must not turn the rest of the
    article into one quotation: a new opening mark restarts the region, and
    a printed reference (`walls`) ends an unclosed one."""
    walls = walls or set()
    out, opened = [], None
    for i, w in enumerate(words):
        if opened is not None and i in walls:
            opened = None
            continue
        if opened is not None and i > opened and _OPEN_Q.search(w) and not _CLOSE_Q.search(w):
            opened = None
        if opened is None:
            if _OPEN_Q.search(w):
                core = _OPEN_Q.sub("", w)
                if core and _CLOSE_Q.search(core):
                    out.append((i, i + 1))
                else:
                    opened = i
        elif _CLOSE_Q.search(w):
            if i + 1 - opened <= QUOTE_REGION_MAX_WORDS:
                out.append((opened, i + 1))
            opened = None
    return out


def _all_citations(text: str, words: list[str]) -> list[tuple[int, int, int, int, int]]:
    """(surah, first_ayah, last_ayah, first_word, end_word) for every printed
    reference, INCLUDING ones naming no real verse (115:1, 2:287) — those are
    exactly the ones a fabricated quotation would carry. Read by the same
    parser as detection (detect.printed_reference_spans), so the two layers
    never disagree about what is a reference."""
    from .detect import _WORD, printed_reference_spans
    starts = [m.start() for m in _WORD.finditer(text)]
    out = []
    for su, a, c0, c1 in printed_reference_spans(text, max_surah=999, max_ayah=999):
        last = a
        r = _RANGE.search(text, c0, min(len(text), c1 + 8))
        if r and int(r.group(1)) == su and int(r.group(2)) == a:
            tail = r.group(3)
            last = int(tail)
            if last < a and len(tail) < len(str(a)):
                # Shorthand range: "2:155-7" is 155 to 157.
                last = int(str(a)[:-len(tail)] + tail)
            last = max(a, last)
        idx = [i for i, st in enumerate(starts) if c0 <= st < c1]
        if idx:
            out.append((su, a, last, idx[0], idx[-1] + 1))
    return sorted(out, key=lambda c: c[3])


def _excerpt(ws: list[str], n: int = 14) -> str:
    return " ".join(ws) if len(ws) <= n else " ".join(ws[:n]) + " …"


def _approved_wording(mizan: Mizan, text: str, surah: int, ayah: int, lang: str) -> bool:
    """Is `text` (the author's whole quotation) itself verbatim approved
    wording of this verse? Then nothing in it is "extra": the located span was
    just narrower than the quotation — e.g. it skipped a translator's
    parenthetical "(All that night)" that sim() cannot see."""
    core = quoted_core(text)
    return any(verbatim(core, mizan.verse(b, surah, ayah) or "")
               for b in mizan.editions(lang))


def _verse_exists(mizan: Mizan, surah: int, ayah: int, lang: str) -> bool:
    eds = mizan.editions(lang) or list(getattr(mizan, "books", {}))
    return any(mizan.verse(b, surah, ayah) for b in eds[:3])


def _misnumbered(mizan: Mizan, f: Finding, lang: str, detector,
                 words: list[str], region: tuple[int, int] | None) -> Finding | None:
    """
    A printed reference gets precedence, so text that is verse X verbatim but
    printed with Y's number is placed at Y and comes out NEAR/UNATTRIBUTED —
    referred, but for the wrong stated reason. If the quoted text alone is an
    approved rendering of another verse, report THAT verse, flagged as a
    citation mismatch.
    """
    if detector is None or f.tier != "reference" or f.state == MATCH:
        return None
    # The whole quotation, not just the span: precedence can leave the cited
    # verse holding a short fragment it shares with the quoted verse.
    whole = " ".join(words[region[0]:region[1]]) if region else f.published_text
    core = quoted_core(whole)
    try:
        spans = detector.find(core, use_semantic=False)
    except Exception:                      # noqa: BLE001 - this is an explanation, not the gate
        return None
    n = len(core.split())
    for sp in spans:
        if (sp.surah, sp.ayah) == (f.surah, f.ayah) or (sp.end_word - sp.start_word) < 0.8 * n:
            continue
        alt = build_finding(mizan, core, sp.surah, sp.ayah, lang,
                            start_word=region[0] if region else f.start_word,
                            end_word=region[1] if region else f.end_word, tier="lexical")
        if alt.state == MATCH:
            alt.published_text = whole
            alt.flags.append(FLAG_CITATION_MISMATCH)
            alt.flag_detail[FLAG_CITATION_MISMATCH] = f.ref
            return alt
    return None


def _context_checks(mizan: Mizan, text: str, lang: str,
                    findings: list[Finding], detector=None) -> list[Finding]:
    """
    What the author printed AROUND each quotation (added 2026-10-04 after a
    red-team review showed all four passing silently):

    1. Words inside the same quotation marks that are not part of any located
       verse ("<verse> and whoever eats meat on a Friday...") -> refer.
    2. A printed reference naming a different verse from the one whose text
       sits next to it (39:53 printed as 39:54) -> refer.
    3. Quoted text labelled with a reference but resembling nothing -> a
       finding for the cited verse, so it cannot vanish as "no quotations".
    4. A reference to a verse that does not exist -> UNRESOLVED.
    """
    words = text.split()
    cites = _all_citations(text, words)
    cite_words = {i for c in cites for i in range(c[3], c[4])}
    regions = quote_regions(words, cite_words)

    def region_of(f: Finding):
        if f.start_word is None:
            return None
        return next(((qs, qe) for qs, qe in regions if qs <= f.start_word < qe), None)

    findings = [_misnumbered(mizan, f, lang, detector, words, region_of(f)) or f
                for f in findings]
    spans = [(f.start_word, f.end_word) for f in findings
             if f.start_word is not None and f.end_word is not None]

    # 1. Extra words inside the quotation marks.
    for f in findings:
        if f.start_word is None or f.end_word is None:
            continue
        for qs, qe in regions:
            if not (qs <= f.start_word < qe):
                continue
            extra = [i for i in range(qs, qe)
                     if i not in cite_words
                     and not any(a <= i < b for a, b in spans)
                     and _HAS_LETTER.search(words[i])]
            if f.state in (MATCH, NEAR) and len(extra) >= EXTRA_WORDS_MIN and not _approved_wording(
                    mizan, " ".join(words[qs:qe]), f.surah, f.ayah, lang):
                f.flags.append(FLAG_EXTRA_WORDS)
                f.flag_detail[FLAG_EXTRA_WORDS] = _excerpt([words[i] for i in extra])
            break

    # 2-4. Each printed reference against the quotation it labels.
    added: list[Finding] = []
    prev_cite_end = 0
    for su, a, last, ws, we in cites:
        placed = [f for f in findings if f.start_word is not None and f.end_word is not None]
        near = [f for f in placed
                if (0 <= ws - f.end_word <= CITE_ADJACENT_WORDS
                    or 0 <= f.start_word - we <= CITE_ADJACENT_WORDS)]
        # A quotation closing right before the reference, whose located verse
        # ends EARLIER: the words in between are inside the quotation too
        # ("<verse> and whoever eats meat on a Friday..." (Quran 39:53)).
        # Nested quotation marks inside the verse defeat region pairing, so
        # this is judged from the reference backwards.
        if not near and ws > 0 and _CLOSE_Q.search(words[ws - 1]):
            before = [f for f in placed
                      if prev_cite_end <= f.end_word < ws and ws - f.end_word <= TAIL_MAX_WORDS]
            if before:
                f = max(before, key=lambda f: f.end_word)
                tail = [i for i in range(f.end_word, ws)
                        if i not in cite_words and _HAS_LETTER.search(words[i])]
                opens = [i for i in range(f.end_word, ws) if _OPEN_Q.search(words[i])]
                quoted = " ".join(words[max(0, f.start_word - 3):ws])
                if (f.state in (MATCH, NEAR) and not opens and len(tail) >= EXTRA_WORDS_MIN
                        and not _approved_wording(mizan, quoted, f.surah, f.ayah, lang)):
                    if FLAG_EXTRA_WORDS not in f.flags:
                        f.flags.append(FLAG_EXTRA_WORDS)
                        f.flag_detail[FLAG_EXTRA_WORDS] = _excerpt([words[i] for i in tail])
                    near = [f]
        prev_cite_end = we
        if near:
            # A reference normally FOLLOWS its quotation; the text after it
            # labels it only when nothing sits right before it.
            f = min(near, key=lambda f: (0 if 0 <= ws - f.end_word <= CITE_ADJACENT_WORDS else 1,
                                         min(abs(ws - f.end_word), abs(f.start_word - we))))
            if f.surah == su and a <= f.ayah <= last:
                continue
            # A verse whose approved wording is identical to the cited verse's
            # (16:42 = 29:59) is not a mismatch: the author's id stands.
            if any(verbatim(f.published_text, mizan.verse(b, su, x) or "")
                   for b in mizan.editions(lang) for x in range(a, last + 1)):
                continue
            # A lexical guess that matched nothing approved, next to the
            # author's explicit reference to another verse: the reference is
            # the better evidence of WHICH verse this is (2:45 and 2:153 share
            # most of their words). Re-place it there; it stays referred.
            if (f.state == UNATTRIBUTED and f.tier != "reference"
                    and _verse_exists(mizan, su, a, lang)):
                nf = build_finding(mizan, f.published_text, su, a, lang,
                                   arabic_text=f.arabic_text, start_word=f.start_word,
                                   end_word=f.end_word, tier="reference")
                findings[findings.index(f)] = nf
                continue
            if FLAG_CITATION_MISMATCH not in f.flags:
                f.flags.append(FLAG_CITATION_MISMATCH)
                f.flag_detail[FLAG_CITATION_MISMATCH] = (
                    f"{su}:{a}" + (f"-{last}" if last != a else ""))
            continue
        # No located quotation next to this reference. Is there quoted text?
        region = next(((qs, qe) for qs, qe in regions
                       if 0 <= ws - qe <= CITE_ADJACENT_WORDS
                       or 0 <= qs - we <= CITE_ADJACENT_WORDS), None)
        if region is None:
            continue          # a bare reference in prose ("see 3:7"): nothing to check
        quoted = " ".join(words[region[0]:region[1]])
        if len(_HAS_LETTER.findall(quoted)) < 3:
            continue
        if any(g.surah == su and a <= g.ayah <= last and g.start_word is not None
               and abs(g.start_word - region[0]) <= TAIL_MAX_WORDS for g in findings + added):
            continue          # this verse already has a card right here
        if not _verse_exists(mizan, su, a, lang):
            nf = unresolved_finding(quoted, lang, region[0], region[1])
            nf.ref = f"{su}:{a}"
            nf.flags.append(FLAG_NO_SUCH_VERSE)
            nf.flag_detail[FLAG_NO_SUCH_VERSE] = f"{su}:{a}"
        else:
            nf = build_finding(mizan, quoted, su, a, lang,
                               start_word=region[0], end_word=region[1],
                               tier="reference")
            if nf.state != MATCH:
                nf.flags.append(FLAG_CITED_NOT_FOUND)
        added.append(nf)
    if added:
        findings = sorted(findings + added,
                          key=lambda f: (f.start_word if f.start_word is not None else 1 << 30))
    findings = _merge_fragments(findings)
    for f in findings:
        if f.flags:
            f.message = " ".join([f.message] + [FLAG_MESSAGE[x] for x in f.flags])
    return findings


_STATE_RANK = {MATCH: 0, NEAR: 1, UNATTRIBUTED: 2, NO_APPROVED_TRANSLATION: 3, UNRESOLVED: 4}


def _merge_fragments(findings: list[Finding]) -> list[Finding]:
    """One card per quotation: pieces of the same verse that touch or overlap
    (a reference-tier fragment beside a lexical one) are one quotation. The
    piece with the stronger state, then the longer span, is kept. Two copies
    of a verse far apart are two quotations and both stay (red-team H1)."""
    out: list[Finding] = []
    for f in findings:
        prev = next((g for g in reversed(out)
                     if g.ref == f.ref and g.start_word is not None and f.start_word is not None
                     and f.start_word - g.end_word <= 5), None)
        if prev is None:
            out.append(f)
            continue
        def key(x):
            return (_STATE_RANK.get(x.state, 9), -((x.end_word or 0) - (x.start_word or 0)))
        if key(f) < key(prev):
            out[out.index(prev)] = f
    return out


def check_document(mizan: Mizan, text: str, lang: str, *,
                   arabic_text: str | None = None,
                   detector=None, arabic_detector=None) -> DocumentReport:
    """
    End-to-end convenience path: Arabic (optional) -> spans -> report.

    Kept thin on purpose. The pieces are useful separately — a caller that
    already has spans should call build_finding/build_report directly rather
    than re-running detection.

    Parameters
    ----------
    text:
        The translation to check.
    lang:
        ISO 639-1 code of `text`.
    arabic_text:
        The Arabic original, when the publisher has it. Supplying it makes
        detection markedly stronger: verses resolved in Arabic are searched for
        in the translation directly rather than retrieved blind.
    detector / arabic_detector:
        Pre-built SpanDetector / ArabicDetector. Pass them in to avoid paying
        index construction on every request.
    """
    from .detect import SpanDetector   # local import: report has no hard dep

    arabic_refs: list[tuple[int, int]] = []
    arabic_texts: dict[tuple[int, int], str] = {}
    arabic_quotes: dict[tuple[int, int], str] = {}   # the author's own words
    if arabic_text:
        if arabic_detector is None:
            from .arabic import ArabicDetector
            arabic_detector = ArabicDetector()
        for q in arabic_detector.detect(arabic_text):
            arabic_refs.append((q.surah, q.ayah))
            arabic_texts[(q.surah, q.ayah)] = q.approved_text
            arabic_quotes[(q.surah, q.ayah)] = q.text
        for s, a in arabic_detector.citations(arabic_text):
            if (s, a) not in arabic_texts:
                arabic_refs.append((s, a))
                arabic_texts[(s, a)] = arabic_detector.verse(s, a) or ""

    # A language with no approved translation indexed is the fourth state, not
    # a failure: the Arabic resolved fine, we simply cannot attribute it.
    if not mizan.editions(lang):
        findings = [
            Finding(ref=f"{s}:{a}", surah=s, ayah=a,
                    state=NO_APPROVED_TRANSLATION, lang=lang, score=0.0,
                    published_text="", arabic_text=arabic_texts.get((s, a)),
                    message=STATE_MESSAGE[NO_APPROVED_TRANSLATION])
            for s, a in dict.fromkeys(arabic_refs)
        ]
        return build_report(mizan, findings, lang)

    if detector is None:
        detector = SpanDetector(mizan, lang)
    spans = detector.find(text, arabic_refs=list(dict.fromkeys(arabic_refs)) or None)

    findings = [
        build_finding(mizan, sp.text, sp.surah, sp.ayah, lang,
                      arabic_text=arabic_texts.get((sp.surah, sp.ayah)),
                      start_word=sp.start_word, end_word=sp.end_word,
                      tier=sp.tier)
        for sp in spans
    ]

    # Verses the Arabic resolved but the translation never surfaced: the
    # translation may have dropped or paraphrased them past recognition. Report
    # them rather than silently passing the document.
    placed = {(f.surah, f.ayah) for f in findings}
    for s, a in dict.fromkeys(arabic_refs):
        if (s, a) in placed:
            continue
        if _is_alias_of_placed(arabic_detector, arabic_quotes.get((s, a)), placed):
            continue
        findings.append(Finding(
            ref=f"{s}:{a}", surah=s, ayah=a, state=UNATTRIBUTED, lang=lang,
            score=0.0, published_text="", arabic_text=arabic_texts.get((s, a)),
            message="Quoted in the Arabic, but no matching rendering was "
                    "located in the translation — referred for review.",
        ))

    findings = _context_checks(mizan, text, lang, findings, detector)
    return build_report(mizan, findings, lang)
