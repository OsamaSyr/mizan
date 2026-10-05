#!/usr/bin/env python3
"""
MIZAN — the demo's extra samples (web/app.js, MORE_SAMPLES) keep their outcome.

A judge clicks these buttons first. Each one is meant to show a different
outcome, so each test below pins what the sample must return through the
app's own pipeline (`app.run_check`): the document verdict and every finding's
verse, state and flags. The sample text is read from web/app.js itself, so a
sample cannot drift away from its test.

The two samples whose quotation is found only by the AI tier (s5, and the last
quotation of L1) need the semantic model; without it they are skipped or
checked without that quotation.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from mizan import semantic  # noqa: E402

AI = semantic.available()


def _samples():
    js = open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8").read()
    start = js.index("/* samples:start */") + len("/* samples:start */")
    return json.loads(js[start:js.index("/* samples:end */")])


SAMPLES = _samples()
_APP = []


def _app():
    """app.py imported in-process, with its own throwaway review database."""
    if not _APP:
        os.environ.setdefault("MIZAN_REVIEW_DB", os.path.join(
            tempfile.mkdtemp(prefix="mizan-samples-"), "review.sqlite"))
        sys.path.insert(0, ROOT)
        import app  # noqa: WPS433
        _APP.append(app)
    return _APP[0]


def _run(key):
    s = SAMPLES[key]
    d = _app().run_check(s["en"], s["ar"], s["lang"], "practising")
    return d["document_verdict"], [(f["ref"], f["state"], f.get("flags") or []) for f in d["findings"]]


def _expect(key, verdict, findings):
    got_verdict, got = _run(key)
    assert (got_verdict, got) == (verdict, findings), (key, got_verdict, got)


# -- with the Arabic source ----------------------------------------------------

def test_s4_one_word_flip_is_referred():
    _expect("s4", "REFER", [("4:48", "NEAR", [])])


def test_s11_verse_quoted_in_arabic_but_missing_from_the_translation():
    _expect("s11", "REFER", [("49:10", "UNATTRIBUTED", [])])


def test_s12_urdu_verbatim_is_attributed():
    _expect("s12", "ATTRIBUTED", [("2:153", "MATCH", [])])


def test_L2_long_bilingual_article_all_attributed():
    _expect("L2", "ATTRIBUTED", [("21:107", "MATCH", []), ("3:159", "MATCH", []),
                                 ("49:10", "MATCH", []), ("16:90", "MATCH", []),
                                 ("17:23", "MATCH", [])])


# -- English only --------------------------------------------------------------

def test_s13_verbatim_without_a_verse_number_is_attributed():
    _expect("s13", "ATTRIBUTED", [("49:10", "MATCH", [])])


def test_s5_reworded_quotation_found_by_the_ai_tier():
    if not AI:
        return  # needs the semantic model
    verdict, got = _run("s5")
    assert verdict == "REFER" and got == [("39:53", "UNATTRIBUTED", [])], (verdict, got)


def test_s6_wrong_printed_verse_number_is_referred():
    _expect("s6", "REFER", [("21:107", "MATCH", ["citation_mismatch"])])


def test_s7_words_added_inside_the_quotation_marks_are_referred():
    _expect("s7", "REFER", [("94:5", "MATCH", ["extra_words_in_quote"])])


def test_s8_unapproved_translation_is_not_attributed():
    _expect("s8", "REFER", [("57:11", "UNATTRIBUTED", [])])


def test_s9_reference_to_a_verse_that_does_not_exist():
    _expect("s9", "REFER", [(None, "UNRESOLVED", ["no_such_verse"])])


def test_s10_prose_without_quotations():
    _expect("s10", "NO_QUOTES", [])


def test_L1_long_article_with_mixed_outcomes():
    verdict, got = _run("L1")
    expected = [("2:153", "MATCH", []), ("2:155", "MATCH", []), ("94:5", "MATCH", []),
                ("94:6", "MATCH", []), ("2:152", "NEAR", []), ("39:10", "NEAR", [])]
    if AI:
        expected.append(("39:53", "UNATTRIBUTED", []))
    else:
        got = [g for g in got if g[0] != "39:53"]
    assert verdict == "REFER" and got == expected, (verdict, got)


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
