# Panel 3 — Clarity and audience fit

`src/mizan/clarity.py` · tests `tests/test_clarity.py` · stdlib only · no model

---

## 0. Why this panel exists

> **Update 2026-10-04:** with the organisers' permission MIZAN moved to
> **Track 4** (knowledge and verification tools), whose criterion this panel
> does not serve; it is now hidden in the interface by default. The rationale
> below is kept as written for the record.

MIZAN was registered in **Track 2**. That track's success criterion has two
clauses joined by «وفي الوقت نفسه» — *and at the same time*:

> «هل حافظ الحل على المعنى والدلالة الشرعية ودقة المصطلحات، **وفي الوقت نفسه**
> حسّن وضوح المحتوى وملاءمته للجمهور المستهدف دون تغيير جوهره؟»

Panels 1–2 (`engine.py`, `detect.py`, `report.py`) answer clause one: every
quotation is traced to a named approved published translation by deterministic
string comparison, and no model is ever allowed near scripture.

Clause two — *improved the clarity of the content and its fit for the target
audience* — is this file. **Without it, MIZAN is a Track 4 provenance tool
submitted to Track 2.**

The clause that governs how it is allowed to do that is the third one: **دون
تغيير جوهره** — without changing its substance. Everything below is a
consequence of taking that literally.

---

## 1. What it does

```python
import sys; sys.path.insert(0, "src")
from mizan.engine import Mizan
from mizan.report import check_document
from mizan.clarity import adapt_document, CURIOUS

m = Mizan()
rep = check_document(m, translation_text, "en", arabic_text=arabic)
clarity = adapt_document(translation_text, CURIOUS, findings=rep.findings,
                         lang="en")       # the language of translation_text

print(clarity.summary)
print(clarity.refusal_rate)              # 0.0 – 1.0
clarity.as_dict()                        # JSON-serialisable for app.py
```

Pipeline, in order, with no branch that can be skipped:

| Step | What happens | Where |
|---|---|---|
| 1 | Split into sentences by **character offsets** | `sentence_spans` |
| 2 | Classify each `QUOTED` / `PROSE` | `segment` |
| 3 | Run the **allow-list gate** on prose: lexical patterns, language screen, **scripture guard** against the index | `refusal_reasons` (`lexical_reasons`, `language_reasons`, `scripture_reasons`) |
| 4 | **Freeze** quotations + approved terms to opaque tokens | `freeze` |
| 5 | Adapt the masked text (deterministic, or model on Oct 5) | `_split_at_conjunctions` / `model_adapt` |
| 6 | **Thaw and verify byte-exact**, or roll back entirely | `thaw`, `verify_restoration` |
| 7 | **Re-gate the output** — adaptation must not have created a ruling, a condition or scripture-like wording | `refusal_reasons` again |
| 8 | Rebuild the document **by character offset** and verify every frozen span against the **final** text | `verify_document` |
| 9 | Measure readability before/after; emit editor suggestions | `readability` |

---

## 2. The allow-list gate, and why it is an allow-list

Every other "AI content improver" is a **block-list**: rewrite everything, then
try to catch the cases you should not have touched. In religious text that is
the wrong default, for two reasons:

1. **The failure is silent.** A rewrite that turns *"Muslims pray five times a
   day"* into *"Many Muslims pray five times a day"* reads better, passes every
   grammar check, and has quietly demoted a pillar of Islam to a statistical
   observation. Nothing in the output signals that it happened.
2. **The failure is theological, not cosmetic.** There is no automated test for
   "this sentence now implies something different about the religion."

So Panel 3 inverts the default. A sentence is **passed through unchanged unless
it positively proves it is ordinary narrative prose**: no scripture, no
citation, no normative force, no attributed position, no creed statement.

### The refusal categories

| Code | Refused because | Example |
|---|---|---|
| `SCRIPTURE` | Overlaps a detected quotation, or sits inside quote marks `" " “ ” « » ﴾ ﴿` — in the sentence itself or in a quotation that opens or closes in another sentence | any verse |
| `SCRIPTURE_LIKE` | Shares wording with an approved translation in the index although neither quoted nor cited (the scripture guard, §11.3), or uses archaic scriptural register (*thee, thou, hath, unto, verily*) | an undetected 20–30 word run from a long verse |
| `SCRIPTURE_UNCHECKED` | The index is not available, so an unquoted verse cannot be ruled out — **fail safe** | no `data/corpus.sqlite`; a language with no approved translation indexed |
| `REFERENCE` | Carries a verse or hadith citation, or frames one in words — the wording *is* the attribution | `"…plainly in Quran 51:56."` · `"Narrated by al-Bukhari…"` · *"as the Quran states"* · *"the Prophet, …, said"* · `رواه مسلم` |
| `NORMATIVE` | Ruling language: modals, the vocabulary of the five rulings and of sin | must, shall, should (any subject), may not, compulsory, mandatory, obligatory, required, forbidden, prohibited, (un)lawful, permissible, allowed, sin(ful), haram, halal · `يجب` `لا يجوز` `حرام` `واجب` `إثم` |
| `IMPLICIT_OBLIGATION` | A ruling dressed as a description, with no modal | *"A Muslim prays five times a day."* · *"Women cover…"* · *"Parents teach…"* |
| `CONDITIONAL` | A condition governs clauses a split could separate from it | *"If A, B, and C"* · *"…, provided that…"* · *"Whoever…"* · *"…except X, and Y"* |
| `PREFERENCE` | Ranking claim — a juristic judgement, not an editorial one | *"It is better to recite slowly."* · `الأفضل` |
| `ATTRIBUTION` | Names a scholar or school as holding a position | *"According to the majority…"* · `جمهور العلماء` |
| `CREED` | Speaks about God, prophethood, revelation, the unseen, the hereafter, or another faith's doctrine (level ب/ج) — a topic test, not a phrase list | *"There is no god worthy of worship except Allah"* · *"Jesus was not crucified"* · *"…depends on Him"* · *"…leads to the Fire"* |
| `LANGUAGE_UNSCREENED` | The patterns above read English only; in any other language (or script) the gate cannot tell a ruling from narrative | every prose sentence of an Urdu, Hindi, Bengali or Tagalog document |
| `TOO_SHORT` | Under 4 words — no measurable clarity problem to fix | — |
| `ROLLBACK` | Adaptation ran but failed the byte-exact check | see §4 |

Two design choices are load-bearing:

- **`refusal_reasons()` returns *all* reasons, never short-circuiting.** "Refused
  for three independent reasons" is stronger evidence of conservatism than
  "refused."
- **`refusal_reasons()` takes no `level` argument.** This is structural, not a
  convention: there is no code path by which selecting an audience could unlock
  a sentence the gate rejects. (It takes `lang`, which only chooses the
  language screen and which approved translations the scripture guard reads.) `test_a_level_cannot_unlock_a_refused_sentence`
  and `test_all_four_levels_run_and_never_disagree_on_substance` pin it down.

### `IMPLICIT_OBLIGATION` deserves its own note

This is the subtle one and the reason the panel is worth building. The
signature is: a religious-role subject (*a Muslim*, *every believer*), followed
within ~8 words by a habitual present-tense act-of-worship verb, with no modal
anywhere. The window matters — without it, *"Muslims in Spain built mosques,
and travellers pray on the road"* would match a subject from clause one against
a verb from clause three.

Past-tense narration is explicitly excluded (`was`/`were`/`had`/`used to`), so
history is not mistaken for a ruling.

The pattern is deliberately **broad**. A false refusal costs a pass-through; a
false acceptance costs the product.

---

## 3. Refusal is the headline, not the footnote

The official scientific package binds every submission to:

> «مقاومة الهلوسة — عند غياب المرجع الكافي أو انخفاض الثقة، تكون الأولوية
> **للامتناع** أو التحفظ أو الإحالة، لا لتوليد إجابة غير موثقة.»
> *(Abstention takes priority.)*

So the output does not hide the refusal rate — it leads with it:

> **"MIZAN declined to adapt 81.7% of the prose in this document, and here is
> each reason."**

A *low* number here would be the worrying result. The panel reports
`refusal_rate`, a per-code `refusal_counts` breakdown, and a human-readable
message per code, because the refusals are the evidence that clause three
(*دون تغيير جوهره*) is being honoured.

### Measured, on real published material

8,195 words of real English da'wah prose (213 distinct paragraphs) from the
real-corpus publishers (`data/real/corpus.jsonl`, the `context_before` /
`context_after` fields), 333 sentences. Source of these figures:
`results/SUMMARY.md`, section *Clarity panel*, produced by `make eval`:

| Level | Prose | Refused | **Refusal rate** | Adapted | Flesch | Byte-exact |
|---|---|---|---|---|---|---|
| `curious` | 333 | 272 | **81.7%** | 7 | 52.4 → 52.9 | ✅ |
| `new_muslim` | 333 | 272 | **81.7%** | 3 | 52.4 → 52.6 | ✅ |
| `practising` | 333 | 272 | **81.7%** | 3 | 52.4 → 52.6 | ✅ |
| `daee` | 333 | 272 | **81.7%** | 2 | 52.4 → 52.5 | ✅ |

Breakdown at `curious`: `CREED` 230 · `REFERENCE` 78 · `SCRIPTURE` 64 ·
`SCRIPTURE_LIKE` 51 · `NORMATIVE` 41 · `CONDITIONAL` 21 ·
`IMPLICIT_OBLIGATION` 11 · `PREFERENCE` 8 · `TOO_SHORT` 4. (A refused sentence
can carry more than one reason, so the codes sum to more than 272.)

Until 2026-10-04 this table read 84 refused (25.2%), 27 adapted, Flesch
52.4 → 54.5. That number was low because the gate was missing most ruling and
creed vocabulary, not because the prose was safe: §11 lists what passed, and
why each of the 188 newly refused sentences is refused. This publisher prose
is creed-dense da'wah writing; 170 of the new refusals are sentences about God,
the angels, Paradise, decree or prophethood.

**The refusal rate is identical across all four levels.** That is the
structural guarantee of §2 showing up as a measurement, not a claim.

On a denser hand-built sample of the kind of paragraph a da'wah publisher
actually sends (`SAMPLE` in the test file — normative, preference, attribution,
citation and narrative in realistic proportions), the rate is **56%**
(unchanged).

**Other languages.** In Urdu, Hindi, Bengali and Tagalog the rate is **100%**
by construction (`LANGUAGE_UNSCREENED`, §11.4): the gate cannot read rulings
there, so nothing is adapted or suggested.

---

## 4. The frozen-token guarantee

This is the product. Not the quality of the rewrite — the proof that the
rewrite could not have touched what it must not touch.

### Mechanism

Before any adaptation, every quotation span and every approved shar'i term is
replaced by an opaque placeholder:

```
␂MZN0␃        (U+2402 SYMBOL FOR START OF TEXT … U+2403 … END OF TEXT)
```

Chosen so that it survives word-splitting as one token, cannot occur in natural
text, and is trivially greppable at 3am on Oct 5.

**The adapter never sees the scripture.** `model_adapt` receives the *masked*
sentence. A model physically cannot rewrite a verse it was never shown — this
is a structural property, not a promise, and it is the same discipline
`detect.semantic_candidates` uses (ids and scores only, never text).

### Verification

`thaw()` raises `FrozenTokenViolation` if any placeholder was **dropped**,
**duplicated**, or if an **unknown** placeholder appeared. All three mean the
same thing: adaptation touched the mask.

`verify_restoration()` then compares **UTF-8 bytes**, not `str`. The encoding
step is deliberate — a transform that silently NFC-normalised an Arabic string
would compare equal under some `str` comparisons but is a different byte
sequence on disk, and the index MIZAN compares against is byte-oriented.

Note it does *not* require `original == restored`; the surrounding prose is
supposed to change. It requires each frozen string to appear in the output
**exactly as many times as in the input, byte for byte**.

**The document-level claim is checked on the final text.** `adapt_document`
rebuilds its output from the original by character offset: each sentence's
output replaces exactly its own span, and everything between sentences
(paragraph breaks, double spaces, a line break inside a multi-sentence
quotation) is copied as-is. `verify_document` then checks every quotation span
**whole** — not its per-sentence pieces — plus every frozen term, against
that final text, and that no placeholder leaked. `frozen.verified_byte_exact`
is that result; `adapted_text` in `as_dict()` is the text it was checked on.
(Until 2026-10-04 the output was `" ".join(sentences)` and the check ran on
pieces, so a quotation's line break became a space while the report said
byte-exact — §11.5.)

### Rollback is total

On any violation the **whole sentence is discarded** and the original is
returned, flagged `ROLLBACK`. Not a repair. Not a partial accept. Not a
best-effort merge.

There is no state in which `Sentence.adapted` holds unverified text.

### The second rollback trigger

After restoration, the output is **re-run through the gate**. If adaptation
introduced normative, preference, attribution or creed language, the sentence
rolls back. A future model that "helpfully" turns *"many Muslims pray at dawn"*
into *"Muslims must pray at dawn"* is caught here, in the harness — not in
production.

This is tested with a deliberately misbehaving adapter, two ways
(`test_frozen_violation_forces_a_full_rollback`,
`test_rollback_also_fires_when_adaptation_introduces_a_ruling`).

---

## 5. Adaptation: suggest, don't rewrite

Deterministic transforms only. No model, no sampling, no network.

**Applied automatically — exactly one transform:**

- **`SPLIT`** — an over-long sentence is split at a *coordinating* conjunction
  (`and`, `but`, `so`, `yet`) preceded by a comma. Subordinating conjunctions
  (`because`, `although`, `which`) are excluded: splitting there strands a
  dependent clause and can invert the logical relation.

  The conjunction is **kept** (`"X, and Y"` → `"X. And Y"`). Dropping it reads
  marginally better but would mean the output is no longer a strict
  content-superset of the input, and *"no word was removed"* is a claim worth
  being able to make without an asterisk. `test_splitting_never_adds_or_removes_a_content_word`
  compares the full word multiset before and after.

  The only character-level edits are: the comma becomes `.` and the
  conjunction's first letter is capitalised. They are made in place by
  character offset, so every other character — line breaks and double spaces
  included — is carried over exactly
  (`test_a_split_changes_only_the_comma_and_the_capital`).

  A sentence a condition governs is never split, even by a caller that skips
  the gate: *"If A, B, and C"* split at ", and" would state C unconditionally.

**Flagged for the editor — never applied:**

| Code | What it surfaces |
|---|---|
| `LENGTH` | Sentence exceeds the level's target |
| `PASSIVE` | Probable passive construction (adjectival participles excluded) |
| `NOUN_CHAIN` | N+ long content words stacked with no connector |
| `GLOSS` | First use of an approved shar'i term, with the package's constraint |

These require a judgement about meaning — exactly the judgement this system is
built to refuse to make on its own.

### The glossary is frozen, not simplified

The frozen terms come from two sources. The package's ten terms from
«نماذج لقاموس المصطلحات الأساسية» always win, each carrying its
**ضابط الاستخدام** — because the constraint is *the reason the term is frozen*.
On top of them, every transliteration in the الجمهرة glossary under
`data/glossary/` is frozen too (343 surface forms in total). If that directory
is missing, clarity falls back to the ten package terms alone. See
`docs/TERMS.md` §7 for how the glossary was collected and reviewed.

The package's ten:

| Term | Approved rendering | Constraint |
|---|---|---|
| Tawhid | Tawhid / Oneness of God | Keep the term; do not reduce to numerical oneness |
| Sharia | Sharia / Islamic law and guidance | Do not reduce to penal law |
| Worship | Worship | Heart, speech and limbs — not only rites |
| … | | `GLOSSARY` in `clarity.py` |

*Sharia* is frozen not because it is hard to read but because the package
explicitly forbids reducing it to criminal law. Freezing it and **suggesting a
gloss** is correct; silently swapping in "Islamic law" is the exact substance
change clause three prohibits. The package is also explicit that the Jumhara
dictionary **takes precedence over automatic translation** in sensitive terms.

Terms are glossed on **first use per document** only.

---

## 6. Audience levels

Four levels, mapped from the package's four content tiers (§8 of
`TECHNICAL_SPEC.md`) read as **reader profiles**:

| Level | Reader | `max_words` | Passive | Gloss terms |
|---|---|---|---|---|
| `curious` | No prior exposure | 18 | ✅ | ✅ |
| `new_muslim` | Recent convert | 22 | ✅ | ✅ |
| `practising` | Reads this regularly | 28 | ✅ | — |
| `daee` | The da'i drafting it | 34 | — | — |

This serves the package's **الجودة الدعوية** clause: «تراعى خلفية المخاطَب،
ومستواه، ولغته، وسياقه».

**A level changes HOW MUCH is flagged and split. It never changes what is
claimed, which sentences are eligible, or any refusal.** See §2 and the
measured table in §3 — all four levels refuse the identical 272 sentences.
The scripture guard and the language screen are level-free too.

---

## 7. Mapping to the Track 2 criterion, clause by clause

| Clause | How Panel 3 satisfies it |
|---|---|
| «حافظ على المعنى» — preserved the meaning | Only transform applied is a split at a coordinating conjunction, proven content-preserving by word-multiset comparison. Everything else is a suggestion to a human. |
| «والدلالة الشرعية» — and the shar'i signification | Normative, preference, attribution and creed sentences are refused outright. Implicit obligations are refused — the case where meaning erodes silently. |
| «ودقة المصطلحات» — and terminological precision | The package's approved glossary is **frozen byte-exact** and carried through adaptation as an opaque token, with the ضابط الاستخدام surfaced to the editor. |
| «وفي الوقت نفسه حسّن وضوح المحتوى» — *and at the same time* improved clarity | Flesch Reading Ease measured before/after on every sentence and on the document (English only — no invented score for other scripts). Measured gain on real material: 52.4 → 52.9 at `curious`, smaller since the gate was corrected (§11); on a run-on sentence 23.9 → 41.6. |
| «وملاءمته للجمهور المستهدف» — and its fit for the target audience | Four audience levels with distinct thresholds, mapped from the package's own content tiers. |
| «دون تغيير جوهره» — without changing its substance | The allow-list gate, the scripture guard against undetected verses, the byte-exact frozen-token invariant with total rollback checked on the final text, and the output re-gate. Measured 81.7% refusal on real material. |

And against the binding scientific standard:

| Package requirement | Panel 3 |
|---|---|
| «قابلًا للتتبع إلى مصدره» | Every refusal carries a machine code + human reason; every suggestion names its span |
| «يفرّق بين النص الشرعي والشرح المولَّد» | `QUOTED` vs `PROSE` classification; scripture is frozen and never reaches the adapter |
| «الأولوية للامتناع» | Refusal is the default and the reported headline |
| «عدم الاستقلال بالفتوى» | Normative and creed language is refused, never authored or restated |
| «الترجمة والتوطين… دون تغيير المضمون إلرضاء توقعات الجمهور» | Levels change presentation only; the refusal set is level-invariant |

---

## 8. What the model layer adds on Oct 5

`model_adapt(sentence: str, level: str) -> str | None` raises
`NotImplementedError` today and carries a full contract in its docstring,
mirroring `detect.semantic_candidates`. The deterministic transforms are the
**documented fallback mode**, exactly as the lexical tier is for detection.

**What it will add.** The deterministic path can only split at a comma plus a
coordinating conjunction, and can only *flag* a passive or a noun stack. It
cannot de-passivise, cannot reorder a fronted subordinate clause, and cannot
simplify a Latinate word to a plain one. Those are the three edits that
actually move a Flesch score for a `curious` reader — and all three need a
language model. That is the honest justification for the model here, measured
against the same baseline, in the same shape as the §2 argument in
`TECHNICAL_SPEC.md`.

**What the contract requires.** Five guarantees the caller depends on:

1. **Token fidelity** — every `␂MZN<n>␃` appears exactly once, unmodified; no
   new token. Enforced by `thaw`.
2. **No new claims** — may reorder, split, de-passivise, simplify connectives.
   May **not** add a fact, attribution, ruling, number, name, date or
   scriptural allusion.
3. **No normative language introduced** — output is re-gated; violations roll
   back.
4. **Deterministic** for a given model version (temperature 0), so
   `--runs 3` reproduces.
5. **Offline-safe** — must *raise* if the provider is unreachable, never return
   degraded output. The caller catches and falls back.

**Wiring it is a one-line change at the call site** (`use_model=True`), not an
edit to this module.

Note the ordering, because it is the whole safety argument: the model is called
**after** the allow-list gate and **after** freezing. It only ever sees prose
that was already cleared, with scripture and approved terms already replaced by
opaque tokens. Then its output is verified byte-exact and re-gated before
anything is accepted.

---

## 9. Tests

```
python3 -m pytest tests/test_clarity.py -v
python3 tests/test_clarity.py
```

**63 tests, all passing** (~4 s with the index, no network, no model).

The suite is weighted towards the safety claims, not the readability one:

- a **real approved verse** pulled from `corpus.sqlite` at runtime survives the
  whole panel byte-exact, embedded in prose long enough to trigger every
  transform;
- scripture is pasted into **no** test file — doing so would create a second,
  unversioned copy that could drift from the index, the exact failure mode
  MIZAN exists to catch;
- every refusal category is tested, in English **and** Arabic;
- the false-positive direction is tested too (past-tense narration is not an
  obligation; plain narrative *is* eligible);
- frozen-token violation forces total rollback — tested with a misbehaving
  adapter, in both trigger modes;
- splitting is proven not to add or remove a content word;
- the report is checked to be JSON round-trippable;
- every defect in §11 has a regression test (section 9 of the test file),
  including verse runs read from the index in five languages.

The plain-python runner prints the measured refusal rate on every run.

### Integration with Panels 1–2

Verified end-to-end against the real engine on scraped material: **7/7
engine-detected quotations survived adaptation byte-exact**, with
`frozen_tokens_verified=True` on every document.

---

## 10. Known limits — stated before a judge finds them

- **English only.** The ruling, creed and condition patterns are English (plus
  a thin Arabic layer). In every other language, and for a foreign-script
  sentence inside an English document, the gate refuses everything
  (`LANGUAGE_UNSCREENED`) and the report's `language_support` says so.
  `readability()` returns `flesch: None` for anything that is not English
  rather than printing a fabricated number.
- **The scripture guard misses short fragments.** On 400 verse runs embedded
  in prose it altered none, but 5 run-bearing sentences (≥ 6 verse words, most
  under 10) stayed eligible with the detector's findings, 11 without. Bengali
  recall is the weakest (9/10 in the test; ~85% on 200 runs) because
  normalisation fragments the script and candidate retrieval misses the verse.
- **The English likeness signal labels religious prose.** 40 of 770 sentences
  of the no-quotation English paragraphs carry `SCRIPTURE_LIKE` from likeness
  alone (Quranic vocabulary, not verse text); all but 8 are refused for other
  reasons too. Kept for recall: it is what catches fragments the run test
  misses.
- **Passive detection is a heuristic.** A curated list of adjectival
  participles suppresses the common false positives, but it will over- and
  under-fire. It is a flag to a human, which is the correct severity for a
  heuristic.
- **Noun-chain detection is a word-length heuristic**, not POS tagging —
  stdlib only, no tagger available.
- **The syllable counter is crude.** This is sound for the claim actually made:
  a sentence is only ever compared against *itself* before and after, so a
  systematic bias cancels. No absolute grade-level claim against an external
  standard is made anywhere.
- **The gate cannot catch a normative or creed claim with no lexical marker.**
  A sentence that implies an obligation or a point of belief purely through
  context will pass — e.g. *"The life spans of all human beings are written
  and the amount of their sustenance apportioned"* (decree, no marker) is
  still eligible on the real corpus. This is the residual risk, and it is why
  the only automatic transform is one that provably cannot change content.
- **Refusal rate is corpus-dependent.** 81.7% on the real-corpus publisher
  prose (creed-dense), 56% on the hand-built sample, 100% in any language
  other than English. It is reported per document, never claimed as a
  universal constant.

---

## 11. What changed on 2026-10-04, and why

An independent review of this panel predicted six defects by reading the code.
Each was reproduced before anything was changed; all reproduced. Two more
came from a probe of other languages. Every fix has a regression test in
section 9 of `tests/test_clarity.py`. The rule applied throughout: no existing
refusal was loosened; over-refusal is acceptable, under-refusal is the failure.

### 11.1 Ruling and creed vocabulary (reproduced)

`refusal_reasons()` returned `[]` for each of these, and `adapt_sentence(...,
"curious")` split three of them:

| Sentence (review's examples) | Before | After |
|---|---|---|
| *"Zakat is compulsory for every adult who owns wealth above the threshold for a full year, and it is paid to the eight categories named in the law."* | `[]` → split *"…for a full year. And it is paid…"* | `NORMATIVE` |
| *"Jesus was not crucified, and he was raised up to Allah, as the Quran states clearly for those who reflect on it."* | `[]` (eligible; not split only because the first clause has 4 words) | `REFERENCE`, `CREED` |
| *"There is no god worthy of worship except Allah alone, and Muhammad is His final messenger to all of mankind."* | `[]` → split *"…except Allah alone. And Muhammad is…"* | `CREED`, `CONDITIONAL`, `SCRIPTURE_LIKE` |
| *"Women should wear loose clothing when they go out in public, and they avoid perfume that others can smell."* | `[]` → split | `NORMATIVE`, `IMPLICIT_OBLIGATION`, `CONDITIONAL` |

Probes of the vocabulary the review listed also passed: *mandatory*,
*unlawful*, bare *lawful*, *may not* (which the docstring claimed), *should*
after any subject but four pronouns; *sin/sins/sinful* passed unless a
scholar was also named. `_CREED` was six fixed phrases.

**Fix.** `_NORMATIVE` now matches the modals (any *should*, *must*, *may not*,
*needs to*), the vocabulary of the five rulings (*compulsory, mandatory,
obligatory, required, forbidden, prohibited, (un)lawful, permissible,
allowed, exempt, condemned…*), sin and punishment, and the transliterated
ruling names with their common spellings. `_CREED` became a **topic** test:
God and His oneness, prophethood and revelation, the unseen and the
hereafter, other faiths' doctrine, and claims about what Islam holds; plus
capitalised divine pronouns and names of the unseen (*Him*, *the Fire*),
case-sensitively so *"the fire spread through the market"* stays prose.
Honorific blessing formulas (*"may the mercy and blessings of God be upon
him"*) are blanked for the topic test only — they are a courtesy, not a
claim. `_REFERENCE` also catches a citation framed in words (*"as the Quran
states"*, *"the Prophet, …, said"*). `_IMPLICIT_SUBJECT` gained family and
rite roles (women, men, husband, wife, parents, children, pilgrims…) and now
tries every subject in the sentence, not only the first.

Found on the way and fixed: `washes?` matched "washe"/"washes" but never
"wash" (so *"Muslims wash…"* was never an implicit obligation; same for
*teach*); the optional article could attach with no space, so "amen" read as
"a men".

### 11.2 Conditions (reproduced)

*"If a woman is menstruating, prayer is not performed, and the missed fasts
are made up after Ramadan ends."* → `[]`, split into *"…prayer is not
performed. And the missed fasts are made up after Ramadan ends."* — the
second clause left the condition and became an unconditional statement.

**Fix.** New code `CONDITIONAL`, level-free: a sentence that opens with a
condition (*if, when, unless, until, whoever, provided, as long as, except,
in case, once…*), introduces one after a comma, or has one anywhere before a
", and/but/so/yet" is refused. A condition inside the last clause with no
comma before it governs only that clause and is left alone. The split
function refuses a governed sentence as well, for any caller that skips the
gate.

### 11.3 The scripture guard (reproduced)

An uncited, unquoted run of 20–30 words from an approved translation is not
in the frozen spans if the detector misses it. Measured with verse runs read
from the index at run time and embedded in neutral prose: of 40 runs, clarity
on its own split 35 inside approved wording, and **11 even with the real
detector's findings** (e.g. *"…then accept their pledge. And ask Allah to
forgive them…"*).

**Fix.** Before any transform, `refusal_reasons(sentence, lang)` asks the
detector's own deterministic index (`detect.SpanDetector`, built once per
process, ~1 s; the server has already paid for it) two questions, and refuses
with `SCRIPTURE_LIKE` if either says yes:

1. **Verbatim run** — do *K* or more consecutive tokens (after
   `engine.normalize`) appear in order in one *plain* approved rendering of a
   candidate verse? Plain = footnotes cut and tafsir-length editions left out,
   detect's own containment rule, so commentary cannot make prose look like a
   verse. Candidates are retrieved per window of the sentence.
2. **Likeness (English only)** — `SpanDetector.scripture_likeness` on every
   8-word window: the IDF-weighted share of the window's vocabulary found in
   one rendering of one candidate verse.

Archaic scriptural register (*thee, thou, hath, unto, verily*) is a lexical
`SCRIPTURE_LIKE` cue that needs no index. **No index → `SCRIPTURE_UNCHECKED`
on every prose sentence** (fail safe); so does a language with no approved
translation indexed. Cost: ~6 ms per sentence, cached.

**Calibration, per language** — `python3 -m mizan.clarity --measure-guard`
(deterministic, seed 20261004). "Real prose" is every sentence of the
publisher prose in `data/real/` (contexts around quotations, plus the
no-quotation paragraphs where they exist). Contexts contain verse text the
publisher printed, so a label there is not necessarily wrong. "Verse runs"
are 200 runs of 20–30 words from long verses of that language's approved
editions, read from the index, embedded in that language's prose; counted
are sentences holding 10+ run words.

| Lang | Params (likeness, run *K*, retrieval window/candidates) | Real prose labelled | Verse runs caught | With the English thresholds everywhere |
|---|---|---|---|---|
| en | 0.95, 6, 16/80 | 103 / 973 (10.6%) | 196 / 200 | 155 / 973 (15.9%) labelled |
| ur | —, 8, 8/40 | 0 / 193 (0.0%) | 200 / 201 | 96 / 193 (49.7%) |
| tl | —, 10, 8/40 | 0 / 593 (0.0%) | 199 / 201 | 79 / 593 (13.3%) |
| hi | —, 12, 16/80 | 6 / 747 (0.8%) | 190 / 200 | 482 / 747 (64.5%) |
| bn | —, 15, 16/80 | 1 / 96 (1.0%) | 169 / 200 | 77 / 96 (80.2%) |

Why per language: `engine.normalize` fragments Devanagari and Bengali at vowel
signs into one-letter tokens that occur in every verse, so likeness is
meaningless there — a neutral Hindi sentence about Medina's markets scored
1.0 and was labelled scripture. Outside English only the run test is used,
with a longer *K* where tokens are fragments. Every run-test hit on real
prose, in all five languages, is verse text the publisher printed. In
English, 40 of 770 no-quotation sentences are labelled by likeness alone
(Quranic vocabulary, not verse text); all but 8 are refused for other reasons
too. It is kept because it catches fragments the run test misses.

**Effect on the English real prose.** The guard alone refuses 2 of the 333
sentences (81.1% → 81.7%): *"They will recline therein on raised thrones."*
(verse wording) and *"Also, the life of this world is very short."* (a
Quranic phrase; over-refusal).

**Effect on the harm.** 400 synthetic documents, each embedding one 20–30 word
verbatim run (read from the index) in neutral prose, level `curious`:

| | Run altered (". And" inside approved wording) | A sentence with 6+ run words left eligible |
|---|---|---|
| before, no findings | 116 / 400 | 374 / 400 |
| before, detector findings | 24 / 400 | 89 / 400 |
| after, no findings | **0 / 400** | 11 / 400 |
| after, detector findings | **0 / 400** | 5 / 400 |

### 11.4 Languages the gate cannot read (probe)

The ruling, creed and condition patterns read English (plus a thin Arabic
layer). In Urdu, Hindi, Bengali and Tagalog nothing screened rulings, yet
sentences were split-eligible and received `LENGTH`, `PASSIVE` and
`NOUN_CHAIN` suggestions; `readability()` printed an English Flesch score for
Devanagari; and Hindi/Bengali had no sentence boundaries (the danda । was not
a terminator).

**Fix.** `SCREENED_LANGS = {"en"}`. In any other language — or for a sentence
mostly in another script inside an English document — every prose sentence
is refused with `LANGUAGE_UNSCREENED`: nothing is adapted and nothing is
suggested. `as_dict()["language_support"]` states it for the UI:
`{"lang", "ruling_screen", "adaptation", "screened_languages", "note",
"note_ar"}`, with `note`/`note_ar` a plain sentence when `ruling_screen` is
false. Flesch is `None` for anything not English. `।` and `॥` end sentences.

Real prose, `curious`, per language (contexts as the eval builds them; Hindi
now segments at the danda, hence more sentences):

| Lang | Before: refused · suggestions | After: refused · suggestions · labelled `SCRIPTURE_LIKE` |
|---|---|---|
| en | 84 / 333 (25.2%) · 247 | 272 / 333 (81.7%) · 55 · 51 |
| ur | 16 / 86 (18.6%) · 43 | 86 / 86 (100%) · 0 · 0 |
| tl | 39 / 201 (19.4%) · 147 | 201 / 201 (100%) · 0 · 0 |
| hi | 6 / 13 (46.2%) · 7 | 224 / 224 (100%) · 0 · 5 (all printed verse) |
| bn | 9 / 13 (69.2%) · 0 | 75 / 75 (100%) · 0 · 1 (printed verse) |

On the no-quotation paragraphs: tl 65/539 → 539/539 refused, 460 → 0
suggestions, 0 labelled; hi 0/45 → 630/630, 45 → 0, 0 labelled.

### 11.5 Whitespace and the byte-exact claim (reproduced)

A two-sentence quotation with a line break inside it, passed as a frozen span:
the output read `"…ends here. EPSILON…"` (line break → space), yet
`verified_byte_exact` was `True` — the output was `" ".join(sentences)` and
the check compared each sentence's piece of the quotation separately.

**Fix.** The output is rebuilt from the original by character offset (§4),
the split edits in place, and `verify_document` checks each quotation span
whole, plus every frozen term, against the final text. `adapted_text` is
now in `as_dict()`. The quotation and every paragraph break survive exactly
(`test_document_whitespace_survives_and_byte_check_is_whole_document`).

Related gap found on the way: the middle sentence of `He said: "One. Two.
Three."` carries no quote mark of its own and was treated as the author's
prose. `quotation_regions()` now marks quotations across sentences (an
unclosed mark stops at the paragraph break); 2 real-corpus sentences were
newly refused for this alone.

### 11.6 What it cost

Adaptation on the real English prose fell from 27 sentences to 7 at
`curious`, and the Flesch gain from +2.1 to +0.5. That is the measured price
of a gate that now refuses what it promised to refuse; the panel's value is
the refusal, and the 61 sentences still eligible are ordinary narrative
(science asides, sira narration, history of other faiths).
