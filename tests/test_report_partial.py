#!/usr/bin/env python3
"""
Regression tests for two report-layer rules.

1. Partial quotations. Writers quote one clause of a verse; the index stores
   whole verses. A clause may be upgraded to MATCH only when it is near-verbatim
   approved wording (calibration table in report.py above PARTIAL_MIN_WORDS).
   It may never be upgraded to NEAR, because at clause level a small edit of
   approved wording is indistinguishable from a different translator.

2. Arabic aliases. «اللَّهُ لَا إِلَٰهَ إِلَّا هُوَ الْحَيُّ الْقَيُّومُ» opens both
   2:255 and 3:2. When the author cites 2:255 and the translation places it,
   the Arabic matcher's 3:2 must not be reported as "quoted but not located".

Runs against the real corpus. Runnable with pytest or plain python3.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mizan.engine import Mizan                     # noqa: E402
from mizan.report import (MATCH, UNATTRIBUTED,     # noqa: E402
                          PARTIAL_MIN_WORDS, build_finding, check_document)

M = Mizan()
PICKTHALL = next(b for b, v in M.books.items() if v["title"].startswith("Pickthall"))


def _pickthall_clause(surah: int, ayah: int, n_words: int) -> str:
    """A verbatim opening clause of Pickthall's rendering, read from the index."""
    return " ".join(M.verse(PICKTHALL, surah, ayah).split()[:n_words])


def test_verbatim_clause_is_match_and_partial():
    clause = _pickthall_clause(2, 255, 11)   # "Allah! There is no deity save Him, ..."
    f = build_finding(M, clause, 2, 255, "en")
    assert f.state == MATCH, f
    assert f.partial is True
    assert f.attributed_to == PICKTHALL
    assert f.approved_excerpt and clause.split()[0] in f.approved_excerpt
    # The full verse is still carried for context.
    assert f.approved_text == M.verse(PICKTHALL, 2, 255)


def test_fresh_retranslation_is_not_upgraded():
    fresh = ("Allah, there is no god but Him, the Living One who sustains "
             "everything. Drowsiness never takes hold of Him.")
    f = build_finding(M, fresh, 2, 255, "en")
    assert f.state == UNATTRIBUTED, f
    assert f.partial is False


def test_modified_clause_is_never_upgraded_to_near():
    # "God" for "Allah" plus "Indeed" for "Lo!": resemblance, not identity.
    modified = "Indeed, the likeness of Jesus with God is as the likeness of Adam."
    f = build_finding(M, modified, 3, 59, "en")
    assert f.partial is False
    assert f.state != MATCH


def test_short_clause_is_never_upgraded():
    short = _pickthall_clause(2, 255, PARTIAL_MIN_WORDS - 1)
    f = build_finding(M, short, 2, 255, "en")
    assert f.partial is False


def test_arabic_alias_is_not_reported_as_missing():
    ar = ("ومن عظيم ما وصف الله به نفسه قوله: ﴿اللَّهُ لَا إِلَٰهَ إِلَّا هُوَ "
          "الْحَيُّ الْقَيُّومُ﴾ [البقرة: ٢٥٥]، وهي آية الكرسي.")
    en = ('Among the greatest of His descriptions of Himself: "Allah! There is '
          'no deity save Him, the Alive, the Eternal." (Quran 2:255). This is '
          'the Verse of the Throne.')
    r = check_document(M, en, "en", arabic_text=ar)
    refs = [f.ref for f in r.findings]
    assert refs == ["2:255"], refs
    assert r.findings[0].state == MATCH
    assert r.verdict == "CLEAR"


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
