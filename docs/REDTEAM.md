# Red-team review — 2026-10-03

An independent reviewer — an AI agent that had not taken part in building
MIZAN — was asked to break it on the properties the challenge's binding
standard requires (scientific package, p. 5–6):

* «ألا ينسب نص أو قول إلى مرجع لا يوجد فيه» — never attribute a text to a
  source it is not in;
* for a misquoted verse: «التنبيه على النص الصحيح بلطف، وإظهار السورة والآية،
  **وعدم البناء على النص المحرف**»;
* abstain or refer when unsure; never adapt rulings; disclose AI involvement.

It found real defects. This file records what was found, what was fixed, and
how each fix is held in place by a test. It is kept in the repository on
purpose: a tool that claims to catch altered scripture should show what it
used to miss.

## Method

Approved verse text was read from the index at runtime and edited one word at
a time, then wrapped in neutral prose of our own, with and without a printed
reference. 38 attack documents ran on the deterministic path before the review
session was stopped; the rest of the findings came from reading the code and
were reproduced afterwards. The attacks are now `tests/test_redteam.py`
(20 tests) plus app-level tests in `tests/test_app.py`.

## Findings and fixes

| # | Severity | Finding (before) | Fix | Test |
|---|---|---|---|---|
| 1 | CRITICAL | A one-word meaning flip of an approved verse came out **MATCH** and the document **CLEAR** — 18 of 18 (e.g. 4:48 "does not forgive" → "does forgive", 0.980; 65:12 seven → six heavens, 0.955). Cause: MATCH was `sim ≥ 0.92`, and one word in a 15+ word verse moves `sim` by less than 0.08. | MATCH now also requires **word identity** with an approved rendering (`engine.verbatim`). Similarity still ranks and locates; identity decides. | `test_one_word_meaning_flips_are_referred`, `test_the_unedited_verses_still_pass` |
| 2 | CRITICAL | Words in parentheses were invisible: `normalize()` deletes `(...)` on both sides, so "He (never) forgives" scored 1.000. | Parenthesised words count in the identity check. Only the *approved* side may drop its translator's glosses. | `test_a_parenthesised_insertion_is_a_change` |
| 3 | HIGH | NEAR (close but modified) passed the gate as «مُسنَد مع تعديل». | **NEAR now refers the document.** It names its closest approved translation for comparison — never as the source — and shows the differing words. | `test_records_without_a_refer_flag_fall_back_to_the_state_list`, `test_a_changed_word_refers_and_says_why` |
| 4 | HIGH | A verse quoted twice: the second copy was silently dropped (overlap resolution kept one span per verse id), so a verbatim copy hid an altered one. | Spans are de-duplicated by position, not verse id. | `test_a_repeated_verse_is_checked_twice` |
| 5 | HIGH | Invented words inside the same quotation marks as a real verse were trimmed away as "prose" → MATCH, CLEAR. | Words inside the author's quotation marks that are not part of a located verse refer the quotation (`extra_words_in_quote`). | `test_invented_words_inside_the_quotation_marks_are_referred` |
| 6 | HIGH | A wrong printed reference (39:53 printed as 39:54) passed. | `citation_mismatch` flag; and when the printed number pulls detection onto a shared fragment, the quoted text is re-identified and reported as the verse it really is. A printed range (39:53-54) is honoured. | `test_a_wrong_printed_reference_is_referred`, `test_a_printed_range_covering_both_verses_is_not_a_mismatch` |
| 7 | MEDIUM | "." → "?" (statement turned into a question) → MATCH 1.000. | The question mark is a token in the identity check. | `test_statement_turned_into_question_is_a_change` |
| 8 | MEDIUM | Invented text with a printed reference — `(Quran 2:255)`, `(Quran 115:1)` — vanished as "no quotations", gate CLEAR. | Quoted text next to a reference always yields a finding: `cited_text_not_found` for a real verse, `no_such_verse` (UNRESOLVED) for a reference to a verse that does not exist. A bare reference in prose ("see 3:7") is still not a quotation. | `test_invented_text_with_a_reference_does_not_vanish`, `test_a_reference_to_a_verse_that_does_not_exist_is_referred`, `test_a_bare_reference_in_prose_is_not_a_quotation` |
| 9 | MEDIUM | Spans whose verse was proposed by the AI model but confirmed by word windows were labelled plain "lexical" — the AI disclosure could be missing. | New tier `lexical+semantic`, labelled «مطابقة نصية باقتراح دلالي — ذكاء اصطناعي»; it triggers the disclosure. | covered by the disclosure test in `tests/test_app.py` |
| 10 | LOW | A body of 100,000 `[` raised an uncaught `RecursionError` (no HTTP response). | Answered as `BAD_JSON`. | `test_input_errors_are_arabic_and_actionable` |
| 11 | INFO | Any 2–3 letter language code was accepted; `fr` with a page of verses → "no quotations", CLEAR. | A language with no approved translation indexed needs the Arabic source; otherwise `LANG_NOT_INDEXED`. | `test_input_errors_are_arabic_and_actionable` |

Clarity-panel and terminology-panel findings (rulings that slipped past the
refusal word lists, splitting a conditional, whitespace inside a quotation,
reductive glossary usage) are recorded in [CLARITY.md](CLARITY.md) and
[TERMS.md](TERMS.md) with their own before/after measurements.

## The other direction: faithful quotations must still pass

A strict rule is only useful if it does not refer faithful text. Verses
copied verbatim from the 22 approved editions — with their leading verse
numbers, translator parentheses and footnote markers — were placed in prose
with a printed reference: **499 of 500** random cases (100 per language)
came out MATCH with the document CLEAR. This check found and fixed one more
defect in the new rule: a quoted verse starting with its number
(`"36. It is not…`) was compared with the number still attached and came out
NEAR. Without a printed reference, 465 of 500 pass; the misses are very short
verses (not located at all) and word-identical refrains (55:13 = 55:16),
which are named by their twin.

## What held

* **No fabricated scripture.** Every approved text shown is read from the
  index by `Mizan.verse`; excerpts are cut from it. Nothing is generated.
* **Escaping and request guards** (code review): every rendered string is
  escaped, CSPs are strict, Host/Origin/content-type are checked, the 512 KB
  cap applies before the body is read, chunked bodies are refused, report ids
  are random 16-hex tokens, static paths are confined.
* **No instruction can steer the verdict.** Text such as a sentence addressed
  to "the checker" inside a document is just text: no language model reads
  the document to make a decision. The embedding model only proposes verse
  ids; the state is a deterministic string comparison.

## What the identity rule ignores — and why

Only differences that cannot change wording: letter case; punctuation other
than the question mark; footnote markers like `[3]`; a leading verse number
and a bracketed one like `(56)`;
invisible format characters (zero-width space, joiners); Arabic diacritics and
letter-form variants (ی/ي, ک/ك, ہ/ه); Latin accents in transliteration
(Allâh = Allah, Muḥammad = Muhammad). Indic vowel signs are **not** ignored —
they change words. A marked omission ("…") is allowed when every marked
segment is verbatim and in order. A look-alike letter from another alphabet
(Cyrillic е for Latin e) is a change and is referred.

## Measured effect on real published text

The 473 real quotations (`tests/real_corpus.py`, deterministic path):

| | before | after |
|---|---|---|
| located, as published | 237 (50.1 %) | 237 (50.1 %) — detection unchanged |
| MATCH, as published | 30 | 14 |
| NEAR, as published (now referred) | 32 | 41 |
| UNATTRIBUTED, as published | 175 | 182 |
| MATCH, citation stripped | 25 | 10 |
| MATCH on prose with no quotation (false attribution) | 0 of 120 paragraphs | 0 of 120 |

The 15 quotations that lost MATCH in the citation-stripped run (the per-item
list in `tests/results_real.json`) were each inspected: every one differs from
the approved translation in its words — "God" printed for "Allah", "you" for
"ye", an added "(O Muhammad)", "ईश्वर" for "अल्लाह", or a passage that runs into
the next verse. Under the old rule these were passed as matching an approved
translation; under the binding standard they are not that translation's
words, so they go to a reviewer with the differing words shown. The two
findings the probe used to count as "false attributions" were both NEAR; they
are now referrals, and MATCH on prose was 0 before and after.
