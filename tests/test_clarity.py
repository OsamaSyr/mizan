#!/usr/bin/env python3
"""
MIZAN — Panel 3 (clarity / audience fit) tests.

Runnable either way:
    python3 -m pytest tests/test_clarity.py -v
    python3 tests/test_clarity.py

What these tests are actually defending
---------------------------------------
Panel 3's value is not that it makes prose nicer. It is that it can be PROVEN
never to have touched scripture or authored a ruling. So the suite is weighted
accordingly: the byte-exactness and refusal tests are the load-bearing ones and
the readability test is the smaller claim.

Scripture used here is pulled from the corpus at runtime, never pasted in.
Pasting a verse into a test file creates a second, unversioned copy that can
drift from the index — the exact failure mode Mizan exists to catch.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mizan.clarity import (                                    # noqa: E402
    CURIOUS, DAEE, ELIGIBLE, GLOSSARY, LEVELS, NEW_MUSLIM, PRACTISING,
    PROSE, QUOTED, REFUSED, R_ATTRIBUTION, R_CONDITIONAL, R_CREED,
    R_IMPLICIT_OBLIGATION, R_NORMATIVE, R_PREFERENCE, R_REFERENCE,
    R_LANGUAGE_UNSCREENED, R_ROLLBACK, R_SCRIPTURE, R_SCRIPTURE_LIKE,
    R_SCRIPTURE_UNCHECKED, R_TOO_SHORT, SUG_GLOSS, SUG_PASSIVE, SUG_SPLIT,
    ClarityReport, FrozenSpan, FrozenTokenViolation, adapt_document,
    adapt_sentence, document_frozen_spans, freeze, lexical_reasons,
    model_adapt, profile, quotation_regions, readability, refusal_reasons,
    scripture_reasons, segment, split_sentences, thaw, verify_document,
    verify_restoration,
)
from mizan import clarity as _clarity                          # noqa: E402

# ----------------------------------------------------------------------
# Corpus access. The engine is only needed for the tests that must prove a
# REAL approved verse survives untouched; everything else is pure Panel 3 and
# runs without the 124 MB index.
# ----------------------------------------------------------------------

_M = []


def _mizan():
    """Lazily open the index; skip (not fail) if the corpus is absent."""
    if not _M:
        try:
            from mizan.engine import Mizan
            _M.append(Mizan())
        except Exception as e:                                  # noqa: BLE001
            _M.append(None)
            print(f"      (corpus unavailable: {type(e).__name__}: {e})")
    return _M[0]


def _approved_verse(surah: int, ayah: int, title_fragment: str = "Pickthall"):
    """One approved English rendering, straight from the index."""
    m = _mizan()
    if m is None:
        return None
    for b in m.books.values():
        if title_fragment.lower() in (b["title"] or "").lower():
            return m.verse(b["book_id"], surah, ayah)
    return None


# ======================================================================
# 1. A quoted verse is NEVER altered — byte-exact
# ======================================================================

def test_quoted_verse_is_byte_exact_through_the_whole_panel():
    """
    The headline guarantee: a real approved verse, embedded in prose long
    enough to trigger every transform, comes out byte-for-byte identical.
    """
    verse = _approved_verse(51, 56)
    if verse is None:
        print("      SKIP: corpus not available")
        return

    prose_before = (
        "Translators preparing material for a general readership often spend "
        "a long time on a single passage, and the reason is that the register "
        "of the English has to carry weight that the original carries by "
        "other means entirely. "
    )
    prose_after = (
        " The committee reviewed that rendering again over several months of "
        "careful comparison with the published editions they had on hand, and "
        "they eventually settled on the wording above."
    )
    doc = prose_before + verse + prose_after

    # Position the verse the way report.Finding would: word indices.
    start_word = len(prose_before.split())
    end_word = start_word + len(verse.split())
    spans = []
    offsets = []
    pos = 0
    for w in doc.split():
        i = doc.index(w, pos)
        offsets.append((i, i + len(w)))
        pos = i + len(w)
    spans.append((offsets[start_word][0], offsets[end_word - 1][1]))

    rep = adapt_document(doc, CURIOUS, quoted_spans=spans)

    assert rep.frozen_tokens_verified, "document-level byte check failed"
    assert verse in rep.adapted_text, "the verse did not survive intact"
    assert verse.encode("utf-8") in rep.adapted_text.encode("utf-8"), \
        "the verse survived as a str but not byte-for-byte"

    quoted = [s for s in rep.sentences if s.kind == QUOTED]
    assert quoted, "the verse's sentence was not classified QUOTED"
    for s in quoted:
        assert s.disposition == REFUSED
        assert R_SCRIPTURE in s.refusal_reasons
        assert s.adapted is None, "a QUOTED sentence was adapted"
        assert s.output == s.original


def test_quote_marked_text_is_refused_even_without_findings():
    """
    Quote marks alone are enough. A publisher may hand us a translation with
    no Arabic side, so detection may produce no spans; the mark itself must
    still protect the text.
    """
    for opener, closer in [('"', '"'), ("“", "”"),
                           ("«", "»"), ("﴾", "﴿")]:
        sent = (f"The passage reads {opener}whatever words happen to sit "
                f"between the marks here{closer} in that edition of the work.")
        reasons = refusal_reasons(sent)
        assert R_SCRIPTURE in reasons, f"{opener}..{closer} not protected"
        out = adapt_sentence(sent, CURIOUS)
        assert out.disposition == REFUSED
        assert out.output == sent


# ======================================================================
# 2. Normative language is refused, with the right reason
# ======================================================================

def test_normative_sentence_is_refused_with_normative_reason():
    cases = [
        "A traveller must repeat the prayer once the journey has ended.",
        "It is forbidden to publish a translation without naming the edition.",
        "Fasting in that month is obligatory for every adult of sound mind.",
        "One should always check the printed reference before going to press.",
        "This action is haram according to the plain sense of the text.",
        "يجب على الناشر أن يذكر اسم الترجمة المعتمدة في مقدمة الكتاب.",
        "لا يجوز نشر ترجمة دون بيان الطبعة التي اعتمد عليها المترجم.",
    ]
    for sent in cases:
        reasons = refusal_reasons(sent)
        assert R_NORMATIVE in reasons, f"not refused as normative: {sent!r}"
        out = adapt_sentence(sent, CURIOUS)
        assert out.disposition == REFUSED
        assert out.adapted is None
        assert out.output == sent, "a refused sentence was modified"


def test_implicit_obligation_is_refused():
    """
    The subtle one. No modal, reads as description, functions as a ruling.
    Rewriting it ("Muslims often pray...") would silently downgrade a ruling,
    which is exactly the substance change Track 2 forbids.
    """
    cases = [
        "A Muslim prays five times a day.",
        "A Muslim fasts the whole of that month each year.",
        "Every believer gives a portion of their wealth each year.",
        "Muslims face the Kaaba during the prayer.",
    ]
    for sent in cases:
        reasons = refusal_reasons(sent)
        assert R_IMPLICIT_OBLIGATION in reasons, \
            f"implicit obligation missed: {sent!r}"
        out = adapt_sentence(sent, CURIOUS)
        assert out.disposition == REFUSED
        assert out.output == sent


def test_past_tense_narration_is_not_mistaken_for_an_obligation():
    """Guard the other direction: history is not a ruling."""
    sent = ("Muslims in that city were praying behind a single imam for many "
            "years before the second mosque was finally built nearby.")
    assert R_IMPLICIT_OBLIGATION not in refusal_reasons(sent)


def test_preference_claims_are_refused():
    for sent in ["It is better to recite slowly than to hurry through it.",
                 "The best of these editions is the one printed most recently.",
                 "Reciting at dawn is more virtuous than reciting at night.",
                 "الأفضل أن تقرأ ببطء وتدبر في أول النهار قبل انشغال الناس."]:
        assert R_PREFERENCE in refusal_reasons(sent), f"missed: {sent!r}"


def test_scholarly_attribution_is_refused():
    for sent in ["According to the majority, the second reading is adopted.",
                 "Scholars differ on how much explanation a translator may add.",
                 "The Hanafi position on this point is recorded in that volume.",
                 "جمهور العلماء على خلاف ما ذكره المترجم في الحاشية الثانية."]:
        assert R_ATTRIBUTION in refusal_reasons(sent), f"missed: {sent!r}"


def test_citations_are_refused():
    for sent in ["The point is made plainly in Quran 51:56 and again later.",
                 "This was narrated by al-Bukhari in the chapter on intention.",
                 "The editors placed a bracketed reference [2:255] in the text.",
                 "رواه مسلم في صحيحه من حديث أبي هريرة رضي الله عنه."]:
        assert R_REFERENCE in refusal_reasons(sent), f"missed: {sent!r}"


def test_creed_statements_are_refused():
    for sent in ["Whoever denies this point has left the fold of the religion.",
                 "The five pillars are taught in the opening chapter of it.",
                 "من جحد هذا الأصل فقد وقع في الكفر عند أهل العلم جميعا."]:
        assert R_CREED in refusal_reasons(sent), f"missed: {sent!r}"


def test_all_refusal_reasons_are_reported_not_just_the_first():
    """
    Refusing for three independent reasons is stronger evidence of
    conservatism than refusing for one, so the gate must not short-circuit.
    """
    sent = ("According to the majority it is better to recite slowly, and "
            "the Quran 2:255 passage is cited as proof of that view.")
    reasons = set(refusal_reasons(sent))
    assert {R_ATTRIBUTION, R_PREFERENCE, R_REFERENCE} <= reasons, reasons


# ======================================================================
# 3. Plain narrative prose IS eligible
# ======================================================================

def test_plain_narrative_sentence_is_eligible():
    cases = [
        "The committee reviewed the manuscript over several months.",
        "Many readers first meet the text in a language they already speak.",
        "The printing house in that city produced three editions that decade.",
        "Her notes on the margin were collected and published much later on.",
    ]
    for sent in cases:
        assert refusal_reasons(sent) == [], \
            f"false refusal {refusal_reasons(sent)} on: {sent!r}"
        out = adapt_sentence(sent, PRACTISING)
        assert out.disposition == ELIGIBLE, sent
        assert out.kind == PROSE


def test_a_level_cannot_unlock_a_refused_sentence():
    """
    Structural guarantee: levels change HOW MUCH is flagged, never WHETHER a
    sentence may be touched. `refusal_reasons` takes no level, so there is no
    code path that could; this test pins that down against future edits.
    """
    sent = "A pilgrim must complete the circuits before leaving the area."
    for level in LEVELS:
        out = adapt_sentence(sent, level)
        assert out.disposition == REFUSED, level
        assert R_NORMATIVE in out.refusal_reasons
        assert out.output == sent


# ======================================================================
# 4. Frozen-token invariant and rollback
# ======================================================================

def test_freeze_and_thaw_roundtrip_is_identity():
    sent = ("The translator explained Tawhid and Sharia to the group before "
            "turning to the question of how Sunnah is rendered in print.")
    masked, spans = freeze(sent)
    assert len(spans) == 3, [s.text for s in spans]
    assert "Tawhid" not in masked and "Sharia" not in masked
    assert thaw(masked, spans) == sent
    assert verify_restoration(sent, thaw(masked, spans), spans)


def test_thaw_raises_when_a_frozen_token_is_dropped():
    sent = "The translator explained Tawhid to the group that same afternoon."
    masked, spans = freeze(sent)
    mangled = masked.replace(spans[0].token, "")        # adapter ate the term
    try:
        thaw(mangled, spans)
    except FrozenTokenViolation as e:
        assert "dropped" in str(e)
    else:
        raise AssertionError("dropping a frozen token did not raise")


def test_thaw_raises_when_a_frozen_token_is_duplicated():
    sent = "The translator explained Tawhid to the group that same afternoon."
    masked, spans = freeze(sent)
    mangled = masked + " " + spans[0].token
    try:
        thaw(mangled, spans)
    except FrozenTokenViolation as e:
        assert "duplicated" in str(e)
    else:
        raise AssertionError("duplicating a frozen token did not raise")


def test_thaw_raises_on_an_unknown_token():
    sent = "The translator explained Tawhid to the group that same afternoon."
    masked, spans = freeze(sent)
    try:
        thaw(masked + " ␂MZN99␃", spans)
    except FrozenTokenViolation as e:
        assert "unknown" in str(e)
    else:
        raise AssertionError("an invented frozen token did not raise")


def test_frozen_violation_forces_a_full_rollback(monkeypatch=None):
    """
    The product's safety guarantee, end to end.

    A deliberately misbehaving adapter is installed in place of `model_adapt`.
    It returns plausible-looking text that has quietly eaten the frozen term.
    The expected outcome is not a repair and not a partial accept: the whole
    sentence is discarded and the ORIGINAL is returned, flagged R_ROLLBACK.
    """
    import mizan.clarity as clarity

    sent = ("The translator explained Tawhid at some length to a room full of "
            "visitors who had never encountered the subject before that day.")
    original_fn = clarity.model_adapt

    def eats_the_frozen_term(sentence: str, level: str) -> str:
        # Strip every frozen token: the single worst thing an adapter can do.
        return clarity._FROZEN_RE.sub("the concept", sentence)

    clarity.model_adapt = eats_the_frozen_term
    try:
        out = clarity.adapt_sentence(sent, CURIOUS, use_model=True)
    finally:
        clarity.model_adapt = original_fn

    assert out.disposition == REFUSED, out.disposition
    assert out.refusal_reasons == [R_ROLLBACK], out.refusal_reasons
    assert out.adapted is None, "unverified text was kept"
    assert out.output == sent, "rollback did not return the original"
    assert "Tawhid" in out.output


def test_rollback_also_fires_when_adaptation_introduces_a_ruling():
    """
    Second rollback trigger: the adapter kept every token but turned a
    description into an obligation. Catching this is what stops a future model
    from "helpfully" authoring a ruling.
    """
    import mizan.clarity as clarity

    sent = ("The congregation gathered early that morning in the courtyard "
            "outside the old building before the doors were opened for them.")
    original_fn = clarity.model_adapt

    def invents_a_ruling(sentence: str, level: str) -> str:
        return sentence + " This is obligatory."

    clarity.model_adapt = invents_a_ruling
    try:
        out = clarity.adapt_sentence(sent, CURIOUS, use_model=True)
    finally:
        clarity.model_adapt = original_fn

    assert out.disposition == REFUSED
    assert out.refusal_reasons == [R_ROLLBACK]
    assert out.output == sent


def test_model_hook_raises_not_implemented_and_falls_back_cleanly():
    """The hook is a documented seam, not a silent no-op."""
    try:
        model_adapt("anything at all", PRACTISING)
    except NotImplementedError as e:
        assert "contract" in str(e).lower()
    else:
        raise AssertionError("model_adapt should raise NotImplementedError")

    # use_model=True must still produce a valid result via the deterministic
    # path — this is the documented offline fallback mode.
    long_sent = ("Many people first encounter the text through a translation, "
                 "and because the translator's choices shape what the reader "
                 "takes away, the published edition chosen matters greatly.")
    out = adapt_sentence(long_sent, CURIOUS, use_model=True)
    assert out.disposition == ELIGIBLE
    assert out.adapted is not None


# ======================================================================
# 5. Readability genuinely improves
# ======================================================================

def test_readability_improves_on_a_long_run_on_sentence():
    sent = ("Many people first encounter the Quran through a translation, and "
            "because the translator's choices shape what the reader "
            "understands, it matters a great deal which published edition a "
            "publisher reaches for when preparing material.")
    out = adapt_sentence(sent, CURIOUS)

    assert out.disposition == ELIGIBLE
    assert out.adapted is not None, "the run-on was not split"
    assert out.readability_after.flesch > out.readability_before.flesch, (
        f"{out.readability_before.flesch} -> {out.readability_after.flesch}")
    assert (out.readability_after.words_per_sentence
            < out.readability_before.words_per_sentence)
    assert out.readability_after.n_sentences > 1
    assert any(s.code == SUG_SPLIT and s.applied for s in out.suggestions)


def test_splitting_never_adds_or_removes_a_content_word():
    """
    The split is only safe because it provably cannot change content. Compare
    the multiset of words before and after, ignoring the one period and the
    one capitalisation the transform is allowed to introduce.
    """
    from collections import Counter
    sent = ("The committee met in the spring of that year to review the "
            "manuscript, and they returned to it again in the autumn after "
            "the printer had sent his first proofs back to them.")
    out = adapt_sentence(sent, CURIOUS)
    assert out.adapted is not None

    def bag(t):
        return Counter(w.strip(".,;:").lower() for w in t.split())

    assert bag(out.original) == bag(out.adapted), (
        bag(out.original) - bag(out.adapted),
        bag(out.adapted) - bag(out.original))


def test_short_sentences_are_left_alone_at_every_level():
    sent = "The committee met in the spring of that year."
    for level in LEVELS:
        out = adapt_sentence(sent, level)
        assert out.adapted is None, f"{level} split a short sentence"
        assert out.output == sent


def test_level_thresholds_are_ordered_and_change_behaviour():
    """A longer sentence survives at daee but is split at curious."""
    sent = ("The committee met in the spring of that year to review the "
            "manuscript, and they came back to the question in the autumn.")
    curious = adapt_sentence(sent, CURIOUS)
    daee = adapt_sentence(sent, DAEE)

    assert profile(CURIOUS).max_words < profile(NEW_MUSLIM).max_words \
        < profile(PRACTISING).max_words < profile(DAEE).max_words
    assert curious.adapted is not None, "curious should split this"
    assert daee.adapted is None, "daee should leave this alone"
    # ...but both make exactly the same claim.
    assert curious.disposition == daee.disposition == ELIGIBLE


def test_readability_reports_no_flesch_for_arabic():
    """Printing an English syllable score for Arabic would be a made-up number."""
    r = readability("هذه فقرة عربية قصيرة كتبها المحرر لاختبار القياس فقط.")
    assert r.flesch is None
    assert r.n_words > 0 and r.words_per_sentence > 0


# ======================================================================
# 6. Glossary and suggestions
# ======================================================================

def test_approved_terms_are_frozen_and_flagged_not_replaced():
    sent = ("The speaker opened by explaining Tawhid to a room of visitors "
            "who had come in from the surrounding towns that afternoon.")
    out = adapt_sentence(sent, CURIOUS)
    assert "Tawhid" in out.output, "an approved term was altered"
    gloss = [s for s in out.suggestions if s.code == SUG_GLOSS]
    assert gloss, "no gloss suggestion for a new audience"
    assert "Oneness of God" in gloss[0].detail
    assert not gloss[0].applied, "a gloss must be suggested, never applied"


def test_glossary_terms_are_not_flagged_for_an_expert_audience():
    sent = ("The speaker opened by explaining Tawhid to a room of visitors "
            "who had come in from the surrounding towns that afternoon.")
    out = adapt_sentence(sent, DAEE)
    assert not [s for s in out.suggestions if s.code == SUG_GLOSS]
    assert "Tawhid" in out.output


def test_a_term_is_glossed_only_on_first_use_across_a_document():
    doc = ("The speaker explained Tawhid to the visitors in the morning. "
           "Later that day another speaker returned to Tawhid once more. "
           "A third session touched on Tawhid briefly near the very end.")
    rep = adapt_document(doc, CURIOUS)
    gloss = [s for sent in rep.sentences for s in sent.suggestions
             if s.code == SUG_GLOSS]
    assert len(gloss) == 1, f"glossed {len(gloss)} times, expected once"


def test_passive_voice_is_flagged_but_never_rewritten():
    sent = ("The manuscript was reviewed by the committee during the long "
            "winter that followed the first printing of the earlier edition.")
    out = adapt_sentence(sent, CURIOUS)
    passives = [s for s in out.suggestions if s.code == SUG_PASSIVE]
    assert passives, "passive construction not flagged"
    assert not passives[0].applied, "a passive must be flagged, not rewritten"
    assert "was reviewed" in out.output


def test_every_glossary_entry_carries_its_package_constraint():
    """The constraint IS the reason the term is frozen; none may be blank."""
    for t in GLOSSARY:
        assert t.constraint.strip(), t.term
        assert t.gloss.strip(), t.term
        assert t.arabic.strip(), t.term


# ======================================================================
# 6b. Glossary loaded from data/glossary (الجمهرة) — Panel 2 feeding Panel 3
# ======================================================================

def test_package_terms_come_first_and_keep_their_constraints():
    """
    Whatever data/glossary adds, the package's ten entries are first and own
    their surface forms: a dictionary record can never overwrite the
    package's ضابط الاستخدام for "Sharia".
    """
    pkg = _clarity._PACKAGE_GLOSSARY
    assert len(pkg) == 10
    assert GLOSSARY[:10] == pkg
    for t in pkg:
        assert _clarity._GLOSSARY_BY_KEY[t.term.lower()] is t, t.term
    assert "package" in _clarity.GLOSSARY_SOURCE


def test_alternative_spelling_of_a_package_term_carries_the_package_rule():
    """
    "Tauhid" is an accepted spelling of التوحيد. It must be frozen like
    "Tawhid" and glossed with the PACKAGE's rendering and constraint.
    """
    sent = ("The speaker opened by explaining Tauhid to a room of visitors "
            "who had come in from the surrounding towns that afternoon.")
    out = adapt_sentence(sent, CURIOUS)
    assert "Tauhid" in out.frozen, out.frozen
    gloss = [s for s in out.suggestions if s.code == SUG_GLOSS]
    assert gloss and "Oneness of God" in gloss[0].detail, gloss
    assert "numerical oneness" in gloss[0].detail


def test_plain_english_equivalents_are_not_frozen_or_glossed():
    """
    الجمهرة renders النار as "Fire" and الإيمان as "Faith". Those words have
    everyday senses; freezing or glossing them as shar'i terms would be wrong.
    """
    sent = ("The fire spread quickly through the old market that night, and "
            "the traders kept their faith in the town council afterwards.")
    out = adapt_sentence(sent, CURIOUS)
    assert out.disposition == ELIGIBLE, out.refusal_reasons
    assert not [s for s in out.suggestions if s.code == SUG_GLOSS], \
        [s.detail for s in out.suggestions]
    assert out.frozen == [], out.frozen


def test_two_spellings_of_one_term_are_glossed_once():
    doc = ("The speaker explained Tawhid to the visitors in the morning. "
           "Later that day another speaker returned to Tauhid once more.")
    rep = adapt_document(doc, CURIOUS)
    gloss = [s for sent in rep.sentences for s in sent.suggestions
             if s.code == SUG_GLOSS]
    assert len(gloss) == 1, [g.span for g in gloss]


def test_frozen_transliterations_survive_a_split_byte_exact():
    sent = ("The volunteers sorted the Da'wah leaflets in the back room of the "
            "centre during the long afternoon, and they finished the job well "
            "before the evening class on Shari'ah began for the guests.")
    out = adapt_sentence(sent, CURIOUS)
    assert out.disposition == ELIGIBLE, out.refusal_reasons
    assert out.adapted is not None, "expected a split at ', and'"
    for term in ("Da'wah", "Shari'ah"):
        assert term in out.frozen, (term, out.frozen)
        assert out.adapted.encode("utf-8").count(term.encode("utf-8")) == 1


def test_apostrophes_inside_transliterations_are_not_quote_marks():
    """
    "Qur'an" and "Da'wah" each contain a straight apostrophe. Two of them in
    one sentence are not an opening and a closing quotation mark.
    """
    sent = ("The Qur'an study circle and the Da'wah table were both set up "
            "near the entrance of the hall that weekend.")
    assert R_SCRIPTURE not in refusal_reasons(sent), refusal_reasons(sent)
    # A real quotation next to such a word is still caught.
    quoted = ("The Qur'an circle read 'whatever words sit between the marks' "
              "aloud that weekend.")
    assert R_SCRIPTURE in refusal_reasons(quoted)


def test_glossary_degrades_to_the_package_ten_if_the_data_is_unreadable():
    """
    A broken data/glossary must never yield an EMPTY glossary — that would
    silently stop freezing shar'i terms. It must fall back to the ten.
    """
    from mizan import terms as _terms
    real = _terms.clarity_entries

    def _boom(*a, **k):
        raise OSError("simulated unreadable data/glossary")

    _terms.clarity_entries = _boom
    try:
        glossary, source = _clarity._load_glossary()
    finally:
        _terms.clarity_entries = real
    assert glossary == _clarity._PACKAGE_GLOSSARY
    assert source == "package (hardcoded)"


def test_every_frozen_surface_is_usable():
    """No empty, one-letter or duplicate-key surfaces in the frozen regex."""
    keys = [t.term.lower() for t in GLOSSARY]
    assert all(len(k) >= 3 for k in keys), [k for k in keys if len(k) < 3]
    assert len(set(keys)) == len(keys), "duplicate surface forms"


# ======================================================================
# 7. Segmentation
# ======================================================================

def test_abbreviations_do_not_split_a_sentence():
    text = "The editor, Dr. Ahmad, reviewed vol. 2 of the series last year."
    assert len(split_sentences(text)) == 1, split_sentences(text)


def test_segment_marks_partial_overlap_as_quoted():
    """
    The dangerous case: half a verse inside an otherwise ordinary sentence.
    Overlap, not containment, is the test.
    """
    text = "He said that ALPHA BETA GAMMA and then he continued speaking."
    start = text.index("ALPHA")
    end = text.index("GAMMA") + len("GAMMA")
    segs = segment(text, [(start, end)])
    assert len(segs) == 1
    assert segs[0][1] == QUOTED, segs


def test_empty_and_whitespace_documents_do_not_crash():
    for text in ["", "   ", "\n\n"]:
        rep = adapt_document(text, PRACTISING)
        assert rep.sentences == []
        assert rep.refusal_rate == 0.0
        assert rep.frozen_tokens_verified


def test_unknown_level_fails_loudly():
    try:
        profile("beginner")
    except ValueError as e:
        assert "beginner" in str(e)
    else:
        raise AssertionError("an unknown level should raise ValueError")


# ======================================================================
# 8. Document-level report: refusal rate and JSON
# ======================================================================

# A realistic mixed sample: the kind of paragraph a da'wah publisher actually
# sends out. Deliberately contains normative, preference, attribution,
# citation and narrative sentences in roughly the proportions real material
# does, so the measured refusal rate means something.
SAMPLE = (
    "Many people first encounter the Quran through a translation, and because "
    "the translator's choices shape what the reader understands, it matters a "
    "great deal which published edition a publisher reaches for. "
    "The committee that prepared this booklet met over several months. "
    "A Muslim prays five times a day. "
    "It is better to recite slowly and with attention than to hurry. "
    "Scholars differ on how much explanation a translator may add in a note. "
    "The point is stated plainly in Quran 51:56. "
    "Printing houses in that region produced three separate editions that "
    "decade, and each of them carried a different set of footnotes at the "
    "back of the volume. "
    "One should always check the reference before sending anything to press. "
    "The speaker closed by explaining Tawhid to the visitors."
)


def test_refusal_rate_is_reported_and_is_substantial():
    rep = adapt_document(SAMPLE, CURIOUS)

    assert isinstance(rep, ClarityReport)
    assert rep.n_prose > 0
    assert rep.n_refused + rep.n_eligible == rep.n_prose
    assert 0.0 < rep.refusal_rate < 1.0, rep.refusal_rate
    # On realistic da'wah prose the gate should be refusing a large minority.
    assert rep.refusal_rate >= 0.3, (
        f"refusal rate {rep.refusal_rate:.0%} is suspiciously permissive")
    assert rep.refusal_counts, "refusals were counted but not explained"
    assert "declined to adapt" in rep.summary
    assert f"{rep.refusal_rate:.0%}" in rep.summary


def test_every_refusal_carries_a_human_readable_reason():
    rep = adapt_document(SAMPLE, CURIOUS)
    for s in rep.sentences:
        if s.disposition != REFUSED:
            continue
        assert s.refusal_reasons, f"refused with no reason: {s.original!r}"
        for item in s.as_dict()["refusal_reasons"]:
            assert item["message"] and item["message"] != item["code"], item


def test_report_is_json_serialisable():
    rep = adapt_document(SAMPLE, NEW_MUSLIM)
    blob = json.dumps(rep.as_dict(), ensure_ascii=False)
    back = json.loads(blob)

    assert back["level"] == NEW_MUSLIM
    assert back["counts"]["sentences"] == len(rep.sentences)
    assert isinstance(back["refusal_rate"], float)
    assert back["frozen"]["verified_byte_exact"] is True
    assert isinstance(back["suggestions"], list)
    assert len(back["sentences"]) == len(rep.sentences)
    for s in back["sentences"]:
        assert {"index", "original", "kind", "disposition", "output"} <= set(s)


def test_refused_sentences_are_reproduced_verbatim_in_the_output():
    """
    The whole-document check: every sentence Mizan declined must appear in the
    output exactly as it went in. No stray capitalisation, no lost comma.
    """
    rep = adapt_document(SAMPLE, CURIOUS)
    out = rep.adapted_text
    for s in rep.sentences:
        if s.disposition == REFUSED:
            assert s.original in out, f"refused text altered: {s.original!r}"


def test_document_readability_does_not_regress():
    rep = adapt_document(SAMPLE, CURIOUS)
    assert rep.readability_before.flesch is not None
    assert rep.readability_after.flesch >= rep.readability_before.flesch, (
        rep.readability_before.flesch, rep.readability_after.flesch)


def test_all_four_levels_run_and_never_disagree_on_substance():
    """
    Levels may differ in how much they split and flag. They must never differ
    on which sentences are REFUSED — that would mean an audience setting could
    unlock a ruling.
    """
    refused_by_level = {}
    for level in LEVELS:
        rep = adapt_document(SAMPLE, level)
        refused_by_level[level] = {
            s.index for s in rep.sentences if s.disposition == REFUSED}
    sets = list(refused_by_level.values())
    assert all(s == sets[0] for s in sets), refused_by_level


# ======================================================================
# 9. Regressions from the 2026-10-04 review
#
# Each test below failed on the version before the review (reproduced first,
# see docs/CLARITY.md §11). The sentences are written for these tests; verse
# text is read from the index at run time, never pasted here.
# ======================================================================

def _refused_untouched(sent: str, level: str = CURIOUS):
    out = adapt_sentence(sent, level)
    assert out.disposition == REFUSED, (sent, out.refusal_reasons)
    assert out.adapted is None, (sent, out.adapted)
    assert out.output == sent
    return out


def test_review_examples_are_refused_and_never_split():
    """
    The four sentences the review gave, plus its conditional one. Before the
    fix each passed `refusal_reasons() == []` and four of them were split at
    `curious` ("...threshold for a full year. And it is paid...").
    """
    cases = {
        "Zakat is compulsory for every adult who owns wealth above the "
        "threshold for a full year, and it is paid to the eight categories "
        "named in the law.": R_NORMATIVE,
        "Jesus was not crucified, and he was raised up to Allah, as the "
        "Quran states clearly for those who reflect on it.": R_CREED,
        "There is no god worthy of worship except Allah alone, and Muhammad "
        "is His final messenger to all of mankind.": R_CREED,
        "Women should wear loose clothing when they go out in public, and "
        "they avoid perfume that others can smell.": R_NORMATIVE,
        "If a woman is menstruating, prayer is not performed, and the missed "
        "fasts are made up after Ramadan ends.": R_CONDITIONAL,
    }
    for sent, code in cases.items():
        reasons = refusal_reasons(sent)
        assert code in reasons, (sent, reasons)
        for level in LEVELS:
            _refused_untouched(sent, level)


def test_ruling_vocabulary_is_refused_as_normative():
    """Bare ruling words, whatever the subject, with no fixed phrase needed."""
    cases = [
        "Fasting in that month is compulsory for every healthy adult at home.",
        "Paying the levy is mandatory for every trader in the old market.",
        "Selling that drink is unlawful for every merchant in the city.",
        "Eating that meat is lawful for the travellers on the road north.",
        "Backbiting is a sin that ruins the trust between close neighbours.",
        "Those sins are wiped away by sincere repentance before death comes.",
        "Hoarding food during a famine is sinful in the eyes of the scholars.",
        "A seller may not sell goods that he does not yet own at all.",
        "Combining the two prayers is permissible for a traveller on the road.",
        "Charging interest on a loan is prohibited for every lender in town.",
        "Washing before the prayer is required for anyone who has slept.",
        "Music is not allowed in the hall during the evening lesson there.",
        "Speaking during the sermon is allowed only for the man who leads it.",
        "The second payment is obligatory once the first has been made.",
        "Visitors should remove their shoes at the door of the prayer hall.",
        "The guests needs to be served before the hosts sit down to eat.",
    ]
    for sent in cases:
        assert R_NORMATIVE in refusal_reasons(sent), sent
        _refused_untouched(sent)


def test_creed_statements_beyond_fixed_phrases_are_refused():
    """A sentence ABOUT God, prophethood, revelation or the unseen is creed."""
    cases = [
        "There is no deity worthy of worship besides the One who created all.",
        "God hears every whisper and knows what the hearts conceal at night.",
        "The angels record every deed, small or large, in a written book.",
        "Paradise is a reward for those who believed and did righteous deeds.",
        "The final prophet was sent to the whole of mankind in that age.",
        "Every soul will taste death before the Day of Judgment arrives.",
        "The guests were told that all existence depends on Him alone now.",
        "The old man warned his sons that lying leads to the Fire in the end.",
        "Many Christians believe in the Trinity and in the crucifixion story.",
    ]
    for sent in cases:
        assert R_CREED in refusal_reasons(sent), sent
        _refused_untouched(sent)
    # The case-sensitive names of the unseen do not fire on ordinary prose.
    assert R_CREED not in lexical_reasons(
        "The fire spread quickly through the old market that night.")


def test_implicit_obligation_covers_family_and_rite_roles():
    cases = [
        "Women cover their hair in front of men who are not close relatives.",
        "Men lower their gaze in the crowded market among the stalls there.",
        "Parents teach their children the prayer from the age of seven years.",
        "A husband provides for his wife and children from his own earnings.",
        "Pilgrims shave their heads after the final rite of the journey.",
    ]
    for sent in cases:
        assert R_IMPLICIT_OBLIGATION in refusal_reasons(sent), sent
        _refused_untouched(sent)


def test_conditional_sentences_are_refused_and_never_split():
    """
    "If A, B, and C": splitting at ", and" would move C out of the "if" and
    state it unconditionally. The gate refuses every shape of governing
    condition the review listed.
    """
    cases = [
        "If the traveller arrives late, the meal is served cold, and the "
        "guests leave before the evening ends in the hall.",
        "When the rain stops, the market opens again on the square, and the "
        "traders set out their goods for the whole afternoon.",
        "Unless the river floods, the ferry runs every hour, and the "
        "passengers pay the fare at the small wooden kiosk.",
        "Whoever arrives first opens the hall for the others, and the keys "
        "are returned to the caretaker before sunset that day.",
        "The fee is waived for the students, and the books are lent free of "
        "charge, provided that they are returned within a month.",
        "The fee is waived as long as the form arrives on time, and the "
        "office stamps it before the end of the working week.",
        "Nobody enters the archive except the two clerks on duty, and they "
        "sign the register at the door every single morning.",
    ]
    for sent in cases:
        assert R_CONDITIONAL in refusal_reasons(sent), sent
        _refused_untouched(sent)
    # Defence in depth: the split itself refuses a governed sentence, even
    # for a caller that skips the gate.
    text, n = _clarity._split_at_conjunctions(cases[0], profile(CURIOUS))
    assert n == 1 and text == cases[0]
    # A condition inside the LAST clause, with no comma before it, governs
    # only that clause; the sentence may still be split.
    tail = ("The committee met in the spring of that year to review the "
            "manuscript, and they returned to the question when the printer "
            "had finished the first proofs.")
    assert R_CONDITIONAL not in refusal_reasons(tail)
    assert adapt_sentence(tail, CURIOUS).adapted is not None


def _verse_runs_only_the_guard_catches(n: int = 3):
    """
    Runs of 22 words from long verses of an approved edition, read from the
    index, embedded in neutral prose with no quote marks and no citation,
    that pass the LEXICAL gate and that the deterministic split would cut.
    Without the scripture guard each would be split inside approved wording.
    """
    m = _mizan()
    if m is None:
        return None
    book = next((b["book_id"] for b in m.books.values()
                 if "pickthall" in (b["title"] or "").lower()), None)
    if book is None:
        return None
    prof = profile(CURIOUS)
    out = []
    for ayah in range(1, 287):
        words = (m.verse(book, 2, ayah) or "").split()
        if len(words) < 30:
            continue
        for st in range(0, len(words) - 22, 2):
            run = " ".join(words[st:st + 22]).strip(" ,;:")
            if any(c in run for c in '.?!"“”()[]'):
                continue
            sent = ("The teacher closed the old notebook and told the class "
                    "that " + run[0].lower() + run[1:]
                    + " before the lesson came to an end.")
            if lexical_reasons(sent):
                continue
            if _clarity._split_at_conjunctions(sent, prof)[1] < 2:
                continue
            out.append((sent, run[0].lower() + run[1:]))
            break
        if len(out) >= n:
            break
    return out


def test_an_undetected_verse_run_is_refused_as_scripture_like():
    """
    A verbatim run from an approved translation that the detector did not
    locate (no findings passed) is not frozen. The scripture guard must
    refuse its sentence, so no ". And" is ever inserted inside approved
    wording. Before the fix every one of these was split.
    """
    runs = _verse_runs_only_the_guard_catches()
    if runs is None:
        print("      SKIP: corpus not available")
        return
    assert runs, "expected verse runs that pass the lexical gate"
    for sent, run in runs:
        assert scripture_reasons(sent) == [R_SCRIPTURE_LIKE], sent
        out = _refused_untouched(sent)
        assert R_SCRIPTURE_LIKE in out.refusal_reasons
        doc = ("The visitors arrived early that morning. " + sent
               + " Then they walked back to the station together.")
        rep = adapt_document(doc, CURIOUS)               # no findings at all
        assert run in rep.adapted_text, "approved wording was altered"
        assert rep.frozen_tokens_verified


def test_ordinary_prose_passes_the_scripture_guard():
    """The guard's false-positive direction, on everyday narrative prose."""
    if _mizan() is None:
        print("      SKIP: corpus not available")
        return
    for sent in [
        "The committee reviewed the manuscript over several months.",
        "The printing house in that city produced three editions that decade.",
        "The volunteers sorted the leaflets in the back room of the centre "
        "during the long afternoon, and they finished well before the class.",
        "Rain fell on the harbour for most of the week, and the fishing boats "
        "stayed tied up along the stone quay until the weekend.",
    ]:
        assert scripture_reasons(sent) == [], (sent, _clarity.scripture_likeness(sent))


def test_scripture_guard_fails_safe_without_an_index():
    """
    No index must never mean "nothing looks like scripture". Every prose
    sentence is refused with SCRIPTURE_UNCHECKED and nothing is adapted.
    """
    sent = ("The committee met in the spring of that year to review the "
            "manuscript, and they returned to it again in the autumn after "
            "the printer had sent his first proofs back to them.")
    real = _clarity._guard_detector
    _clarity._guard_detector = lambda lang: None
    try:
        assert R_SCRIPTURE_UNCHECKED in refusal_reasons(sent)
        _refused_untouched(sent)
        rep = adapt_document(sent + " " + sent, CURIOUS)
        assert rep.n_adapted == 0 and rep.n_refused == rep.n_prose == 2
        assert rep.as_dict()["scripture_guard"]["available"] is False
    finally:
        _clarity._guard_detector = real
    # A language with no approved translation indexed fails the same way,
    # with no monkeypatching: the detector cannot be built for it.
    assert R_SCRIPTURE_UNCHECKED in refusal_reasons(sent, lang="zz")
    assert adapt_sentence(sent, CURIOUS, lang="zz").adapted is None


def test_document_whitespace_survives_and_byte_check_is_whole_document():
    """
    A two-sentence quotation with a line break and a double space inside it,
    between prose separated by a paragraph break. The first version rebuilt
    the output with " ".join(...): the line break became a space while the
    report still said verified_byte_exact=True.
    """
    q = "ALPHA BETA GAMMA DELTA ends here.\nEPSILON ZETA  ETA THETA ends there."
    doc = ("The editor opened the volume to the page in question.\n\n" + q
           + "  Then the committee closed the meeting for the day.\n")
    s = doc.index(q)
    rep = adapt_document(doc, CURIOUS, quoted_spans=[(s, s + len(q))])
    assert rep.adapted_text == doc, repr(rep.adapted_text)
    assert rep.as_dict()["adapted_text"] == doc
    assert rep.frozen_tokens_verified

    # The verifier itself compares WHOLE spans against the final text: the
    # collapsed output that the old per-piece check accepted is rejected.
    collapsed = doc.replace(".\nEPSILON", ". EPSILON")
    pieces = [FrozenSpan(token="", text=t, kind="quotation")
              for t in q.split("\n")]
    assert verify_restoration(doc, collapsed, pieces), "old check accepted it"
    whole = document_frozen_spans(doc, [(s, s + len(q))], [])
    assert not verify_document(doc, collapsed, whole)


def test_a_split_changes_only_the_comma_and_the_capital():
    """Line breaks and double spaces inside an adapted sentence are kept."""
    sent = ("The committee met in the spring of that year  to review the\n"
            "manuscript, and they returned to it again in the autumn after "
            "the printer had sent his first proofs back to them.")
    out = adapt_sentence(sent, CURIOUS)
    assert out.adapted == sent.replace("manuscript, and", "manuscript. And"), \
        repr(out.adapted)
    doc = "First line of the notes.\n\n" + sent + "\n\nLast line of the notes."
    rep = adapt_document(doc, CURIOUS)
    assert rep.adapted_text == doc.replace("manuscript, and", "manuscript. And")


def test_sentences_inside_a_multi_sentence_quotation_are_refused():
    """
    The middle of `said: "One. Two. Three."` carries no quote mark of its own;
    it is still someone else's words.
    """
    doc = ('The old caretaker said: "The doors open at dawn every single day '
           'of the week. The keys hang on the hook behind the long desk in the '
           'hall. Nobody takes them home at night." Then he went back inside.')
    regions = quotation_regions(doc)
    assert len(regions) == 1
    rep = adapt_document(doc, CURIOUS)
    middle = [s for s in rep.sentences if s.original.startswith("The keys")]
    assert middle and R_SCRIPTURE in middle[0].refusal_reasons
    # An unclosed mark stops at the paragraph break, not at the end of text.
    assert quotation_regions('He said: "unclosed words\n\nNext paragraph.') == [
        (9, 24)]


# ----------------------------------------------------------------------
# Languages the gate cannot read (coordinator's probe, 2026-10-04)
# ----------------------------------------------------------------------

# Neutral sentences written for these tests: a city's markets, no religion.
_NEUTRAL = {
    "hi": "उन वर्षों में मदीना शहर तेज़ी से बढ़ा, और यमन और सीरिया से बहुत से "
          "व्यापारी उसके बाज़ारों में कपड़ा, खजूर और मसाले बेचने आए जो वहाँ रहने "
          "वाले परिवारों को बेचे जाते थे।",
    "ur": "اس سال شہر کے بازار میں کپڑے اور کھجور بیچنے والے بہت سے تاجر آئے اور "
          "شام تک خریداروں کی بھیڑ رہی۔",
    "tl": "Noong taong iyon, maraming mangangalakal ang dumating sa pamilihan ng "
          "lungsod upang magbenta ng tela at datiles.",
    "bn": "সেই বছর শহরের বাজারে অনেক ব্যবসায়ী কাপড় আর খেজুর বিক্রি করতে এসেছিল "
          "এবং সন্ধ্যা পর্যন্ত ক্রেতাদের ভিড় ছিল।",
}


def test_scripture_guard_does_not_label_neutral_prose_in_any_language():
    """
    The guard was calibrated on English. normalize() fragments Devanagari at
    vowel signs into one-letter tokens found in every verse, so the neutral
    Hindi sentence scored likeness 1.0 and was labelled SCRIPTURE_LIKE.
    Thresholds are now per language (docs/CLARITY.md §11).
    """
    if _mizan() is None:
        print("      SKIP: corpus not available")
        return
    for lang, sent in _NEUTRAL.items():
        assert scripture_reasons(sent, lang) == [], (lang, _clarity.scripture_likeness(sent, lang))


def test_scripture_guard_still_catches_verse_runs_in_every_language():
    """Recall per language, on runs read from that language's own edition."""
    m = _mizan()
    if m is None:
        print("      SKIP: corpus not available")
        return
    frames = {"ur": ("اس دن استاد نے کلاس کو بتایا کہ", "اور پھر سبق ختم ہو گیا۔"),
              "tl": ("Noong araw na iyon sinabi ng guro sa klase na",
                     "at saka natapos ang aralin."),
              "hi": ("उस दिन शिक्षक ने कक्षा को बताया कि", "और फिर पाठ समाप्त हो गया।"),
              "bn": ("সেদিন শিক্ষক ক্লাসকে বললেন যে", "এবং তারপর পাঠ শেষ হলো।")}
    for lang, (pre, suf) in frames.items():
        editions = sorted(m.editions(lang))
        if not editions:
            continue
        caught = tried = 0
        for ayah in range(1, 287):
            words = (m.verse(editions[0], 2, ayah) or "").split()
            if len(words) < 32:
                continue
            sent = f"{pre} {' '.join(words[3:28])} {suf}"
            tried += 1
            caught += scripture_reasons(sent, lang) == [R_SCRIPTURE_LIKE]
            if tried == 10:
                break
        assert caught >= 8, (lang, caught, tried)


def test_unscreened_language_is_refused_with_no_suggestions():
    """
    The ruling, creed and condition patterns read English only. In any other
    language every prose sentence is refused, nothing is adapted, no
    suggestion is made (a LENGTH note on a sentence we cannot screen is advice
    we cannot stand behind), Flesch is not invented, and the report says so.
    """
    for lang, sent in _NEUTRAL.items():
        rep = adapt_document(sent + " " + sent, CURIOUS, lang=lang)
        assert rep.n_prose >= 1
        assert rep.n_refused == rep.n_prose and rep.n_adapted == 0, lang
        for s in rep.sentences:
            assert R_LANGUAGE_UNSCREENED in s.refusal_reasons, (lang, s.refusal_reasons)
            assert s.suggestions == [] and s.output == s.original
        d = rep.as_dict()
        assert d["suggestions"] == []
        support = d["language_support"]
        assert support["ruling_screen"] is False and support["adaptation"] is False
        assert support["note"] and support["note_ar"], support
        assert d["readability_before"]["flesch"] is None, lang
        assert "English only" in rep.summary
    # English is screened, and says so.
    d = adapt_document("The committee met in the spring of that year.",
                       CURIOUS, lang="en").as_dict()
    assert d["language_support"]["ruling_screen"] is True
    assert d["language_support"]["note"] is None


def test_a_foreign_script_sentence_in_an_english_document_is_unscreened():
    """An Urdu sentence inside English text is not read by English patterns."""
    doc = ("The committee met in the spring of that year to review it. "
           + _NEUTRAL["ur"])
    rep = adapt_document(doc, CURIOUS, lang="en")
    assert R_LANGUAGE_UNSCREENED not in rep.sentences[0].refusal_reasons
    assert R_LANGUAGE_UNSCREENED in rep.sentences[-1].refusal_reasons


def test_danda_ends_a_sentence():
    """Without । a Hindi or Bengali document was a single "sentence"."""
    assert len(split_sentences("पहला वाक्य यहाँ है। दूसरा वाक्य यहाँ है।")) == 2
    assert len(split_sentences("প্রথম বাক্য এখানে। দ্বিতীয় বাক্য এখানে।")) == 2


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

    # The refusal rate is a headline number, so print it on every run.
    rep = adapt_document(SAMPLE, CURIOUS)
    print(f"\nmeasured on the realistic sample (level=curious):")
    print(f"  {rep.summary}")
    for code, n in sorted(rep.refusal_counts.items(), key=lambda kv: -kv[1]):
        print(f"    {n:>2}x {code}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
