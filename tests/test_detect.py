#!/usr/bin/env python3
"""
MIZAN — detection and attribution tests.

Runnable either way, on either path:
    python3 -m pytest tests/test_detect.py -v              # deterministic path
    .venv/bin/python -m pytest tests/test_detect.py -v     # + semantic tier (c)

Tests that depend on tier (c) assert the semantic behaviour when the tier is
available and the deterministic behaviour when it is not, so the suite is
meaningful on a machine without the ML extras rather than merely skipped.

Every quoted text here is pulled from the corpus at runtime rather than pasted
into the file. Pasting scripture into a test file would create a second,
unversioned copy of the text that could silently drift from the index — exactly
the failure mode Mizan exists to catch.
"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mizan import semantic                                    # noqa: E402
from mizan.arabic import ArabicDetector, fold_arabic          # noqa: E402
from mizan.detect import (SHORT_SPAN_THRESHOLD, SHORT_SPAN_WORDS,  # noqa: E402
                          SpanDetector, citation_word_ranges,
                          printed_references, semantic_candidates,
                          snap_to_quotes)
from mizan.engine import Mizan                                 # noqa: E402
from mizan.report import (MATCH, NEAR, UNATTRIBUTED,           # noqa: E402
                          NO_APPROVED_TRANSLATION, VERDICT_CLEAR,
                          VERDICT_REFER, build_finding, build_report,
                          check_document, word_diff)

# Built once: index construction is the slow part and nothing mutates it.
M = Mizan()
_DETECTORS: dict[str, SpanDetector] = {}
_ARABIC: list[ArabicDetector] = []

# book_ids from the quranpedia dump, resolved by title so a renumbered dump
# fails loudly here instead of quietly testing the wrong edition.
def _book(title_fragment: str) -> int:
    hits = [b["book_id"] for b in M.books.values()
            if title_fragment.lower() in (b["title"] or "").lower()]
    assert hits, f"no indexed translation matching {title_fragment!r}"
    return hits[0]


PICKTHALL = _book("Pickthall")
YUSUFALI = _book("Yusuf Ali")
SAHIH = _book("Sahih International")


def _shortest_edition(lang: str, surah: int, ayah: int) -> int:
    """
    The edition with the most compact rendering of this verse.

    Several indexed editions are footnoted or tafsir renderings running to
    hundreds of words; tests that want "a plain quotation of a verse" should
    pick the compact one explicitly rather than whichever sorts first.
    """
    cands = [(len((M.verse(b, surah, ayah) or "").split()), b)
             for b in M.editions(lang) if M.verse(b, surah, ayah)]
    assert cands, f"no {lang} rendering of {surah}:{ayah}"
    return min(cands)[1]


SEMANTIC = semantic.available()


def detector(lang: str) -> SpanDetector:
    """The default detector: tier (c) on exactly when it is available."""
    if lang not in _DETECTORS:
        _DETECTORS[lang] = SpanDetector(M, lang)
    return _DETECTORS[lang]


def lexical_detector(lang: str) -> SpanDetector:
    """Deterministic tiers only, whatever is installed."""
    key = lang + ":lexical"
    if key not in _DETECTORS:
        _DETECTORS[key] = SpanDetector(M, lang, semantic=False)
    return _DETECTORS[key]


def arabic() -> ArabicDetector:
    if not _ARABIC:
        _ARABIC.append(ArabicDetector())
    return _ARABIC[0]


# ======================================================================
# Arabic-side detection
# ======================================================================

def test_arabic_corpus_is_complete():
    """6236 ayat, 114 surahs. A short download poisons everything downstream."""
    n = arabic().con.execute("SELECT COUNT(*) FROM arabic_ayat").fetchone()[0]
    surahs = arabic().con.execute(
        "SELECT COUNT(DISTINCT surah) FROM arabic_ayat").fetchone()[0]
    assert n == 6236, f"expected 6236 ayat, got {n}"
    assert surahs == 114, f"expected 114 surahs, got {surahs}"


def test_arabic_detects_plain_quote():
    """A verse quoted in ordinary (undiacritised) Arabic resolves to its id."""
    det = arabic()
    simple = det.con.execute(
        "SELECT text_simple FROM arabic_ayat WHERE surah=51 AND ayah=56"
    ).fetchone()[0]
    doc = f"قال الله تعالى في كتابه: {simple}. وهذه الآية أصل في بيان الغاية من الخلق."
    refs = {(q.surah, q.ayah) for q in det.detect(doc)}
    assert (51, 56) in refs, f"51:56 not detected, got {refs}"


def test_arabic_detects_uthmani_quote():
    """
    The same verse pasted in Uthmani orthography must resolve identically.

    This is the case that breaks naive matchers: Uthmani writes the long alef
    as a superscript mark, so after diacritic-stripping the two spellings agree
    on only ~71% of words. Both spellings are indexed for exactly this reason.
    """
    det = arabic()
    uthmani = det.con.execute(
        "SELECT text_uthmani FROM arabic_ayat WHERE surah=51 AND ayah=56"
    ).fetchone()[0]
    doc = f"قال تعالى: {uthmani} وفي الآية بيان عظيم."
    refs = {(q.surah, q.ayah) for q in det.detect(doc)}
    assert (51, 56) in refs, f"Uthmani spelling not detected, got {refs}"


def test_arabic_honours_ornate_brackets():
    """﴿ ﴾ is an explicit assertion of scripture and is honoured directly."""
    det = arabic()
    uthmani = det.con.execute(
        "SELECT text_uthmani FROM arabic_ayat WHERE surah=1 AND ayah=5"
    ).fetchone()[0]
    quotes = det.detect(f"وقال سبحانه ﴿{uthmani}﴾ وهي تتكرر في كل ركعة.")
    assert any((q.surah, q.ayah) == (1, 5) for q in quotes)


def test_arabic_parses_citation_marker():
    """[السورة: رقم] with Arabic-Indic digits resolves to (surah, ayah)."""
    assert (2, 255) in arabic().citations("ورد في [البقرة: ٢٥٥] آية الكرسي.")
    assert (51, 56) in arabic().citations("انظر (الذاريات: 56) في هذا الباب.")


def test_arabic_no_false_positive_on_plain_prose():
    """Pious prose that quotes nothing must produce no detections."""
    prose = [
        "إن الإسلام يدعو إلى الصدق في المعاملة والإحسان إلى الجار ورعاية الوالدين.",
        "عقدت الجمعية اجتماعها السنوي وناقش الحاضرون خطة العمل للعام القادم.",
        "يرى الباحثون أن الترجمة عمل دقيق يحتاج إلى معرفة باللغتين وإلمام بالسياق.",
    ]
    for p in prose:
        found = arabic().detect(p)
        assert found == [], f"false positive on {p!r}: {[q.ref for q in found]}"


def test_arabic_never_modifies_scripture():
    """`verse()` returns the stored Tanzil bytes, unaltered."""
    det = arabic()
    stored = det.con.execute(
        "SELECT text_uthmani FROM arabic_ayat WHERE surah=2 AND ayah=255"
    ).fetchone()[0]
    assert det.verse(2, 255) == stored


def test_fold_arabic_converges_orthographies():
    """Folding is additive over engine.normalize(), not a replacement."""
    assert fold_arabic("ٱلْحَمْدُ") == fold_arabic("الحمد")


# ======================================================================
# Tier (a): printed references
# ======================================================================

def test_printed_reference_formats():
    """The common citation styles, Latin and Arabic-Indic, all parse."""
    text = ("As it says (Quran 51:56), and again in [Qur'an 2:255], "
            "and in Arabic القرآن ١١٢:١ too.")
    refs = {(s, a) for s, a, _ in printed_references(text)}
    assert (51, 56) in refs
    assert (2, 255) in refs
    assert (112, 1) in refs


def test_printed_reference_rejects_impossible_ids():
    """Surah 999 is not a verse id; a page number must not become one."""
    refs = {(s, a) for s, a, _ in printed_references("see Quran 999:1 and 0:5")}
    assert refs == set()


# ======================================================================
# Tier (c): the semantic layer (bge-m3, local)
# ======================================================================

def test_semantic_contract_ids_and_scores_or_raise():
    """
    Tier (c) either proposes verse ids — never text — or RAISES. It must not
    return an empty list when it could not look, because "nothing resembles a
    verse" and "the model is unavailable" are different statements.
    """
    try:
        out = semantic_candidates("Say: He is Allah, the One and Only", "en")
    except semantic.SemanticUnavailable:
        assert not SEMANTIC, "available() said yes but the call raised"
        return
    assert SEMANTIC
    assert 0 < len(out) <= 20
    for item in out:
        assert len(item) == 3 and not any(isinstance(x, str) for x in item)
        s, a, score = item
        assert 1 <= s <= 114 and a >= 1 and 0.0 <= score <= 1.0


def test_find_degrades_cleanly_without_semantic():
    """
    Asking for tier (c) where it cannot run must fall back to the lexical tier
    and say why — never crash, never silently pretend.
    """
    verse = M.verse(PICKTHALL, 112, 1)
    doc = f"The Quran answers plainly. {verse} A short chapter."
    old = os.environ.get("MIZAN_SEMANTIC")
    os.environ["MIZAN_SEMANTIC"] = "0"
    try:
        det = SpanDetector(M, "en", semantic=True)
        spans = det.find(doc)
        assert det.last_semantic["used"] is False
    finally:
        if old is None:
            os.environ.pop("MIZAN_SEMANTIC", None)
        else:
            os.environ["MIZAN_SEMANTIC"] = old
    assert any(s.ref == "112:1" for s in spans)


# Fresh renderings written for this test — deliberately NOT any published
# translation. On the deterministic path none of them is located at all.
#
# Not every fresh rendering is located by tier (c), and the misses are worth
# knowing: "every soul shall taste death" (3:185) is also 21:35 and 29:57, and
# "no soul is burdened beyond its capacity" (2:286) recurs in 2:233, 6:152,
# 7:42, 23:62 and 65:7. When the Quran repeats its own wording, the model's top
# verse barely beats the next, and the margin gate declines to pick one. 2:255
# is a poor probe for another reason: its first sentence IS the whole of 3:2.
# The held-out measurement (tests/test_semantic.py --measure) is the honest
# rate; these are illustrations that must keep working.
FRESH = {
    (51, 56): ("I brought the jinn and humankind into being for no reason other "
               "than that they should serve Me."),
    (49, 13): ("O humanity, We formed you from one male and one female and "
               "arranged you into peoples and tribes so that you might come to "
               "know one another; the noblest of you before God is the one most "
               "conscious of Him."),
    (17, 23): ("Your Lord has commanded that you worship none but Him and that you "
               "treat your parents with goodness; if one or both reach old age "
               "with you, do not say to them so much as a sigh of irritation, nor "
               "scold them."),
}


def test_fresh_paraphrase_is_located_and_referred_never_matched():
    """
    A rendering written from scratch shares too little vocabulary with any
    approved edition for the lexical tier: on the deterministic path these are
    not located at all. That is the measured failure tier (c) exists for.

    With tier (c), each is LOCATED at the right verse and REFERRED — NEAR or
    UNATTRIBUTED — because the verdict is still engine.sim() against the
    approved text. Without it, whatever is found must at least never be a
    MATCH. Either way the model never decides a verdict.
    """
    for (s, a), fresh in FRESH.items():
        doc = ("Many readers find comfort in one passage above all. " + fresh +
               " It is often recited in many households.")
        rep = check_document(M, doc, "en", detector=detector("en"))
        assert all(f.state != MATCH for f in rep.findings), (
            f"{s}:{a}: a fresh rendering must never MATCH an approved edition")
        if SEMANTIC:
            hit = [f for f in rep.findings if (f.surah, f.ayah) == (s, a)]
            assert hit, f"{s}:{a} not located: {[(f.ref, f.tier) for f in rep.findings]}"
            assert hit[0].state in (NEAR, UNATTRIBUTED)
            assert rep.verdict == VERDICT_REFER


def test_fresh_paraphrase_is_unattributed():
    """
    Attribution alone (verse id given) of a fresh rendering: UNATTRIBUTED.

    This is the honest limit of the deterministic comparison, and it does not
    change with tier (c): the model may LOCATE such a passage, but the words
    still match no indexed translation closely enough to attribute, so the
    system refers rather than guesses.
    """
    fresh = ("God — there is no deity except Him, the Ever-Living, the One who "
             "upholds all existence. Neither drowsiness nor sleep ever overtakes "
             "Him. To Him belongs whatever fills the heavens and whatever fills "
             "the earth.")
    f = build_finding(M, fresh, 2, 255, "en")
    assert f.state == UNATTRIBUTED, (
        f"expected UNATTRIBUTED, got {f.state} at score {f.score} "
        f"({f.attributed_title})")
    assert f.attributed_to is None
    # It must still show the comparison, so a reviewer can judge for themselves.
    assert f.approved_text


# ======================================================================
# Attribution verdicts — the four states that matter
# ======================================================================

def test_verbatim_quote_is_match_and_names_the_right_book():
    """
    A verbatim Pickthall quotation inside prose: detected, MATCH, and
    attributed to Pickthall specifically — not merely to "some approved book".
    """
    verse = M.verse(PICKTHALL, 49, 13)
    doc = ("On human equality the Quran is explicit. " + verse +
           " Race and lineage confer no rank before God.")

    spans = detector("en").find(doc)
    hit = [s for s in spans if s.ref == "49:13"]
    assert hit, f"49:13 not detected; got {[s.ref for s in spans]}"

    f = build_finding(M, hit[0].text, 49, 13, "en")
    assert f.state == MATCH, f"expected MATCH, got {f.state} at {f.score}"
    assert f.attributed_to == PICKTHALL, (
        f"attributed to {f.attributed_title!r}, expected Pickthall")
    assert f.score >= 0.92


def test_modified_quote_is_near_not_match():
    """
    An approved rendering with small edits is NEAR: derived, but altered.

    The edit is mechanical (two word substitutions) so the test states what it
    changed rather than relying on a hand-written near-copy that could drift.
    """
    verse = M.verse(SAHIH, 21, 107)
    # Derived from the Sahih rendering by small, deliberate substitutions:
    # "have not sent" -> "did not send", brackets dropped, "to the worlds" ->
    # "unto all the worlds". A short verse is used on purpose — the same two
    # edits inside a 100-word verse barely move the score, so the NEAR band
    # only has meaning relative to the length of what was quoted.
    modified = ("And We did not send you, O Muhammad, except as a mercy unto "
                "all the worlds.")
    assert modified != verse

    f = build_finding(M, modified, 21, 107, "en")
    assert f.state == NEAR, (
        f"expected NEAR, got {f.state} at score {f.score}")
    assert 0.75 <= f.score < 0.92
    assert f.n_edits > 0, "a modified quote must show edits in the diff"


def test_wrong_verse_text_is_rejected():
    """Another verse's text, offered as this verse, must not be attributed."""
    other = M.verse(PICKTHALL, 2, 255)
    f = build_finding(M, other, 112, 1, "en")
    assert f.state == UNATTRIBUTED


# ======================================================================
# False-positive probe
# ======================================================================

def test_paragraph_with_no_quotation_yields_zero_findings():
    """
    The control that matters most: ordinary prose about Islam, quoting nothing.

    A gate that fires on the author's own sentences is unusable, because the
    author's prose has no approved text to be compared against.
    """
    prose = ("Islam teaches that worship is not confined to ritual. Honesty in "
             "trade, kindness to neighbours and care for parents are all acts of "
             "devotion. Many new Muslims are surprised by how much of the "
             "religion concerns daily conduct rather than ceremony.")
    spans = detector("en").find(prose)
    assert spans == [], f"false positives: {[(s.ref, s.score, s.text) for s in spans]}"

    report = check_document(M, prose, "en")
    assert report.findings == []
    assert report.verdict == VERDICT_CLEAR


def test_secular_prose_yields_zero_findings():
    """Non-religious prose must also stay clean."""
    prose = ("The committee met on Tuesday to review the annual budget. Members "
             "discussed the cost of the new building and agreed to postpone a "
             "decision until the next quarter, when fuller figures are available.")
    assert detector("en").find(prose) == []


# ======================================================================
# Other languages — Bengali and Urdu
# ======================================================================

def test_bengali_quote_resolves_and_attributes():
    """
    A verbatim Bengali quotation is located and attributed to the right book.

    51:56 is used rather than 2:255 because the Bengali editions render 2:255
    with extensive footnotes (1,272 words in edition 1967) — see
    test_footnoted_edition_is_a_known_limitation for that case, which is a
    property of the corpus, not of the detector.
    """
    bn_book = _shortest_edition("bn", 51, 56)
    verse = M.verse(bn_book, 51, 56)
    doc = "কুরআনে বলা হয়েছে। " + verse + " এই আয়াতটি প্রসিদ্ধ।"

    spans = detector("bn").find(doc)
    hit = [s for s in spans if s.ref == "51:56"]
    assert hit, f"Bengali 51:56 not detected; got {[s.ref for s in spans]}"

    f = build_finding(M, hit[0].text, 51, 56, "bn")
    assert f.state == MATCH, f"got {f.state} at {f.score}"
    assert f.attributed_to == bn_book


def test_footnoted_edition_is_a_known_limitation():
    """
    KNOWN LIMITATION, asserted so it cannot regress unnoticed.

    Some indexed editions interleave translator footnotes with the verse: the
    Bengali edition 1967 stores 1,272 words under 2:255, of which the verse is
    a small part. A reader quoting that whole entry is still located, but the
    span covers only the verse-like portion, so the score lands below MATCH.

    The system behaves correctly here — it refers rather than guessing — but
    the measured score is reported honestly rather than papered over.
    """
    bn_book = 1967
    verse = M.verse(bn_book, 2, 255)
    assert len(verse.split()) > 500, "corpus changed; revisit this limitation"

    doc = "কুরআনে বলা হয়েছে। " + verse + " এই আয়াতটি আয়াতুল কুরসি।"
    spans = detector("bn").find(doc)
    hit = [s for s in spans if s.ref == "2:255"]
    assert hit, "the verse should still be LOCATED even when footnoted"
    # Located, but not confidently attributable — which is the safe outcome.
    assert hit[0].score < 0.92


def test_urdu_quote_resolves_and_attributes():
    """A verbatim Urdu quotation is located and attributed to the right book."""
    ur_book = _shortest_edition("ur", 13, 28)
    verse = M.verse(ur_book, 13, 28)
    doc = ("دلوں کا سکون اللہ کے ذکر میں ہے۔ " + verse +
           " اس لیے ذکر کی عادت ڈالنی چاہیے۔")

    spans = detector("ur").find(doc)
    hit = [s for s in spans if s.ref == "13:28"]
    assert hit, f"Urdu 13:28 not detected; got {[s.ref for s in spans]}"

    f = build_finding(M, hit[0].text, 13, 28, "ur")
    assert f.state in (MATCH, NEAR)
    assert f.attributed_to == ur_book


def test_hindi_quote_resolves():
    """Hindi has a single indexed edition — attribution must still work."""
    hi_book = M.editions("hi")[0]
    verse = M.verse(hi_book, 49, 13)
    doc = "क़ुरआन में समानता के बारे में कहा गया है। " + verse + " यह आयत प्रसिद्ध है।"
    spans = detector("hi").find(doc)
    assert any(s.ref == "49:13" for s in spans), [s.ref for s in spans]


# ======================================================================
# The fifth state, and the document rule
# ======================================================================

def test_no_approved_translation_state():
    """
    A language with no indexed translation yields NO_APPROVED_TRANSLATION,
    not UNRESOLVED.

    Without this state every quotation in an uncovered language would fall into
    "unresolved" and drag every document to referral — which would make the
    tool useless precisely where coverage is thinnest.
    """
    assert M.editions("sw") == [], "test assumes Swahili is not indexed"
    r = M.attribute("some text", 51, 56, "sw")
    assert r["state"] == NO_APPROVED_TRANSLATION

    report = check_document(
        M, "Hakuna tafsiri iliyoidhinishwa kwa lugha hii.", "sw",
        arabic_text="قال الله تعالى: وما خلقت الجن والإنس إلا ليعبدون.")
    assert report.findings, "Arabic resolved, so there must be a finding"
    assert all(f.state == NO_APPROVED_TRANSLATION for f in report.findings)
    # It is a coverage gap, not a problem with the document.
    assert report.verdict == VERDICT_CLEAR


def test_one_unattributed_item_refers_the_whole_document():
    """The governing rule: a gate whose result can be diluted is not a gate."""
    good = build_finding(M, M.verse(PICKTHALL, 112, 1), 112, 1, "en")
    assert good.state == MATCH
    bad = build_finding(M, "Something that resembles no approved rendering at "
                           "all, written freshly for this test.", 2, 255, "en")
    assert bad.state == UNATTRIBUTED

    assert build_report(M, [good], "en").verdict == VERDICT_CLEAR
    assert build_report(M, [good, bad], "en").verdict == VERDICT_REFER
    assert build_report(M, [bad, good, good], "en").verdict == VERDICT_REFER


def test_empty_document_is_clear():
    """No quotation means nothing to attribute — not a referral."""
    r = build_report(M, [], "en")
    assert r.verdict == VERDICT_CLEAR
    assert "No Quranic quotation" in r.summary


# ======================================================================
# Report shape — what a UI actually consumes
# ======================================================================

def test_finding_carries_everything_a_ui_needs():
    """Each finding must be renderable without a second lookup."""
    f = build_finding(M, M.verse(YUSUFALI, 1, 5), 1, 5, "en")
    d = f.as_dict()
    for key in ("ref", "state", "attributed_to", "attributed_title", "score",
                "published_text", "approved_text", "diff", "message"):
        assert key in d, f"missing {key} in finding"
    assert d["attributed_title"], "a MATCH must name the translation"
    assert d["message"], "every state needs human-facing wording"


def test_report_is_json_serialisable():
    """The API hands this straight out; it must not contain dataclasses."""
    import json
    verse = M.verse(PICKTHALL, 112, 1)
    report = check_document(M, f"The Quran says plainly. {verse} So it is.", "en")
    blob = json.dumps(report.as_dict(), ensure_ascii=False)
    assert "MATCH" in blob
    assert report.as_dict()["index"]["version"] == M.version


def test_word_diff_marks_the_changed_words():
    """The diff must localise the edit, not just report that one happened."""
    ops, n = word_diff("Say: He is Allah, the Only One!",
                       "Say: He is Allah, the One!")
    assert n >= 1
    changed = [o for o in ops if o.op != "equal"]
    assert changed, "a real edit must produce a non-equal op"
    assert any("Only" in " ".join(o.published) for o in changed)


def test_report_never_says_the_text_is_wrong():
    """
    Vocabulary discipline: the output is attribution, never a judgement on
    scripture. "wrong"/"error"/"incorrect" must appear nowhere user-facing.
    """
    fresh = ("God, there is no deity but He, the Living, the Sustainer of all "
             "that exists, whom neither slumber nor sleep can seize.")
    f = build_finding(M, fresh, 2, 255, "en")
    report = build_report(M, [f], "en")
    blob = (report.summary + " " + " ".join(x.message for x in report.findings)).lower()
    for banned in ("wrong", "error", "incorrect", "false"):
        assert banned not in blob, f"report used the word {banned!r}: {blob}"


# ======================================================================
# Boundary refinement — the step worth 36 points
# ======================================================================

def test_refinement_recovers_a_clipped_span():
    """
    A window that clips the quotation and swallows prose must be hill-climbed
    back onto the verse boundary.

    In the prototype this single step moved attribution accuracy from 64% to
    100%. The test asserts the mechanism directly rather than trusting the
    aggregate.
    """
    verse = M.verse(PICKTHALL, 49, 13)
    doc = ("Scholars often point out the following about human equality. " +
           verse + " Race and lineage confer no rank whatsoever before God.")
    words = doc.split()

    v_start = len("Scholars often point out the following about human equality.".split())
    v_end = v_start + len(verse.split())

    # Deliberately bad seed: starts 5 words early, ends 5 words short.
    bad_start, bad_end = v_start - 5, v_end - 5
    det = detector("en")
    before = det.best_rendering(49, 13, " ".join(words[bad_start:bad_end]))[2]
    rs, re_, after, book = det.refine_span(words, bad_start, bad_end, 49, 13)

    assert after > before, f"refinement did not improve: {before} -> {after}"
    assert after >= 0.92, f"refined span should reach MATCH, got {after}"
    assert book == PICKTHALL


def test_refinement_is_deterministic():
    """Same input, same span. No model, no randomness, reproducible numbers."""
    verse = M.verse(SAHIH, 2, 286)
    doc = "A comfort to those in hardship. " + verse + " So the verse is read."
    runs = [tuple(s.as_dict().items()) for _ in range(3)
            for s in detector("en").find(doc)]
    assert len(set(runs)) == len(runs) // 3, "repeated runs disagreed"


# ======================================================================
# Arabic -> translation, the full path
# ======================================================================

def test_arabic_refs_guide_translation_detection():
    """
    Supplying the Arabic original makes the translation side stronger: the
    verse is already resolved, so only its position is in question.
    """
    det = arabic()
    ar_verse = det.con.execute(
        "SELECT text_simple FROM arabic_ayat WHERE surah=51 AND ayah=56"
    ).fetchone()[0]
    en_verse = M.verse(PICKTHALL, 51, 56)

    report = check_document(
        M,
        f"The purpose of creation is stated plainly. {en_verse} Nothing is clearer.",
        "en",
        arabic_text=f"قال الله تعالى: {ar_verse}. وهذه الآية أصل عظيم.")

    refs = {f.ref for f in report.findings}
    assert "51:56" in refs, f"got {refs}"
    f = next(x for x in report.findings if x.ref == "51:56")
    assert f.state in (MATCH, NEAR)
    assert f.arabic_text, "the Arabic source text must be carried through"


def test_arabic_quote_missing_from_translation_is_reported():
    """
    A verse quoted in the Arabic but absent from the translation must not pass
    silently — the translation may have dropped it.
    """
    det = arabic()
    ar_verse = det.con.execute(
        "SELECT text_simple FROM arabic_ayat WHERE surah=51 AND ayah=56"
    ).fetchone()[0]
    report = check_document(
        M,
        "This passage discusses the purpose of human life in general terms, "
        "without reproducing any verse of the Quran at all.",
        "en",
        arabic_text=f"قال الله تعالى: {ar_verse}. وهذه الآية أصل عظيم.")

    assert any(f.ref == "51:56" for f in report.findings)
    assert report.verdict == VERDICT_REFER


# ======================================================================
# The demo's own samples, and the defects they exposed
# ======================================================================
#
# These three paragraphs are the app's hardcoded examples (web/app.js), copied
# here as regression fixtures: publisher-style prose around a quotation, not
# scripture references. Each assertion below is a defect the app agent found
# by clicking them.

DEMO_51_56 = ('Then He clarified that creation was not left purposeless, saying: '
              '"And I did not create the jinn and mankind except to serve Me." '
              '(Quran 51:56). This verse is a foundation for understanding the '
              'purpose of human existence.')
DEMO_3_59 = ('God said concerning Jesus, peace be upon him: "Indeed, the likeness '
             'of Jesus with God is as the likeness of Adam." (Quran 3:59). Being '
             'created without a father is no proof of divinity.')
DEMO_2_255 = ('Among the greatest of His descriptions of Himself: "Allah! There is '
              'no deity save Him, the Alive, the Eternal." (Quran 2:255). This is '
              'the Verse of the Throne.')


def test_citation_is_a_wall_the_span_cannot_cross():
    """
    The 3:59 span used to run past "(Quran 3:59)." into the author's next
    sentence ("Being created without a father is"), which dragged its score
    down. A quotation ends where its citation begins.
    """
    spans = detector("en").find(DEMO_3_59)
    hit = [s for s in spans if s.ref == "3:59"]
    assert hit, [s.as_dict() for s in spans]
    assert "Being" not in hit[0].text and "3:59)" not in hit[0].text
    assert hit[0].text.startswith('"Indeed') and hit[0].text.endswith('Adam."')
    assert hit[0].tier == "reference"
    # A faithful partial quotation: most of its words are in an approved edition.
    assert hit[0].coverage is not None and hit[0].coverage >= 0.8


def test_spurious_short_span_is_gone():
    """'peace be upon him: "Indeed, the likeness' once scored 0.588 against
    37:181 and pushed the real 3:59 quotation out of the report."""
    refs = {s.ref for s in detector("en").find(DEMO_3_59)}
    assert "37:181" not in refs, refs
    assert "3:59" in refs


def test_printed_reference_wins_over_a_near_identical_verse():
    """
    "Allah! There is no deity save Him, the Alive, the Eternal" is the whole
    of 3:2 AND the opening of 2:255. The publisher printed 2:255 and the
    quoted words are contained in it, so the publisher's id wins.
    """
    spans = detector("en").find(DEMO_2_255)
    refs = [s.ref for s in spans]
    assert "2:255" in refs and "3:2" not in refs, refs
    hit = next(s for s in spans if s.ref == "2:255")
    assert hit.tier == "reference" and hit.coverage >= 0.8


def test_span_extends_to_the_authors_quotation_marks():
    """Similarity peaked one word short ('...except to'); the reviewer must see
    everything the author put inside the quotation marks."""
    hit = [s for s in detector("en").find(DEMO_51_56) if s.ref == "51:56"]
    assert hit and hit[0].text == '"And I did not create the jinn and mankind except to serve Me."'


def test_quote_snapping_is_bounded():
    words = 'He said "a b c d e f" and left.'.split()
    assert snap_to_quotes(words, 3, 6) == (2, 8)        # back to the open, on to the close
    assert snap_to_quotes(words, 3, 4) == (2, 4)        # close 4 words away: not reached
    assert snap_to_quotes("no quotes here at all".split(), 1, 3) == (1, 3)


def test_citation_word_ranges():
    rng = citation_word_ranges(DEMO_3_59)
    words = DEMO_3_59.split()
    assert [(s, a) for s, a, _, _ in rng] == [(3, 59)]
    _, _, w0, w1 = rng[0]
    assert words[w0:w1] == ["(Quran", "3:59)."]


def test_arabic_resolved_spans_are_labelled_arabic():
    """Spans placed from the Arabic source must not claim a printed reference."""
    det = arabic()
    ar_verse = det.con.execute(
        "SELECT text_simple FROM arabic_ayat WHERE surah=51 AND ayah=56").fetchone()[0]
    en_verse = M.verse(PICKTHALL, 51, 56)
    report = check_document(
        M, f"The purpose of creation is stated plainly. {en_verse} Nothing is clearer.",
        "en", arabic_text=f"قال الله تعالى: {ar_verse}.", detector=detector("en"))
    f = next(x for x in report.findings if x.ref == "51:56")
    assert f.tier == "arabic", f.tier


def test_short_span_gate():
    """
    The false-positive fix: a lexical span under SHORT_SPAN_WORDS words must be
    at least NEAR-quality (0.75). Chance collisions of ordinary prose sit in
    the 0.55-0.75 band; a real short verse quoted verbatim clears it easily.

    Known residual, kept visible: the probe paragraph listing "the Books of
    Abraham, the Torah of Moses" scores 0.773 against 87:19 ("The Books of
    Abraham and Moses") and still clears the bar. The bar was set on the dev
    split, not moved to silence a test example.
    """
    det = lexical_detector("en")
    assert SHORT_SPAN_THRESHOLD == 0.75 and SHORT_SPAN_WORDS == 12
    assert not det.short_span_ok("one two three four five six seven", 0.70)
    assert det.short_span_ok("one two three four five six seven", 0.76)
    assert not det.short_span_ok("one two three four five", 0.99)    # under MIN_SPAN_WORDS
    assert det.short_span_ok(" ".join(["w"] * 12), 0.56)            # long: plain threshold
    prose = ("Islam teaches that worship is not confined to ritual. Honesty in "
             "trade, kindness to neighbours and care for parents are all acts of "
             "devotion, and the Prophet taught that a smile is charity.")
    assert det.find(prose) == [], [s.as_dict() for s in det.find(prose)]
    short = M.verse(SAHIH, 112, 1)
    assert any(s.ref == "112:1" for s in det.find(f"It is said: {short} That is all."))


def test_index_is_shared_between_detectors():
    """report.check_document builds a SpanDetector per call when none is
    passed; the index must not be rebuilt each time."""
    a, b = SpanDetector(M, "en", semantic=False), SpanDetector(M, "en", semantic=False)
    assert a._ix is b._ix


# ======================================================================
# Plain-python runner
# ======================================================================

def _main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed.append((name, e))
            print(f"  FAIL  {name}\n          {e}")
        except Exception as e:                      # noqa: BLE001
            failed.append((name, e))
            print(f"  ERROR {name}\n          {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
