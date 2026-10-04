#!/usr/bin/env python3
"""
MIZAN — Panel 2 (terminology lock) tests.

Runnable either way:
    python3 -m pytest tests/test_terms.py -v
    python3 tests/test_terms.py

What these tests are actually defending
---------------------------------------
Panel 2 makes one kind of claim: "the Arabic uses term X; the translation does
(or does not) use the equivalent the approved dictionary gives for X". The
claim is only worth making if

  * an approved rendering, or an accepted spelling of its transliteration,
    is never reported as a problem (false alarms teach editors to ignore us);
  * a reduction the package's ضابط الاستخدام rules out IS reported, with the
    rule quoted;
  * the panel says "cannot check" instead of guessing — for other languages,
    for ambiguous unvocalised Arabic, and with no Arabic at all;
  * it runs offline, deterministically, and never rewrites the text.

Two kinds of test below. Most run against a small SYNTHETIC glossary written
to a temp dir, so the logic is pinned regardless of what the crawl collected.
The `test_real_*` ones run against data/glossary/ as collected from الجمهرة
and skip (not fail) when it is absent.
"""
import importlib.util
import json
import os
import socket
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from mizan import terms as T                                    # noqa: E402
from mizan.terms import (                                       # noqa: E402
    APPROVED, MISSING, NOT_IN_GLOSSARY, STATUSES, VARIANT,
    ar_candidates, check_terms, fold_ar, latin_key, load_glossary,
)


def _fetch_script():
    """scripts/fetch_jamhara.py as a module (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location(
        "fetch_jamhara", os.path.join(ROOT, "scripts", "fetch_jamhara.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------------
# Synthetic glossary. Shapes copied from real records; URLs are the real
# entry URLs so a reader can check them, but the CONTENT is fixed here so the
# tests do not move when the crawl is re-run.
# ----------------------------------------------------------------------

W = "https://islamic-content.com/dictionary/word"
_FIXTURE = [
    ("التوحيد", ["Monotheism", "Tawheed"], 3529, False, ["ur", "fr"]),
    ("الشريعة", ["The sharia", "Sharee‘ah"], 5979, False, []),
    ("العبادة", ["Worship", "`Ibaadah"], 6732, False, []),
    ("المناسك", ["Rites"], 14706, False, []),
    ("الصلاة", ["Prayer", "Salah"], 6327, False, ["ur"]),
    ("الزكاة", ["Zakat"], 5419, False, []),
    ("السنة", ["The sunnah"], 5744, True, []),
    ("المسجد الحرام", ["The Sacred Mosque"], 9431, False, []),
    ("الحرام", ["Forbidden"], 4169, False, []),
    ("المسجد", ["Mosque"], 9429, False, []),
    ("القرآن", ["The Qur’an"], 7771, False, []),
    ("الصمد", None, 99999, False, []),        # an entry with no English
    ("أهل الكتاب", ["People of scripture"], 1817, False, []),
    ("القبلة", ["The qiblah"], 7670, False, []),
    ("صلاة الجماعة", ["Congregational prayer"], 6330, False, []),
]

_TMP = []


def _fixture_glossary():
    """Write the fixture once per process and load it."""
    if not _TMP:
        d = tempfile.mkdtemp(prefix="mizan_terms_")
        with open(os.path.join(d, T.JAMHARA_FILE), "w", encoding="utf-8") as f:
            for ar, en, wid, amb, langs in _FIXTURE:
                f.write(json.dumps({
                    "term_ar": ar, "approved_en": en,
                    "definition_ar": "تعريف", "category": "العقيدة",
                    "source_url": f"{W}/{wid}/en" if en else f"{W}/{wid}",
                    "retrieved_at": "2026-10-03T00:00:00Z",
                    "word_id": wid, "seed_ar": ar, "sense_rank": 0,
                    "source_url_ar": f"{W}/{wid}", "other_languages": langs,
                    "match_ar": [ar], "ambiguous_ar": amb,
                    "definition_en": "A definition.",
                }, ensure_ascii=False) + "\n")
        fj = _fetch_script()
        with open(os.path.join(d, T.PACKAGE_FILE), "w", encoding="utf-8") as f:
            json.dump({"source": fj.PACKAGE_SOURCE, "terms": [
                {**p, "approved_en": [x.strip() for x in
                                      p["approved_en_raw"].split("/")]}
                for p in fj.PACKAGE_TERMS]}, f, ensure_ascii=False)
        _TMP.append(d)
    return load_glossary(_TMP[0])


def _check(arabic, translation, lang="en"):
    return check_terms(arabic, translation, lang, glossary=_fixture_glossary())


def _by_term(rep):
    return {f.term_ar: f for f in rep.findings}


# ======================================================================
# 1. Approved renderings and accepted transliterations -> APPROVED
# ======================================================================

def test_dictionary_equivalent_is_approved():
    rep = _check("التوحيد أساس الدعوة.", "Monotheism is the basis of the call.")
    f = _by_term(rep)["التوحيد"]
    assert f.status == APPROVED, f
    assert f.found == "Monotheism"
    assert f.source_url == f"{W}/3529/en", "suggestion must cite its source"


def test_package_equivalent_is_approved_even_if_dictionary_differs():
    """The package's own English column ('Oneness of God') is approved too."""
    rep = _check("التوحيد", "This is the Oneness of God.")
    assert _by_term(rep)["التوحيد"].status == APPROVED


def test_transliteration_spellings_are_all_accepted():
    cases = {
        "التوحيد": ["Tawhid", "Tawheed", "Tauhid", "at-Tawhid", "TAWHID"],
        "الصلاة": ["Salah", "Salat", "Salaah", "as-Salah"],
        "الزكاة": ["Zakat", "Zakah", "Zakaat"],
        "الشريعة": ["Sharia", "Shariah", "Shari'ah", "Sharee‘ah"],
    }
    for ar, spellings in cases.items():
        for sp in spellings:
            rep = _check(ar, f"The text explains {sp} to new readers.")
            f = _by_term(rep)[ar]
            assert f.status == APPROVED, (ar, sp, f)
            assert f.found == sp, (sp, f.found)


def test_plural_and_possessive_forms_still_match():
    rep = _check("الصلاة", "Their prayers were long.")
    assert _by_term(rep)["الصلاة"].status == APPROVED
    rep = _check("الشريعة", "The Sharia's scope is wide.")
    assert _by_term(rep)["الشريعة"].status == APPROVED


def test_latin_key_unifies_spelling_but_not_different_words():
    same = [("Salah", "Salat"), ("Zakat", "Zakah"), ("Tawhid", "Tawheed"),
            ("Qur'an", "Quran"), ("Sharee‘ah", "Sharia"), ("Ka'bah", "Kaaba"),
            ("KaꜤbah", "Kabah"), ("Du‘ā’", "Dua")]
    for a, b in same:
        assert latin_key(a) == latin_key(b), (a, b, latin_key(a), latin_key(b))
    for a, b in [("Quran", "Koran"), ("Sawm", "Siyam"), ("Salah", "Sabr")]:
        assert latin_key(a) != latin_key(b), (a, b)


# ======================================================================
# 2. Reductions the package rules out -> VARIANT, with the rule quoted
# ======================================================================

def test_sharia_reduced_to_penal_law_is_variant():
    rep = _check("تطبيق الشريعة في المجتمع",
                 "Applying Islamic penal law in society.")
    f = _by_term(rep)["الشريعة"]
    assert f.status == VARIANT, f
    assert f.found == "Islamic penal law"
    assert "ولا يختزل في العقوبات أو القانون الجنائي" in f.suggestion, \
        "a VARIANT must quote the package rule it rests on"


def test_worship_narrowed_to_rituals_is_variant():
    rep = _check("العبادة لله وحده", "The rituals are for God alone.")
    f = _by_term(rep)["العبادة"]
    assert f.status == VARIANT and f.found == "rituals", f
    assert "الشعائر" in f.suggestion


def test_discouraged_rendering_wins_even_with_the_approved_term_present():
    """
    Changed 2026-10-04. The first version returned APPROVED as soon as any
    approved form was present, so a reduction written next to the approved
    term ("Tawhid here simply means numerical oneness") passed. Discouraged
    renderings are now checked first and win. "Not merely penal law" contains
    the discouraged words too, so it is flagged — and the suggestion says that
    a denial of the reduction needs no change. Over-flagging is the safe side.
    """
    rep = _check("الشريعة", "The Sharia is not merely penal law.")
    f = _by_term(rep)["الشريعة"]
    assert f.status == VARIANT and f.found == "penal law", f
    assert "also uses the approved “Sharia”" in f.suggestion, f.suggestion
    assert "no change is needed" in f.suggestion


def test_reduction_next_to_the_approved_term_is_variant():
    """The review's two examples, with the Arabic beside them."""
    cases = [
        ("التوحيد", "Tawhid here simply means numerical oneness.",
         "التوحيد", "numerical oneness", "الوحدانية العددية"),
        ("تطبيق الشريعة", "The Sharia is, in this book, the penal code of "
         "punishments.", "الشريعة", "penal code", "العقوبات"),
    ]
    for ar, tr, term, found, rule_words in cases:
        f = _by_term(_check(ar, tr))[term]
        assert f.status == VARIANT, (tr, f)
        assert f.found == found, f.found
        assert rule_words in f.suggestion, "the package rule must be quoted"


def test_without_arabic_a_reduction_in_the_terms_own_sentence_is_flagged():
    """
    With no Arabic, a discouraged rendering is reported only when it sits in
    the SAME sentence as the term's own transliteration: the text names the
    term and explains it reductively. Elsewhere it is an ordinary word.
    """
    rep = _check(None, "Tawhid here simply means numerical oneness.")
    f = _by_term(rep)["التوحيد"]
    assert f.status == VARIANT and f.found == "numerical oneness", f
    assert "may reduce" in f.suggestion
    rep = _check(None, "The Sharia is, in this book, the penal code of punishments.")
    assert _by_term(rep)["الشريعة"].status == VARIANT
    # Different sentences: no claim.
    rep = _check(None, "The Sharia guides all of life. The thief faced punishment.")
    assert _by_term(rep)["الشريعة"].status == APPROVED


def test_discouraged_phrase_that_renders_another_term_is_not_blamed():
    """
    «المناسك» is approved as "Rites". When it is in the same Arabic, "rites"
    in the translation is ITS rendering, not a reduction of «العبادة».
    """
    rep = _check("العبادة والمناسك", "the rites")
    by = _by_term(rep)
    assert by["المناسك"].status == APPROVED
    assert by["العبادة"].status == MISSING, by["العبادة"]


def test_unattested_spelling_is_variant():
    rep = _check("القرآن كتاب الله", "The Koran is the book of God.")
    f = _by_term(rep)["القرآن"]
    assert f.status == VARIANT and f.found == "Koran", f


# ======================================================================
# 3. Absent -> MISSING, but only where that is a fair thing to say
# ======================================================================

def test_term_absent_from_translation_is_missing():
    rep = _check("أقام الصلاة في وقتها", "He performed it on time.")
    f = _by_term(rep)["الصلاة"]
    assert f.status == MISSING, f
    assert f.found is None
    assert "Prayer" in f.suggestion and "nothing was changed" in f.suggestion


def test_ambiguous_unvocalised_term_is_never_missing():
    """«السنة» is also 'the year'. No approved rendering -> skipped, not MISSING."""
    rep = _check("في تلك السنة سافر", "That year he travelled.")
    assert "السنة" not in _by_term(rep)
    assert "السنة" in rep.skipped_ambiguous
    # ...but an approved rendering of it is still confirmed.
    rep = _check("اتباع السنة", "Following the Sunnah.")
    assert _by_term(rep)["السنة"].status == APPROVED


def test_entry_without_english_is_not_in_glossary_not_missing():
    rep = _check("الله الصمد", "God, the Self-Sufficient.")
    f = _by_term(rep)["الصمد"]
    assert f.status == NOT_IN_GLOSSARY and f.approved_en is None, f


# ======================================================================
# 4. Arabic matching: clitics, suffixes, longest match, scripture skipped
# ======================================================================

def test_clitic_prefixes_and_suffixes_are_handled():
    for form in ["الصلاة", "والصلاة", "فالصلاة", "بالصلاة", "للصلاة",
                 "كالصلاة", "صلاة", "صلاته", "وصلاتهم", "صَلَاتِهِ"]:
        rep = _check(f"تحدث عن {form} طويلا", "He spoke at length.")
        assert "الصلاة" in _by_term(rep), form


def test_guards_stop_clitic_stripping_from_inventing_matches():
    """«حجة» (argument) is not «الحج»; «كبر» is not ك + «بر»."""
    assert "حج" not in ar_candidates(fold_ar("حجة"))
    assert "بر" not in ar_candidates(fold_ar("كبر"))


def test_longest_match_wins():
    rep = _check("صلى في المسجد الحرام", "He prayed in the Sacred Mosque.")
    by = _by_term(rep)
    assert "المسجد الحرام" in by and by["المسجد الحرام"].status == APPROVED
    assert "الحرام" not in by, "«الحرام» inside «المسجد الحرام» must not fire"
    assert "المسجد" not in by


def test_ta_marbuta_is_not_folded_into_a_pronoun_suffix():
    """
    engine.normalize folds ة -> ه, which makes «عبادة» (worship) look like
    «عباده» (His servants) and «القبلة» like «قبله» (before him). Measured on
    approved Quran translations that produced hundreds of false MISSINGs.
    """
    rep = _check("ومن قبله كانوا عباده", "And before him they were His servants.")
    assert rep.findings == [], [f.as_dict() for f in rep.findings]
    rep = _check("استقبل القبلة في عبادته", "He faced the qiblah in his worship.")
    by = _by_term(rep)
    assert by["القبلة"].status == APPROVED and by["العبادة"].status == APPROVED


def test_english_articles_inside_phrases_do_not_block_a_match():
    """The dictionary writes "People of scripture"; publishers add "the"."""
    rep = _check("أهل الكتاب", "Then the People of the Scripture said so.")
    f = _by_term(rep)["أهل الكتاب"]
    assert f.status == APPROVED and f.found == "People of the Scripture", f


def test_honorific_formulas_are_not_terminology():
    """«عليه الصلاة والسلام» is a blessing formula, not the term «الصلاة»."""
    for ar in ("أنزل الله التوراة على موسى عليه الصلاة والسلام",
               "والصلاة والسلام على رسول الله",
               "قال عليه الصَّلاةُ والسَّلامُ"):
        rep = _check(ar, "peace be upon him")
        assert "الصلاة" not in _by_term(rep), (ar, [f.as_dict() for f in rep.findings])
    # The term itself, next to the formula, is still found.
    rep = _check("علمنا الصلاة عليه الصلاة والسلام", "He taught us prayer, peace be upon him.")
    assert _by_term(rep)["الصلاة"].status == APPROVED


def test_a_rendering_inside_another_terms_phrase_still_counts():
    """
    "congregational prayer" renders «صلاة الجماعة» and contains "prayer",
    which renders «الصلاة». With both in the Arabic, both are APPROVED.
    """
    rep = _check("الإقامة إعلام بالشروع في الصلاة، وفضل صلاة الجماعة كبير",
                 "Announcing that the congregational prayer is about to start.")
    by = _by_term(rep)
    assert by["صلاة الجماعة"].status == APPROVED
    assert by["الصلاة"].status == APPROVED, by["الصلاة"]


def test_consistency_mode_does_not_report_a_word_inside_a_longer_term():
    rep = _check(None, "They prayed in al-Masjid al-Haram.")
    assert "الحرام" not in _by_term(rep), [f.as_dict() for f in rep.findings]


def test_scripture_inside_ornate_brackets_is_not_terminology_checked():
    rep = _check("قال تعالى ﴿ وأقيموا الصلاة وآتوا الزكاة ﴾", "God said: ...")
    assert not rep.findings, [f.as_dict() for f in rep.findings]


def test_parenthesised_terms_are_still_found():
    """engine.normalize drops '(...)'; terms written that way must survive."""
    rep = _check("وهذا هو (التوحيد) الخالص", "This is pure Tawhid.")
    assert _by_term(rep)["التوحيد"].status == APPROVED


# ======================================================================
# 5. No Arabic: consistency mode only
# ======================================================================

def test_without_arabic_only_consistency_is_checked():
    rep = _check(None, "Tawhid comes first. Later the author writes Tawheed.")
    assert rep.mode == T.MODE_TRANSLATION_ONLY
    f = _by_term(rep)["التوحيد"]
    assert f.status == VARIANT, f
    assert "Tawhid" in f.found and "Tawheed" in f.found


def test_apostrophe_style_is_not_an_inconsistency():
    """Straight vs curly apostrophe is typography, not a second spelling."""
    rep = _check(None, "The Qur'an was read; later the Qur’an was recited.")
    assert _by_term(rep)["القرآن"].status == APPROVED


def test_the_arabic_article_is_not_a_second_spelling():
    """"as-Salah" then "Salah" is one spelling with and without the article."""
    rep = _check(None, "He described as-Salah; later, Salah was explained.")
    assert _by_term(rep)["الصلاة"].status == APPROVED


def test_words_inside_a_multiword_glossary_hit_are_not_reported_unknown():
    rep = _check(None, "They prayed in al-Masjid al-Ḥarām that night.")
    assert all(f.status != NOT_IN_GLOSSARY for f in rep.findings), \
        [f.as_dict() for f in rep.findings]


def test_without_arabic_consistent_transliteration_is_approved():
    rep = _check("", "Zakat is due once a year; Zakat is paid on savings.")
    f = _by_term(rep)["الزكاة"]
    assert f.status == APPROVED and rep.mode == T.MODE_TRANSLATION_ONLY


def test_without_arabic_plain_english_raises_nothing():
    """'prayer' and 'mosque' could render many Arabic words; no claim made."""
    rep = _check(None, "They walked to the mosque for the evening prayer.")
    assert rep.findings == [], [f.as_dict() for f in rep.findings]


def test_without_arabic_words_outside_the_glossary_are_counted_not_listed():
    """
    Changed 2026-10-04. The first version reported every transliterated word
    outside the glossary as a NOT_IN_GLOSSARY finding with the word in
    `found` — mostly personal names, which app.py stores with the check. Now
    they are counted and never listed.
    """
    rep = _check(None, "Ibn Mas’ud narrated it; Qur’ān scholars agree, and "
                       "Ḥamīda and Sa’id won’t disagree.")
    by_found = {f.found: f for f in rep.findings}
    assert set(by_found) == {"Qur’ān"}, [f.as_dict() for f in rep.findings]
    assert by_found["Qur’ān"].status == APPROVED
    assert rep.unchecked_transliterations == 3, rep.unchecked_transliterations
    blob = json.dumps(rep.as_dict(), ensure_ascii=False)
    for name in ("Mas’ud", "Ḥamīda", "Sa’id"):
        assert name not in blob, f"{name} leaked into the stored report"
    assert any("3 transliterated words" in n for n in rep.notes), rep.notes


def test_findings_only_carry_glossary_terms():
    """
    Every finding names a glossary term, and `found` is only the matched
    form — never surrounding prose — in both modes.
    """
    text = ("Ibn Mas’ud told Ḥamīda that Tawheed and the Sharia, which some "
            "call Islamic penal law, were taught by Sa’id in the Koran class.")
    g = _fixture_glossary()
    for arabic in (None, "التوحيد والشريعة والقرآن والصلاة"):
        rep = check_terms(arabic, text, "en", glossary=g)
        assert rep.findings
        forms = {}
        for t in g.terms:
            fs = ([x for a in t.approved_en for x in T._split_alternatives(a)]
                  + t.translit + t.discouraged + t.unattested)
            forms[t.term_ar] = {T._form_keys(x) for x in fs}
        for f in rep.findings:
            assert f.term_ar, f
            if f.found is None:
                continue
            for part in f.found.split(" / "):
                assert T._form_keys(part) in forms[f.term_ar], (f.term_ar, part)


# ======================================================================
# 6. Other languages: honest about what can be checked
# ======================================================================

def test_non_latin_language_is_not_in_glossary_never_missing():
    rep = _check("التوحيد والصلاة", "توحید اور نماز", lang="ur")
    statuses = {f.status for f in rep.findings}
    assert statuses == {NOT_IN_GLOSSARY}, [f.as_dict() for f in rep.findings]
    tawhid = _by_term(rep)["التوحيد"]
    assert tawhid.source_url == f"{W}/3529/ur", \
        "should point at the dictionary's own Urdu page when it exists"
    assert any("cannot check" in n for n in rep.notes)


def test_latin_script_language_gets_the_transliteration_check():
    rep = _check("التوحيد والصلاة", "Ang Tawhid ay batayan ng pananampalataya.",
                 lang="tl")
    by = _by_term(rep)
    assert by["التوحيد"].status == APPROVED
    assert by["الصلاة"].status == NOT_IN_GLOSSARY, "no guess for Tagalog words"


# ======================================================================
# 7. Contract, determinism, no rewriting, offline
# ======================================================================

def test_output_contract_shape():
    rep = _check("التوحيد والشريعة والصلاة",
                 "Tawhid, penal law, and nothing else.")
    d = rep.as_dict()
    json.dumps(d, ensure_ascii=False)
    assert {"findings", "counts"} <= set(d)
    for f in d["findings"]:
        assert set(f) == {"term_ar", "approved_en", "status", "found",
                          "suggestion", "source_url"}, set(f)
        assert f["status"] in STATUSES
        assert f["approved_en"] is None or isinstance(f["approved_en"], list)
    c = d["counts"]
    assert set(STATUSES) <= set(c)
    assert sum(c[s] for s in STATUSES) == c["total"] == len(d["findings"])


def test_found_is_quoted_verbatim_from_the_translation():
    """The panel quotes; it never normalises or rewrites what it reports."""
    tr = "As-Salah and ZAKĀT, then the Sharee‘ah and Islamic Penal Law."
    rep = _check("الصلاة والزكاة والشريعة والعبادة", tr)
    for f in rep.findings:
        if f.found:
            assert f.found in tr, f.found


def test_deterministic():
    a = _check("التوحيد والشريعة", "Tawheed and penal law.").as_dict()
    b = _check("التوحيد والشريعة", "Tawheed and penal law.").as_dict()
    assert a == b


def test_deterministic_across_processes():
    """
    Same input, different PYTHONHASHSEED, identical report. A tie between two
    terms at one Arabic position once depended on set iteration order, which
    Python randomises per process; this pins the fix.
    """
    import subprocess
    code = (
        "import json,sys; sys.path.insert(0, %r); "
        "from mizan.terms import check_terms; "
        "r = check_terms(%r, %r, 'en'); "
        "print(json.dumps(r.as_dict(), ensure_ascii=False, sort_keys=True))"
    ) % (os.path.join(ROOT, "src"),
         "وأقيموا صلاتهم وآتوا زكاتهم، فالإيمان والإحسان والتوحيد والشريعة "
         "والعبادة والسنة والحديث والدعوة والجنة والنار والقرآن والمسجد الحرام",
         "Establish prayer and give zakah; faith, excellence, Tawheed, the "
         "Sharia, worship, the Sunnah, hadith, the call, Paradise, the Fire, "
         "the Qur'an and the Sacred Mosque.")
    outs = set()
    for seed in ("0", "1", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        outs.add(subprocess.run([sys.executable, "-c", code], env=env,
                                capture_output=True, text=True,
                                check=True).stdout)
    assert len(outs) == 1, "report differs across hash seeds"


def test_runs_offline_with_no_network():
    """Block sockets entirely; loading and checking must still work."""
    real = socket.socket

    def _blocked(*a, **k):
        raise AssertionError("terms.py attempted network access")

    socket.socket = _blocked
    try:
        load_glossary.cache_clear()
        rep = check_terms("التوحيد", "Tawhid", "en")
        assert rep.findings and rep.findings[0].status == APPROVED
    finally:
        socket.socket = real


def test_falls_back_to_the_package_ten_without_data():
    empty = tempfile.mkdtemp(prefix="mizan_empty_")
    g = load_glossary(empty)
    assert g.source == "fallback"
    assert len(g.terms) == 10
    rep = check_terms("الشريعة", "Islamic criminal law", "en", glossary=g)
    assert rep.findings[0].status == VARIANT
    assert any("ten terms" in n for n in rep.notes)


def test_a_running_process_picks_up_the_glossary_when_it_appears():
    """
    A server started before data/glossary/ existed must switch from the
    package-ten fallback to the collected glossary without a restart.
    """
    d = tempfile.mkdtemp(prefix="mizan_late_")
    assert load_glossary(d).source == "fallback"
    src = _TMP[0] if _TMP else None
    if src is None:
        _fixture_glossary()
        src = _TMP[0]
    for name in (T.PACKAGE_FILE, T.JAMHARA_FILE):
        with open(os.path.join(src, name), encoding="utf-8") as f_in, \
                open(os.path.join(d, name), "w", encoding="utf-8") as f_out:
            f_out.write(f_in.read())
    g = load_glossary(d)
    assert g.source == "jamhara+package", g.source
    assert len(g.terms) > 10


def test_reviewed_dictionary_errors_are_not_used_as_approved_renderings():
    """
    The dictionary is the authority, but some pages contradict themselves.
    A wrong-sense entry is not loaded; a suspect title is dropped while the
    dictionary's own transliteration is kept. Nothing is substituted.
    """
    d = tempfile.mkdtemp(prefix="mizan_review_")
    recs = [
        {"term_ar": "الحوض", "approved_en": ["Pond"], "sense_rank": 0,
         "match_ar": ["الحوض"], "source_url": f"{W}/4544/en", "word_id": 4544,
         "review": {"flag": "wrong_sense", "reason": "a pool, not the Basin"}},
        {"term_ar": "الحوض", "approved_en": ["The Basin"], "sense_rank": 1,
         "match_ar": ["الحوض"], "source_url": f"{W}/4545/en", "word_id": 4545},
        {"term_ar": "البعث", "approved_en": ["Expedition", "Ba`th"],
         "sense_rank": 0, "match_ar": ["البعث"], "source_url": f"{W}/2118/en",
         "word_id": 2118,
         "review": {"flag": "suspect_title", "reason": "defined as resurrection"}},
    ]
    with open(os.path.join(d, T.JAMHARA_FILE), "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    g = load_glossary(d)

    rep = check_terms("الحوض", "They gathered at the pond.", "en", glossary=g)
    f = _by_term(rep)["الحوض"]
    assert f.status == MISSING and "Pond" not in (f.approved_en or []), f
    assert f.source_url == f"{W}/4545/en"

    rep = check_terms("البعث", "The expedition set out.", "en", glossary=g)
    f = _by_term(rep)["البعث"]
    assert f.status == MISSING and "Expedition" not in (f.approved_en or []), f
    rep = check_terms("البعث", "Belief in the Ba'th.", "en", glossary=g)
    assert _by_term(rep)["البعث"].status == APPROVED


def test_a_curated_spelling_is_never_presented_as_an_approved_equivalent():
    """
    Every dictionary sense of «التفسير» was reviewed out (none is exegesis),
    leaving only MIZAN's curated spelling "Tafsir". Using it is accepted;
    its absence is NOT_IN_GLOSSARY, not MISSING-with-"Approved: Tafsir".
    """
    d = tempfile.mkdtemp(prefix="mizan_tafsir_")
    with open(os.path.join(d, T.JAMHARA_FILE), "w", encoding="utf-8") as f:
        f.write(json.dumps({"term_ar": "التفسير", "approved_en": ["Understanding"],
                            "match_ar": ["التفسير"], "sense_rank": 0,
                            "source_url": f"{W}/3153/en", "word_id": 3153,
                            "review": {"flag": "suspect_title", "reason": "x"}},
                           ensure_ascii=False) + "\n")
    g = load_glossary(d)
    f = check_terms("كتب التفسير", "books of exegesis", "en", glossary=g).findings[0]
    assert f.status == NOT_IN_GLOSSARY and f.approved_en is None, f
    assert "Tafsir" in f.suggestion
    f = check_terms("كتب التفسير", "books of tafsir", "en", glossary=g).findings[0]
    assert f.status == APPROVED, f


def test_subphrases_must_come_from_the_dictionary_title():
    """
    "Patience" is accepted for الصبر because the dictionary's own title is
    "Patience in adversity". A word that is NOT in the title ("Endurance") is
    ignored even if someone adds it to the table.
    """
    d = tempfile.mkdtemp(prefix="mizan_sub_")
    with open(os.path.join(d, T.JAMHARA_FILE), "w", encoding="utf-8") as f:
        f.write(json.dumps({"term_ar": "الصبر",
                            "approved_en": ["Patience in adversity"],
                            "match_ar": ["الصبر"], "sense_rank": 0,
                            "source_url": f"{W}/6128/en"},
                           ensure_ascii=False) + "\n")
    real = dict(T.SUBPHRASES)
    T.SUBPHRASES["الصبر"] = ["Patience", "Endurance"]
    try:
        g = load_glossary(d)
    finally:
        T.SUBPHRASES.clear()
        T.SUBPHRASES.update(real)
    approved = next(t for t in g.terms if t.term_ar == "الصبر").approved_en
    assert "Patience" in approved and "Endurance" not in approved, approved
    rep = check_terms("الصبر", "Endurance in hardship.", "en", glossary=g)
    assert rep.findings[0].status == MISSING


# ======================================================================
# 8. The collector: robots policy and homograph resolution
# ======================================================================

def test_robots_policy_unions_groups_and_respects_crawl_delay():
    fj = _fetch_script()
    text = ("User-agent: ClaudeBot\nDisallow: /ayah/\nCrawl-delay: 5\n\n"
            "User-agent: Amazonbot\nDisallow: /dictionary/\n\n"
            "User-agent: *\nDisallow: /admin/\nDisallow: /api/\n")
    p = fj.robots_policy(text)
    assert {"/ayah/", "/admin/", "/api/"} <= set(p["disallow"])
    assert "/dictionary/" not in p["disallow"], "Amazonbot's rule is not ours"
    assert p["delay_used"] >= 5.0
    f = fj.Fetcher(p, offline=True)
    assert not f.allowed("https://islamic-content.com/api/words")
    assert not f.allowed("https://islamic-content.com/ayah/1")
    assert f.allowed("https://islamic-content.com/dictionary/word/196")
    assert not f.allowed("https://example.com/dictionary/word/196")


def test_hard_deny_holds_even_if_robots_allows_everything():
    fj = _fetch_script()
    p = fj.robots_policy("User-agent: *\nDisallow:\n")
    f = fj.Fetcher(p, offline=True)
    assert not f.allowed("https://islamic-content.com/api/x")
    assert not f.allowed("https://islamic-content.com/ayah/2/255")


def test_vowels_separate_homographs():
    fj = _fetch_script()
    assert not fj.vowel_compatible("الخُطْبَة", "الْخِطْبَةُ")   # sermon/proposal
    assert fj.vowel_compatible("الخُطْبَة", "خُطْبَةُ")
    assert not fj.vowel_compatible("الإله", "الآلَةُ")           # God/tool
    assert not fj.vowel_compatible("القُرْآن", "الْقِرَانُ")       # Quran/qiran
    assert not fj.vowel_compatible("المُصْحَف", "الْمُصَحَّف")
    assert fj.vowel_compatible("التوحيد", "التَّوْحِيد")          # no vowels: ok


# ======================================================================
# 9. The collected data (skips when data/glossary/ is absent)
# ======================================================================

def _real():
    load_glossary.cache_clear()
    g = load_glossary()
    if g.source != "jamhara+package":
        print("      SKIP: data/glossary/jamhara.jsonl not present")
        return None
    return g


def test_real_records_carry_required_fields_and_sources():
    if _real() is None:
        return
    path = os.path.join(T.GLOSSARY_DIR, T.JAMHARA_FILE)
    if not os.path.exists(path):
        # The full dictionary file (with definitions) is local-only — its
        # licence is personal, non-commercial — so a clean clone has only
        # jamhara.public.jsonl. Nothing to check here.
        print("      SKIP: data/glossary/jamhara.jsonl not present (local-only file)")
        return
    with open(path, encoding="utf-8") as f:
        recs = [json.loads(line) for line in f]
    assert len(recs) >= 150, len(recs)
    for r in recs:
        for k in ("term_ar", "approved_en", "definition_ar", "category",
                  "source_url", "retrieved_at"):
            assert k in r, (k, r.get("term_ar"))
        assert r["approved_en"] is None or (
            isinstance(r["approved_en"], list) and all(r["approved_en"]))
        assert r["source_url"].startswith(
            "https://islamic-content.com/dictionary/word/"), r["source_url"]
        assert "/api/" not in r["source_url"] and "/ayah/" not in r["source_url"]
        if r["approved_en"]:
            assert r["source_url"].endswith("/en"), \
                "an English equivalent must cite the English page it came from"


def test_real_package_terms_are_transcribed_and_linked():
    if _real() is None:
        return
    with open(os.path.join(T.GLOSSARY_DIR, T.PACKAGE_FILE), encoding="utf-8") as f:
        pkg = json.load(f)
    assert len(pkg["terms"]) == 10
    for p in pkg["terms"]:
        assert p["usage_rule_ar"].strip()
        assert p["jamhara"] and p["jamhara"]["source_url"], p["term_ar"]


def test_real_glossary_behaves_on_core_terms():
    g = _real()
    if g is None:
        return
    cases = [
        ("التوحيد", "Monotheism", APPROVED),
        ("التوحيد", "Tawheed", APPROVED),
        ("الصلاة", "Salat", APPROVED),
        ("الزكاة", "Zakah", APPROVED),
        ("الشريعة", "Islamic penal law", VARIANT),
        ("العبادة", "rituals", VARIANT),
        ("الصيام", "Siyam", APPROVED),
        ("الوضوء", "Wudu", APPROVED),
        ("الصلاة", "nothing relevant here", MISSING),
    ]
    for ar, tr, want in cases:
        rep = check_terms(ar, f"The text: {tr}.", "en", glossary=g)
        got = {f.term_ar: f.status for f in rep.findings}
        assert want in got.values(), (ar, tr, want, got)


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
