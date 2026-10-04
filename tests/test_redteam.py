#!/usr/bin/env python3
"""
Regression tests from the independent red-team review of 2026-10-03.

Every attack here passed silently before the fix: a one-word meaning flip of an
approved verse came out MATCH and the document CLEAR. The binding standard
forbids exactly that — «ألا ينسب نص أو قول إلى مرجع لا يوجد فيه» and, for a
misquoted verse, «عدم البناء على النص المحرف» (scientific package pp. 5–6).

Verse text is read from the index at runtime and edited one word at a time; no
approved translation is stored in this file. The surrounding prose is our own.

Runs on the deterministic path. Runnable with pytest or plain python3.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mizan.detect import SpanDetector                  # noqa: E402
from mizan.engine import Mizan, strict_tokens, verbatim  # noqa: E402
from mizan.report import (MATCH, NEAR, UNRESOLVED,      # noqa: E402
                          FLAG_CITATION_MISMATCH, FLAG_CITED_NOT_FOUND,
                          FLAG_EXTRA_WORDS, FLAG_NO_SUCH_VERSE, check_document)

M = Mizan()
D = SpanDetector(M, "en", semantic=False)
SI = next(b for b, v in M.books.items() if "Sahih International" in v["title"])
PRE = "In this article we reflect on a passage from the Quran. "
POST = " Readers are encouraged to study the verse with a teacher."


def _v(s, a):
    return M.verse(SI, s, a)


def _check(body):
    return check_document(M, PRE + body + POST, "en", detector=D)


def _edit(s, a, old, new):
    v = _v(s, a)
    assert old in v, f"anchor {old!r} not in {s}:{a} — index changed?"
    return v.replace(old, new, 1)


# ---------------------------------------------------------------- the rule

def test_verbatim_is_word_identity_not_similarity():
    v = _v(4, 48)
    assert verbatim(v, v) == "whole"
    assert verbatim(v.upper().replace(",", ""), v) == "whole"     # case, punctuation
    assert verbatim(_edit(4, 48, "does not forgive", "does forgive"), v) is None
    assert verbatim(v.replace("forgives", "(never) forgives", 1), v) is None
    assert verbatim(" ".join(v.split()[3:12]), v) == "excerpt"
    assert verbatim("… " + " ".join(v.split()[2:8]) + " … " + " ".join(v.split()[10:16]), v) == "excerpt"


def test_strict_tokens_keep_indic_vowel_signs_and_fold_latin_accents():
    assert strict_tokens("मुहम्मद") == ["मुहम्मद"]
    assert strict_tokens("Allâh Muḥammad") == ["allah", "muhammad"]
    assert strict_tokens("sins?") == ["sins", "qmark"]


# ---------------------------------------------------------------- CRITICAL 1-2

FLIPS = [
    (4, 48, "does not forgive", "does forgive"),
    (39, 53, "forgives all sins", "forgives no sins"),
    (65, 12, "seven heavens", "six heavens"),
    (17, 32, "do not approach", "do approach"),
    (2, 286, "does not charge", "does charge"),
    (19, 30, "[Jesus]", "[Moses]"),
]


def test_one_word_meaning_flips_are_referred():
    for s, a, old, new in FLIPS:
        for cite in ("", f" (Quran {s}:{a})"):
            r = _check(f'"{_edit(s, a, old, new)}"{cite}')
            states = [f.state for f in r.findings]
            assert r.verdict == "REFER", (s, a, cite, states)
            assert MATCH not in states, (s, a, cite, states)


def test_the_unedited_verses_still_pass():
    for s, a, _, _ in FLIPS:
        r = _check(f'"{_v(s, a)}" (Quran {s}:{a})')
        assert r.verdict == "CLEAR", (s, a, [(f.state, f.flags) for f in r.findings])
        assert [f.state for f in r.findings] == [MATCH]


def test_a_parenthesised_insertion_is_a_change():
    v = _v(4, 48)
    r = _check(f'"{v.replace("forgives", "(never) forgives", 1)}" (Quran 4:48)')
    assert r.verdict == "REFER" and r.findings[0].state == NEAR


def test_statement_turned_into_question_is_a_change():
    v = _v(39, 53).rstrip('."') + '?"'
    r = _check(f'"{v}" (Quran 39:53)')
    assert r.verdict == "REFER"


def test_near_diff_shows_the_changed_word():
    r = _check(f'"{_edit(4, 48, "does not forgive", "does forgive")}" (Quran 4:48)')
    f = r.findings[0]
    changed = [op for op in f.diff if op.op != "equal"]
    assert changed and any("not" in " ".join(op.approved).lower() for op in changed)


# ---------------------------------------------------------------- HIGH

def test_a_repeated_verse_is_checked_twice():
    v, alt = _v(39, 53), _edit(39, 53, "forgives all sins", "forgives no sins")
    for first, second in ((v, alt), (alt, v)):
        r = _check(f'"{first}" (Quran 39:53). The author repeats it later: "{second}" (Quran 39:53).')
        assert r.verdict == "REFER"
        assert sorted(f.state for f in r.findings) == [MATCH, NEAR]


def test_invented_words_inside_the_quotation_marks_are_referred():
    tail = _check(f'"{_v(39, 53)} and whoever eats meat on a Friday will never enter Paradise." (Quran 39:53)')
    head = _check(f'"Music is forbidden to you, and indeed, {_v(4, 48)}" (Quran 4:48)')
    for r in (tail, head):
        assert r.verdict == "REFER"
        assert any(FLAG_EXTRA_WORDS in f.flags for f in r.findings)


def test_a_wrong_printed_reference_is_referred():
    r = _check(f'"{_v(39, 53)}" (Quran 39:54)')
    assert r.verdict == "REFER"
    assert r.findings[0].flags == [FLAG_CITATION_MISMATCH]
    assert r.findings[0].flag_detail[FLAG_CITATION_MISMATCH] == "39:54"


def test_a_printed_range_covering_both_verses_is_not_a_mismatch():
    r = _check(f'"{_v(39, 53)} {_v(39, 54)}" (Quran 39:53-54)')
    assert r.verdict == "CLEAR", [(f.ref, f.state, f.flags) for f in r.findings]


# ---------------------------------------------------------------- MEDIUM

INVENTED = '"Whoever helps a stranger on the road will be given a palace in the next life."'


def test_invented_text_with_a_reference_does_not_vanish():
    r = _check(INVENTED + " (Quran 2:255)")
    assert r.verdict == "REFER"
    assert r.findings[0].ref == "2:255" and FLAG_CITED_NOT_FOUND in r.findings[0].flags


def test_a_reference_to_a_verse_that_does_not_exist_is_referred():
    for ref in ("115:1", "2:287"):
        r = _check(INVENTED + f" (Quran {ref})")
        assert r.verdict == "REFER", ref
        assert r.findings[0].state == UNRESOLVED and FLAG_NO_SUCH_VERSE in r.findings[0].flags


def test_a_bare_reference_in_prose_is_not_a_quotation():
    r = _check("Scholars discuss this theme at length, see for example (Quran 3:7) and its commentary.")
    assert r.verdict == "CLEAR" and not r.findings


# ---------------------------------------------------------------- must still pass

def test_faithful_quotations_still_pass():
    v = _v(39, 53)
    cases = [
        f'Allah says: "{v}" (Quran 39:53)',
        f'"{v}"',                                              # no citation
        '"…' + " ".join(v.split()[6:20]) + '…" (Quran 39:53)',  # marked excerpt
        f'"{v.replace("mercy", "mer" + chr(0x200B) + "cy")}" (Quran 39:53)',  # invisible char
    ]
    for body in cases:
        r = _check(body)
        assert r.verdict == "CLEAR", (body[:60], [(f.state, f.flags) for f in r.findings])


def test_a_lookalike_letter_is_a_change():
    v = _v(39, 53).replace("mercy", "m" + chr(0x0435) + "rcy")     # Cyrillic е
    assert _check(f'"{v}" (Quran 39:53)').verdict == "REFER"


# ---------------------------------------------------------------- found on a never-seen article (2026-10-04)

def test_surah_name_references_are_read_and_stop_the_next_span():
    """"(an-Nahl 16:127)"-style references were not read: the next verse's span
    swallowed them and a faithful quotation came out NEAR."""
    yali = next(b for b, v in M.books.items() if v["title"].startswith("Yusuf Ali"))
    v1, v2 = M.verse(yali, 46, 35), M.verse(yali, 68, 48)
    for ref1 in ("(al-Ahqaf 46:35)", "[al-Ahqaf 46:35]", "( Ahqaf 46:35)", "(al-Ahqaf46:35)"):
        r = _check(f"Patience is commanded. {v1} {ref1} {v2} (al-Qalam 68:48)")
        got = [(f.ref, f.state, f.flags) for f in r.findings]
        assert r.verdict == "CLEAR" and got == [("46:35", MATCH, []), ("68:48", MATCH, [])], (ref1, got)


def test_shorthand_range_reference_covers_its_verses():
    r = _check(f'"{_v(39, 53)} {_v(39, 54)}" (az-Zumar 39:53-4)')
    assert r.verdict == "CLEAR", [(f.ref, f.state, f.flags) for f in r.findings]


def test_an_unclosed_quotation_mark_does_not_swallow_the_article():
    """An author who never closes a quotation must not turn every later
    quotation into "extra words"."""
    yali = next(b for b, v in M.books.items() if v["title"].startswith("Yusuf Ali"))
    body = (f'The first lesson. “{M.verse(yali, 31, 17)} (Luqman 31:17) '
            f'The second lesson is about steadfastness. {M.verse(yali, 3, 200)} (Al Imran 3:200) '
            f'And the third. “{M.verse(yali, 13, 24)}” (ar-Ra’d 13:24)')
    r = _check(body)
    assert all(FLAG_EXTRA_WORDS not in f.flags for f in r.findings), \
        [(f.ref, f.flags, f.flag_detail) for f in r.findings]


# ---------------------------------------------------------------- every language

PROSE = {
    "en": "In this article we reflect on a passage. ",
    "ur": "اس مضمون میں ہم ایک حوالے پر غور کرتے ہیں۔ ",
    "bn": "এই প্রবন্ধে আমরা একটি অংশ নিয়ে ভাবি। ",
    "hi": "इस लेख में हम एक अंश पर विचार करते हैं। ",
    "tl": "Sa artikulong ito ay pinag-iisipan natin ang isang talata. ",
}


def test_approved_verses_quoted_verbatim_pass_in_every_language():
    """The other side of the strict rule: a verse copied word for word from
    any approved edition — with its leading verse number, translator
    parentheses and all — must still be MATCH and the document CLEAR."""
    import random
    rng = random.Random(20261004)
    for lang, prose in PROSE.items():
        det = SpanDetector(M, lang, semantic=False)
        eds, n = M.editions(lang), 0
        while n < 8:
            b, s = rng.choice(eds), rng.randint(1, 114)
            rows = M.con.execute("SELECT ayah, text FROM ayat WHERE book_id=? AND surah=?",
                                 (b, s)).fetchall()
            a, txt = rng.choice(rows)
            if not txt or len(txt.split()) < 6:
                continue
            n += 1
            r = check_document(M, f'{prose}"{txt}" (Quran {s}:{a}). {prose}', lang, detector=det)
            got = [(f.ref, f.state, f.flags) for f in r.findings]
            assert r.verdict == "CLEAR" and (f"{s}:{a}", MATCH, []) in got, (lang, b, s, a, got)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
