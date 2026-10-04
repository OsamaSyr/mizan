#!/usr/bin/env python3
"""
MIZAN — collect the approved glossary from الجمهرة (islamic-content.com).

Why this exists
---------------
The official scientific package names «موسوعة الجمهرة - مفردات المحتوى
الإسلامي islamic-content.com/dictionary» as THE reference for translation and
terminology, with the rule «يقدم على الترجمة التلقائية في المصطلحات الشرعية
الحساسة». Panel 2 (terminology lock, src/mizan/terms.py) can only honour that
rule if it holds the dictionary's OWN English equivalents — not ones we wrote.
This script fetches them, slowly, and records where every equivalent came from.

Page structure (verified live 2026-10-03)
-----------------------------------------
    /dictionary                      landing page; links the 8 subject
                                     categories as /dictionary/term/<cid>
    /dictionary/term/<cid>           the category's FULL word listing in one
                                     page: <h2 class="post-title"><a
                                     href=".../dictionary/word/<id>">TITLE</a>
                                     (titles are vocalised: «التَّوْحِيد»)
    /dictionary/word/<id>            the Arabic entry: vocalised <h1>,
                                     breadcrumb categories, one or more
                                     definition blocks (<h5> names the source
                                     work: «من معجم المصطلحات الشرعية»,
                                     «من موسوعة المصطلحات الإسلامية», ...) and
                                     a list «ترجمة هذا المصطلح متوفرة باللغات
                                     التالية» linking /dictionary/word/<id>/<lang>
    /dictionary/word/<id>/en         the English entry: <h1>TITLE<br/>(عربي)
                                     — TITLE is the approved English
                                     equivalent — plus an English definition
                                     whose prose often names the dictionary's
                                     own transliteration ("Tawheed" (monotheism))

So the English equivalent sits one link behind the Arabic page, exactly as the
Arabic page says. We follow that link only when the Arabic page lists English;
we never probe /en for entries that do not offer it.

How terms are chosen (and why not "all 12,000")
-----------------------------------------------
The listings hold ~12,700 entries, most of them specialist (hadith-grading
vocabulary, rare fiqh sub-cases). A terminology lock is only useful on terms
that actually occur in da'wah material, and every page costs >= 5 s of the
host's goodwill. So SEEDS below is a hand-curated list of the vocabulary of
introductory da'wah content: the package's own ten, the core terms the brief
names, then the pillars of Islam and Iman, worship, ethics, family law, da'wah
and the sciences of Quran and hadith. Each seed is RESOLVED against the
dictionary's own listing — we never guess a URL.

Homographs are real. Unvocalised «الخطبة» is both the sermon (خُطبة) and a
marriage proposal (خِطبة); «القرآن» collides with «القِران» (a hajj mode);
«الدين» is religion (الدِّين) or debt (الدَّين). Dictionary titles are
vocalised, so a seed may carry vowels and only titles with COMPATIBLE vowels
are accepted (see `vowel_compatible`).

Crawl ethics (not optional)
---------------------------
* robots.txt is re-read LIVE at the start of every online run, before any
  other request. If it cannot be read, the run aborts (fail closed).
* The STRICTEST applicable policy is obeyed: the union of every Disallow line
  in the `*` group and the `ClaudeBot` group (this client is operated by an
  AI agent), plus a hard deny on /api/ and /ayah/ regardless of what robots
  says, and the largest Crawl-delay among them, never below 5 s.
* Single-threaded. One request at a time, ever.
* Every response is cached under data/glossary/raw/, so `--offline` rebuilds
  all outputs with zero network traffic and reruns are free.
* The User-Agent names the project and a contact address.

The dictionary is the authority, but it is not error-free: some entries are
a different sense than their vocalised title says (4973 «الدِّيْنُ» is defined
as a debt), and some English titles contradict their own definition (2118
«البعث», defined as the resurrection, is titled "Expedition"). REVIEW below
records each such case with the page's own evidence; nothing is rewritten.

Outputs
-------
    data/glossary/jamhara.jsonl            one record per dictionary entry (sense)
    data/glossary/package_terms.json       the package's ten terms + ضابط الاستخدام
    data/glossary/parallel_definitions.jsonl  same-section AR/EN definitions,
                                           for calibrating terms.py on prose
    data/glossary/manifest.json            robots decision, request counts, gaps
    data/glossary/raw/                     every fetched page, verbatim (gzip)

An English equivalent the dictionary does not offer is recorded as null.
Nothing here ever invents one.

Usage
-----
    python3 scripts/fetch_jamhara.py              # crawl (cache first)
    python3 scripts/fetch_jamhara.py --offline    # rebuild from cache only
    python3 scripts/fetch_jamhara.py --plan       # resolve seeds, print, stop
    python3 scripts/fetch_jamhara.py --inspect 4973 2118   # evidence for REVIEW
    python3 scripts/fetch_jamhara.py --max-senses 2

Stdlib only, Python 3.11.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import html as html_mod
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GLOSSARY_DIR = os.path.join(ROOT, "data", "glossary")
RAW = os.path.join(GLOSSARY_DIR, "raw")
RAW_INDEX = os.path.join(RAW, "_index.json")
ROBOTS_SNAPSHOT = os.path.join(RAW, "robots.txt")
OUT_JSONL = os.path.join(GLOSSARY_DIR, "jamhara.jsonl")
OUT_PACKAGE = os.path.join(GLOSSARY_DIR, "package_terms.json")
OUT_MANIFEST = os.path.join(GLOSSARY_DIR, "manifest.json")
# Parallel Arabic/English definitions of the same section, for calibrating
# terms.py on approved PROSE (see terms.measure_parallel_definitions).
OUT_PARALLEL = os.path.join(GLOSSARY_DIR, "parallel_definitions.jsonl")

HOST = "islamic-content.com"
BASE = f"https://{HOST}"
DICTIONARY = f"{BASE}/dictionary"
USER_AGENT = ("Mizan-Research/0.1 (Islamic AI Challenge 2026; "
              "contact osamaabdullh2002@gmail.com)")

# The robots groups whose rules bind this client. `*` because our own UA is
# not named; ClaudeBot because an AI agent operates this script. Their rules
# are UNIONED — the strictest reading, not the most convenient one.
APPLICABLE_AGENTS = ("*", "claudebot", "mizan-research")
# Never fetched, whatever robots.txt says tomorrow.
HARD_DENY = ("/api/", "/ayah/")
MIN_DELAY = 5.0          # seconds; floor even if robots states less
JITTER = 0.5             # added on top, so we are never AT the limit
TIMEOUT = 30
MAX_SENSES = 3           # dictionary entries kept per seed (homonyms/senses)

# ---------------------------------------------------------------------------
# The package's own sample glossary — «نماذج لقاموس المصطلحات الأساسية»,
# page 8 of «المرجعية والحزمة العلمية والبيانات» (نسخة 20/3/1448).
#
# Transcribed by hand and checked against the PDF's layout-mode text (the plain
# text extraction mangles «ال» ligatures). `usage_rule_ar` is VERBATIM.
# `discouraged_en` is NOT package text: it is MIZAN's reading of what each
# rule forbids, kept deliberately narrow and tied to the rule by `basis`, so a
# reviewer can disagree with a specific line instead of with the tool.
# ---------------------------------------------------------------------------
PACKAGE_SOURCE = ("المرجعية والحزمة العلمية والبيانات — تحدي الذكاء الاصطناعي "
                  "في خدمة المحتوى الإسلامي، نسخة 20/3/1448، ص 8: "
                  "«نماذج لقاموس المصطلحات الأساسية»")

PACKAGE_TERMS = [
    {
        "term_ar": "الإسلام", "approved_en_raw": "Islam",
        "usage_rule_ar": "دين الاستسلام لله بالتوحيد والانقياد له بالطاعة، "
                         "ويشرح بحسب السياق ولا يختزل في معنى ثقافي عام.",
        "usage_rule_en": "Submission to God through Tawhid and obedience; "
                         "explained by context, never reduced to a general "
                         "cultural meaning.",
        "discouraged_en": ["Islamic culture", "Muslim culture",
                           "cultural identity", "Muslim identity"],
        "basis": "ولا يختزل في معنى ثقافي عام",
    },
    {
        "term_ar": "التوحيد", "approved_en_raw": "Tawhid / Oneness of God",
        "usage_rule_ar": "يفضل إبقاء المصطلح مع شرح معناه إفراد الله "
                         "بالربوبية والألوهية ووصفه بما جاء الوحي به من "
                         "أسمائه الحسنى؛ ولا يختزل في ترجمة قد توحي بمجرد "
                         "الوحدانية العددية.",
        "usage_rule_en": "Keep the term and explain it; do not reduce it to a "
                         "rendering that suggests mere numerical oneness.",
        "discouraged_en": ["numerical oneness", "numerical unity",
                           "unitarianism", "monism"],
        "basis": "ولا يختزل في ترجمة قد توحي بمجرد الوحدانية العددية",
    },
    {
        "term_ar": "العبادة", "approved_en_raw": "Worship",
        "usage_rule_ar": "تشمل أعمال القلب والقول والعمل التي يتقرب بها العبد "
                         "إلى الله، ولا تحصر في الشعائر فقط.",
        "usage_rule_en": "Covers acts of the heart, speech and limbs by which "
                         "a servant draws near to God; not confined to rites.",
        "discouraged_en": ["rituals", "ritual", "rites", "rite",
                           "ritual practice", "religious rituals"],
        "basis": "ولا تحصر في الشعائر فقط",
    },
    {
        "term_ar": "النبوة", "approved_en_raw": "Prophethood",
        "usage_rule_ar": "تستخدم للدلالة على اصطفاء الأنبياء بالوحي، مع "
                         "التمييز بينها وبين القيادة الدينية البشرية.",
        "usage_rule_en": "God's selection of prophets by revelation, kept "
                         "distinct from human religious leadership.",
        "discouraged_en": ["religious leadership", "spiritual leadership",
                           "spiritual leader", "religious leader"],
        "basis": "مع التمييز بينها وبين القيادة الدينية البشرية",
    },
    {
        "term_ar": "الوحي", "approved_en_raw": "Revelation",
        "usage_rule_ar": "يشرح بوصفه ما أوحاه الله إلى أنبيائه، مع تجنب "
                         "استعمالات فضفاضة قد توهم الإلهام الشخصي.",
        "usage_rule_en": "What God revealed to His prophets; avoid loose uses "
                         "that suggest personal inspiration.",
        "discouraged_en": ["inspiration", "personal inspiration", "intuition",
                           "spiritual insight"],
        "basis": "مع تجنب استعمالات فضفاضة قد توهم الإلهام الشخصي",
    },
    {
        "term_ar": "الشريعة",
        "approved_en_raw": "Sharia / Islamic law and guidance",
        "usage_rule_ar": "يشرح بحسب السياق، ولا يختزل في العقوبات أو القانون "
                         "الجنائي.",
        "usage_rule_en": "Explained by context; never reduced to punishments "
                         "or criminal law.",
        "discouraged_en": ["Islamic penal law", "penal law", "penal code",
                           "criminal law", "Islamic criminal law",
                           "punishments", "punishment"],
        "basis": "ولا يختزل في العقوبات أو القانون الجنائي",
    },
    {
        "term_ar": "الحديث", "approved_en_raw": "Hadith",
        "usage_rule_ar": "ما نُقل عن النبي ﷺ من قول أو فعل أو تقرير ونحو ذلك، "
                         "مع بيان درجة الثبوت عند الاستدلال.",
        "usage_rule_en": "What is reported from the Prophet ﷺ of word, deed or "
                         "approval; state its grade when used as proof.",
        "discouraged_en": [],
        "basis": "",
    },
    {
        "term_ar": "السنة", "approved_en_raw": "Sunnah",
        "usage_rule_ar": "هدي النبي ﷺ وطريقته، ويحدد المقصود بحسب السياق "
                         "العلمي.",
        "usage_rule_en": "The Prophet's ﷺ guidance and way; the intended sense "
                         "is fixed by the scholarly context.",
        "discouraged_en": [],
        "basis": "",
    },
    {
        "term_ar": "الفتوى", "approved_en_raw": "Fatwa",
        "usage_rule_ar": "جواب شرعي يصدره مؤهل في واقعة أو سؤال؛ ولا يساوى "
                         "بالمعلومة العامة.",
        "usage_rule_en": "A shar'i answer issued by a qualified person on a "
                         "case or question; not equated with general "
                         "information.",
        "discouraged_en": ["general information"],
        "basis": "ولا يساوى بالمعلومة العامة",
    },
    {
        "term_ar": "الدعوة",
        "approved_en_raw": "Da‘wah / Invitation to Islam",
        "usage_rule_ar": "التعريف بالإسلام والدعوة إليه بالحكمة، ويختار "
                         "المقابل بحسب السياق والجمهور.",
        "usage_rule_en": "Introducing Islam and inviting to it with wisdom; "
                         "the equivalent is chosen by context and audience.",
        "discouraged_en": [],
        "basis": "",
    },
]

# ---------------------------------------------------------------------------
# Sense review. الجمهرة is the authority, but its pages contain errors a reader
# can see on the page itself. Every line below was checked by reading the
# entry's OWN Arabic and English definitions side by side. Nothing is
# replaced or invented: the record keeps the dictionary's title verbatim and
# carries the flag, and terms.py declines to use a flagged title.
#
#   wrong_sense    the entry is a different word or sense than the seed means
#                  — terms.py does not load the record at all
#   suspect_title  the definition IS the seed's sense, but the English title
#                  is not — terms.py keeps the dictionary's transliteration
#                  and drops the title
# ---------------------------------------------------------------------------
REVIEW: dict[int, tuple[str, str]] = {}


def _review(text: str) -> dict[int, tuple[str, str]]:
    out = {}
    for line in text.strip().splitlines():
        wid, flag, reason = (x.strip() for x in line.split("|", 2))
        out[int(wid)] = (flag, reason)
    return out


REVIEW = _review("""
4973 | wrong_sense   | vocalised «الدِّيْنُ» but defined «حَقٌّ لازِمٌ في الذِّمَّةِ» — a debt; English "Debt"
2118 | suspect_title | defined as the resurrection of the dead (AR and EN); English title "Expedition"
5191 | suspect_title | defined as leaving Islam for disbelief (AR and EN); English title "Reverberation"
4298 | wrong_sense   | defined as recording revenue collected by officials — bookkeeping, not the Reckoning; English "Accounting"
4544 | wrong_sense   | defined «مكان يجتمع فيه الماء» — any pool, not the Prophet's Basin; English "Pond"
4545 | wrong_sense   | defined «مكان يجتمع فيه الماء» with fiqh examples — any pool, not the Prophet's Basin
4299 | wrong_sense   | defined as recording revenue collected by officials — bookkeeping, not the Reckoning
5669 | suspect_title | defined as the leading scholars of the Companions and Followers (AR and EN); English title "Lending"
5670 | wrong_sense   | defined «دفع مال إرفاقاً لمن ينتفع به ويرد مثله» — a loan, not the righteous predecessors
5351 | wrong_sense   | vocalised «الرُّوحُ» but defined «الرحمة. وهي صفة لله تعالى» — mercy (الرَّوْح), not the soul; English "Mercy"
4216 | wrong_sense   | defined «مصطلح لدى بعض الفقهاء يفيد الخلاف» — a jurists' term for disagreement, not the Sanctuary; English "Prohibited"
2801 | wrong_sense   | defined as handing a thing over to another (a sale/transfer), not the closing salam of the prayer
6874 | suspect_title | defined as the original ruling legislated by a proof and unchanged by circumstance (AR and EN); English title "Firm intention"
6772 | suspect_title | defined «استعمال الأمور في مواضعها» — putting things in their proper place, i.e. justice; English title "Moderation"
2038 | suspect_title | defined as the deed that brings one closer to Allah — righteousness; English title "The Beneficent" (the divine name)
2142 | wrong_sense   | defined as money given to a proxy to cover performing hajj on another's behalf; English "Hajj wages" — not conveying the message
3153 | wrong_sense   | defined as clarifying an ambiguous admission (AR) / an ability to relate ideas (EN); English "Understanding" — not Quranic exegesis
9567 | suspect_title | defined as the sheets in which the Quran is collected (AR and EN); English title "Altering narrator" belongs to «المُصَحِّف»
6166 | wrong_sense   | «الصحيح» as the preferred juristic view; English "The correct view" — not the hadith grade
6167 | wrong_sense   | «الصحيح» as an act that achieves its intended effect (valid) — not the hadith grade
6168 | wrong_sense   | «الصحيح» as a term signalling a juristic preference — not the hadith grade
10238 | wrong_sense  | «الموضوع» as the grammatical subject of a sentence; English "Subject matter" — not a fabricated hadith
10239 | wrong_sense  | «الموضوع» as the grammatical subject of a sentence — not a fabricated hadith
5764 | wrong_sense   | «السند» as a written legal deed; English "Legal document" — not the chain of narration
5765 | wrong_sense   | «السند» as a written legal deed — not the chain of narration
144  | wrong_sense   | «الأثر» as the result of something; English "Effect" — not a narration (athar)
145  | wrong_sense   | «الأثر» as the result of something — not a narration (athar)
5837 | wrong_sense   | «السيرة» as the rules governing relations with non-Muslims (siyar); English "Lifestyle" — not the Prophet's biography
5838 | wrong_sense   | «السيرة» as a person's state or conduct — not the Prophet's biography
1408 | suspect_title | defined as announcing that the congregational prayer is starting (AR and EN); English title "Residency"
9740 | suspect_title | defined as the Prophet's ascension (AR and EN); English title "Tool of ascension" renders the instrument-noun form, not the event
""")

# ---------------------------------------------------------------------------
# Seeds. One line per term:
#
#     [~]primary | alternative | ... [@ match form, match form]   # comment
#
# * "##" opens a theme (recorded, not used for matching).
# * Vowels on a seed are a CONSTRAINT used to separate homographs; seeds
#   without vowels accept any vocalisation.
# * Alternatives are tried in order only when the primary has no listing
#   entry at all.
# * "@ ..." fixes the Arabic surface form(s) terms.py looks for in running
#   text. Default: the dictionary title when the primary matched, the primary
#   seed when an alternative did («الحديث الصحيح» resolves to the entry
#   «الصحيح», but bare «الصحيح» in prose just means "correct").
# * "#<id>" pins dictionary entries by id, bypassing resolution. Used only
#   when the dictionary files the right sense under a vowel pattern that
#   contradicts it (see REVIEW), and only after reading that entry.
# * A leading "~" marks the unvocalised Arabic as AMBIGUOUS in running prose:
#   a common word outside its technical sense («السنة» is also "the year»,
#   «الكرسي» "the chair", «الحدود» "the borders"). terms.py still reports an
#   approved or discouraged rendering for such a term but never reports it as
#   MISSING — that would flag ordinary words. Recorded as `ambiguous_ar`.
# ---------------------------------------------------------------------------
SEEDS = """
## package — the ten terms of «نماذج لقاموس المصطلحات الأساسية»
الإسلام
التوحيد
العبادة
النبوة
الوحي
الشريعة
~الحديث
~السُّنَّة
الفتوى
الدعوة

## core — named in the brief
الصلاة
الزكاة
الصيام #6393 @ الصيام   # entry 14515 «صِيَامٌ» has no English and reads «انْظُرْ: صَوْمٌ»; follow it
الصوم
الحج
الشرك
الإيمان
~الإحسان
الجنة
~النار
القيامة | يوم القيامة
الملائكة
الرسول
الصحابة
الحلال
الحرام
الوضوء
~الطهارة
الجهاد
الهجرة
القبلة
الكعبة
المسجد
~الإمام
الخُطْبَة | خُطْبَةُ الجُمُعَةِ @ خطبة الجمعة
التقوى
الصبر
~الذِّكْر
الدعاء
البدعة
الكفر
النفاق

## creed — العقيدة
العقيدة
~الدِّين
~المِلَّة
الفطرة
الإِلَه
الربوبية
توحيد الألوهية
توحيد الربوبية
توحيد الأسماء والصفات
الأسماء الحسنى
~الصفات
~الغيب
~القَدَر
القضاء والقدر
اليوم الآخر
الآخرة
~البعث   # the verb «بعث» (He sent / raised) shares the spelling
~الحساب
~الميزان
الصراط
الشفاعة
الحوض
البرزخ
عذاب القبر
الجن
الشيطان
إبليس
السحر
المعجزة
الكرامة
النبي
الأنبياء
الرسل
~الرسالة
خاتم النبيين | خاتم الأنبياء
القُرْآن
التوراة
الإنجيل
الزبور
الكتب السماوية | الكتب المنزلة
أركان الإسلام
أركان الإيمان
الشهادتان
~الشهادة
كلمة التوحيد
الولاء والبراء
الطاغوت
~العبودية
الرِّدَّة
الإلحاد
أهل الكتاب
المشرك | المشركون
الكافر
المنافق
المؤمن
المسلم
~الأُمَّة
السلف
أهل السنة والجماعة
الصحابي
التابعي | التابعون
العرش
~الكرسي
اللوح المحفوظ
الإسراء
المعراج
التوسل
الوسيلة
~الرُّوح

## worship — العبادات
~الأذان   # «آذان» (ears) folds onto it once hamza seats are folded
~الإقامة
الركعة
الركوع
السجود
~التشهد   # the verb «تشهد» (you witness) shares the spelling
~التكبير
تكبيرة الإحرام
~التسليم
~الجمعة #6331 @ الجمعة, صلاة الجمعة   # 14372 «جُمُعَةٌ» has no English and reads «انْظُرْ: صلاة الجمعة»; follow it
صلاة الجماعة | الجماعة
صلاة العيد
صلاة التراويح #2668 @ صلاة التراويح, التراويح   # 14491 has no English page; 2668 «التراويح» is the same prayer and has one
صلاة الجنازة
صلاة الاستخارة #833 @ صلاة الاستخارة, الاستخارة   # 14489 reads «انْظُرْ: اسْتِخَارَة»; follow it
قيام الليل
التهجد
الوِتْر #6345 @ الوتر, صلاة الوتر   # 10805 has no English page; 6345 «صلاة الوتر» is the same prayer and has one
النافلة | النفل
التطوع
التيمم
الغُسْل
الجنابة
الحيض
النجاسة
~الصدقة
زكاة الفطر
النصاب
رمضان
السحور
~الإفطار
الاعتكاف
ليلة القدر
العمرة
الإحرام
الطواف
~السعي
عرفة | يوم عرفة
المناسك
~الهَدْي   # unvocalised «الهدي» folds onto «الهدى» (guidance) — ى/ي are folded
الأضحية
~العيد
عيد الفطر
عيد الأضحى
التلبية
~الحرم
المسجد الحرام
المسجد النبوي
المسجد الأقصى
النية
البسملة
التسبيح
التهليل
التحميد
~الحمد
الاستعاذة
الاستغفار
التلاوة
الصلاة على النبي | الصلاة على النبي صلى الله عليه وسلم @ الصلاة على النبي

## rulings — الأحكام وأصول الفقه
~الفرض
~الواجب
المستحب
المندوب
المكروه
المباح
الحكم الشرعي
الرخصة
العزيمة
الفقه
الفقيه
المفتي
الاجتهاد
التقليد
~المذهب
الإجماع
القياس
~المصلحة
مقاصد الشريعة
~الضرورة
~الحدود
القِصَاص
الدية
الكفارة
~النذر   # «النُّذُر» (warnings) and the verb «نذر» share the spelling
~اليمين
الذبح
الذكاة
الذبيحة

## ethics — الأخلاق والسلوك
~الأخلاق
~الصدق
~الأمانة
الإخلاص
التوبة
التوكل
~الخوف
~الرجاء
~المحبة
~الرِّضا
الشكر
الخشوع
الحياء
الزهد
الورع
الرحمة
~العدل
~الظلم
~الكبر
الحسد
الرياء
الغِيبَة
النميمة
~الكذب
الكبائر
الصغائر
~الذنب
المعصية
~الطاعة
المغفرة
~الهداية
~الضلال
~الحكمة
بر الوالدين
صلة الرحم
اليتيم
~الجار
~البر
~الحسنة
~السيئة

## family and dealings — الأسرة والمعاملات
النكاح
~الزواج
الطلاق
الخلع
المهر
~العدة
~الولي
الحجاب
العورة
المَحْرَم
الميراث
~الوصية
~الوقف
الرِّبا
الزنا
الخمر
الميسر
الذبائح

## da'wah — الدعوة والاحتساب
الأمر بالمعروف والنهي عن المنكر | الأمر بالمعروف @ الأمر بالمعروف
الحسبة
~المعروف
~المنكر
الداعية
الموعظة
النصيحة
~البلاغ
التبليغ
المؤلفة قلوبهم
~الشبهة
الحوار
الوسطية
الغلو
التكفير
~الفتنة

## Quran and hadith sciences — علوم القرآن والحديث
التفسير
~التأويل
التجويد
الترتيل
~التنزيل
أسباب النزول
الناسخ والمنسوخ
~المكي
~المدني
السورة
~الآية
المُصْحَف
الإعجاز
الحديث القدسي
الحديث الصحيح | الصحيح
الحديث الحسن | الحسن
الحديث الضعيف | الضعيف
الحديث الموضوع | الموضوع
المتواتر
~السند
الإسناد
~المتن
الراوي
~الأثر

## sira — السيرة
السيرة
~الغزوة
الأنصار
المهاجرون
أهل البيت
أمهات المؤمنين
الخلفاء الراشدون
"""


# ---------------------------------------------------------------------------
# Arabic helpers — composed on top of MIZAN's frozen normaliser so that a term
# folds here exactly the way terms.py will fold it at runtime.
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(ROOT, "src"))
from mizan.arabic import fold_arabic  # noqa: E402

_HARAKAT = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")
# fatha, damma, kasra, sukun. Sukun counts: «المُصْحَف» vs «المُصَحَّف».
_SHORT_VOWELS = {"\u064E": "a", "\u064F": "u", "\u0650": "i", "\u0652": "0"}
_LETTER_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
                              "ة": "ه", "ى": "ي"})


def strip_harakat(text: str) -> str:
    """Display form: no diacritics or tatweel, hamza seats kept («الإسلام»)."""
    return re.sub(r"\s+", " ", _HARAKAT.sub("", text)).strip()


def fold_key(text: str) -> str:
    """Matching key: MIZAN fold, spaces removed (titles vary in spacing)."""
    return fold_arabic(text).replace(" ", "")


def drop_article(key: str) -> str:
    """«الصيام» and «صيام» are the same entry for resolution purposes."""
    return key[2:] if key.startswith("ال") and len(key) > 3 else key


def _vowel_skeleton(text: str) -> list[tuple[str, str | None, str | None]]:
    """
    [(base_letter, hamza_seat, short_vowel)] for every letter.

    The hamza seat is kept apart from the base letter because it is lexical:
    «الإله» (God) and «الآلة» (a tool) fold to the same key, and only the seat
    (إ vs آ) tells them apart. A bare ا carries no seat information, since
    many sources simply drop the hamza.

    Word-final vowels are dropped: case endings vary freely between sources
    and carry no lexical distinction.
    """
    out: list[tuple[str, str | None, str | None]] = []
    for word in text.split():
        letters: list[list] = []
        for ch in unicodedata.normalize("NFC", word):
            if ch in _SHORT_VOWELS:
                if letters:
                    letters[-1][2] = _SHORT_VOWELS[ch]
            elif unicodedata.category(ch) == "Mn" or ch == "\u0640":
                continue
            elif "\u0621" <= ch <= "\u064A" or ch == "ٱ":
                letters.append([ch.translate(_LETTER_FOLD), _SEATS.get(ch), None])
        if letters:
            letters[-1][2] = None
        out.extend(tuple(x) for x in letters)
    return out


_SEATS = {"أ": "hamza-above", "إ": "hamza-below", "آ": "madda", "ٱ": "wasla"}


def vowel_compatible(seed: str, title: str) -> bool:
    """
    True unless seed and title DISAGREE on a short vowel or a hamza seat at
    the same letter.

    «الخُطْبَة» (sermon) vs «الْخِطْبَةُ» (proposal): damma vs kasra on خ ->
    incompatible. «الإله» vs «الآلَةُ»: إ vs آ -> incompatible. An unvocalised
    seed constrains only through its hamza seats. Letters that do not align
    one-to-one (rare spelling variants) are treated as compatible: the check
    exists to reject a KNOWN wrong homograph, not to be clever.
    """
    a, b = _vowel_skeleton(seed), _vowel_skeleton(title)
    if len(a) != len(b):
        return True
    for (la, sa, va), (lb, sb, vb) in zip(a, b):
        if la != lb:
            return True
        if sa and sb and sa != sb and "wasla" not in (sa, sb):
            return False
        if va and vb and va != vb:
            return False
    return True


# ---------------------------------------------------------------------------
# robots.txt — strictest applicable reading
# ---------------------------------------------------------------------------

def parse_robots(text: str) -> list[dict]:
    """Groups as [{'agents': [...], 'disallow': [...], 'allow': [...], 'delay': f}]."""
    groups: list[dict] = []
    current = None
    last_was_agent = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, value = (p.strip() for p in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            if current is None or not last_was_agent:
                current = {"agents": [], "disallow": [], "allow": [], "delay": None}
                groups.append(current)
            current["agents"].append(value.lower())
            last_was_agent = True
            continue
        last_was_agent = False
        if current is None:
            continue
        if field == "disallow" and value:
            current["disallow"].append(value)
        elif field == "allow" and value:
            current["allow"].append(value)
        elif field == "crawl-delay":
            try:
                current["delay"] = float(value)
            except ValueError:
                pass
    return groups


def robots_policy(text: str) -> dict:
    """
    Union the Disallow lines of every applicable group; take the max delay.

    `Allow` lines are ignored on purpose: they can only loosen a Disallow, and
    the strictest reading never needs them.
    """
    groups = parse_robots(text)
    applicable = [g for g in groups
                  if any(a in APPLICABLE_AGENTS for a in g["agents"])]
    disallow = sorted({d for g in applicable for d in g["disallow"]}
                      | set(HARD_DENY))
    delays = [g["delay"] for g in applicable if g["delay"] is not None]
    return {
        "groups_applied": [g["agents"] for g in applicable],
        "disallow": disallow,
        "crawl_delay_stated": max(delays) if delays else None,
        "delay_used": max([MIN_DELAY] + delays) + JITTER,
    }


# ---------------------------------------------------------------------------
# Polite, cached, single-threaded fetcher
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cache_path(url: str) -> str:
    """
    Stable, human-readable cache filename for a URL.

    Gzipped: a word page is 170-550 KB of mostly site chrome, and ~700 of them
    uncompressed would put ~170 MB into a public repository. Compressed they
    are still the raw bytes, byte-for-byte recoverable with `gunzip`.
    """
    p = urllib.parse.urlsplit(url)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", p.path.strip("/")) or "index"
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return os.path.join(RAW, f"{slug}__{h}.html.gz")


def _read_cache(path: str) -> str:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return f.read()


def _write_cache(path: str, body: str) -> None:
    os.makedirs(RAW, exist_ok=True)
    # mtime=0 keeps the archive bytes deterministic across reruns.
    with open(path, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            gz.write(body.encode("utf-8"))


class Fetcher:
    """
    Every page goes through here, so the rules are enforced in ONE place.

    `offline=True` never opens a socket: a cache miss is recorded and None is
    returned, which the caller treats as "unknown", never as "absent".
    """

    def __init__(self, policy: dict | None, offline: bool):
        self.policy = policy or {"disallow": list(HARD_DENY),
                                 "delay_used": MIN_DELAY + JITTER}
        self.offline = offline
        self._last = 0.0
        self.stats = {"network": 0, "cache_hits": 0, "cache_misses_offline": 0,
                      "blocked_by_policy": 0, "http_errors": 0}
        self.blocked: list[str] = []
        self.errors: list[dict] = []
        self.index = self._load_index()

    @staticmethod
    def _load_index() -> dict:
        if os.path.exists(RAW_INDEX):
            with open(RAW_INDEX, encoding="utf-8") as f:
                return json.load(f)
        return {}

    def save_index(self) -> None:
        os.makedirs(RAW, exist_ok=True)
        with open(RAW_INDEX, "w", encoding="utf-8") as f:
            json.dump(self.index, f, ensure_ascii=False, indent=1, sort_keys=True)

    def allowed(self, url: str) -> bool:
        p = urllib.parse.urlsplit(url)
        if p.netloc != HOST:
            return False
        path = p.path or "/"
        return not any(path.startswith(d) for d in self.policy["disallow"])

    def retrieved_at(self, url: str) -> str | None:
        meta = self.index.get(url)
        return meta.get("retrieved_at") if meta else None

    def get(self, url: str) -> str | None:
        if not self.allowed(url):
            self.stats["blocked_by_policy"] += 1
            self.blocked.append(url)
            return None
        path = cache_path(url)
        legacy = path[:-len(".gz")]
        if not os.path.exists(path) and os.path.exists(legacy):
            # Pages cached uncompressed while the site structure was being
            # investigated: same bytes, recompressed, original mtime kept.
            mtime = os.path.getmtime(legacy)
            with open(legacy, encoding="utf-8") as f:
                _write_cache(path, f.read())
            os.utime(path, (mtime, mtime))
            os.remove(legacy)
        if os.path.exists(path):
            self.stats["cache_hits"] += 1
            if url not in self.index:      # cached before the index existed
                self.index[url] = {
                    "status": 200, "file": os.path.basename(path),
                    "retrieved_at": datetime.fromtimestamp(
                        os.path.getmtime(path), timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")}
            return _read_cache(path)
        if url in self.index and self.index[url].get("status") != 200:
            return None                    # known failure; do not re-ask
        if self.offline:
            self.stats["cache_misses_offline"] += 1
            return None

        wait = self.policy["delay_used"] - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Accept-Language": "ar,en;q=0.8"})
        status, body = 0, ""
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                status = r.status
                body = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status = e.code
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            status = -1
            self.errors.append({"url": url, "error": f"{type(e).__name__}: {e}"})
        finally:
            self._last = time.monotonic()
            self.stats["network"] += 1

        self.index[url] = {"status": status, "retrieved_at": now_iso(),
                           "file": os.path.basename(path) if status == 200 else None}
        if status != 200 or not body:
            self.stats["http_errors"] += 1
            if status not in (-1,):
                self.errors.append({"url": url, "status": status})
            return None
        _write_cache(path, body)
        return body


def read_robots(offline: bool) -> tuple[dict, str, str]:
    """
    Live robots.txt read (online) or the saved snapshot (offline).

    Returns (policy, text, provenance). Online failure ABORTS the run: not
    being able to read the rules is not permission to ignore them.
    """
    url = f"{BASE}/robots.txt"
    if offline:
        if not os.path.exists(ROBOTS_SNAPSHOT):
            sys.exit("offline: no robots.txt snapshot — run online once first")
        with open(ROBOTS_SNAPSHOT, encoding="utf-8") as f:
            text = f.read()
        return robots_policy(text), text, "snapshot (offline run)"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if r.status != 200:
                sys.exit(f"robots.txt returned {r.status}; aborting (fail closed)")
            text = r.read().decode("utf-8", "replace")
    except Exception as e:                                   # noqa: BLE001
        sys.exit(f"robots.txt unreadable ({e}); aborting (fail closed)")
    os.makedirs(RAW, exist_ok=True)
    with open(ROBOTS_SNAPSHOT, "w", encoding="utf-8") as f:
        f.write(text)
    return robots_policy(text), text, f"live {now_iso()}"


# ---------------------------------------------------------------------------
# Page parsers. Regex over a known, server-rendered template: brittle by
# nature, so each parser is small, named, and fails to None rather than to a
# guess. A layout change shows up as nulls in the manifest, not as bad data.
# ---------------------------------------------------------------------------

def _text(fragment: str) -> str:
    t = re.sub(r"<br\s*/?>", " ", fragment)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"<[^>]*$", " ", t)        # a tag cut off at a block boundary
    return re.sub(r"\s+", " ", html_mod.unescape(t)).strip()


def parse_landing(html: str) -> dict[int, str]:
    """Category id -> Arabic category name, from /dictionary."""
    out: dict[int, str] = {}
    for m in re.finditer(
            r'href="https://islamic-content\.com/dictionary/term/(\d+)"[^>]*>(.*?)</a>',
            html, re.S):
        name = _text(m.group(2))
        if name:
            out[int(m.group(1))] = name
    return out


_LISTING_ITEM = re.compile(
    r'<h2 class="post-title[^"]*">\s*<a href="https://islamic-content\.com/'
    r'dictionary/word/(\d+)">(.*?)</a>', re.S)


def parse_listing(html: str) -> list[tuple[int, str]]:
    """[(word_id, vocalised title)] from a /dictionary/term/<cid> page."""
    return [(int(m.group(1)), _text(m.group(2)))
            for m in _LISTING_ITEM.finditer(html)]


def _article(html: str) -> str:
    i = html.find('<article class="entry-wraper">')
    if i < 0:
        return ""
    j = html.find("</article>", i)
    return html[i:j if j > 0 else None]


def _definition_blocks(article: str) -> list[tuple[str, str]]:
    """[(source work named in <h5>, inner html)] in page order."""
    heads = list(re.finditer(r'<h5 class="text-left">(.*?)</h5>', article, re.S))
    out = []
    for k, m in enumerate(heads):
        end = heads[k + 1].start() if k + 1 < len(heads) else article.find('id="related"')
        out.append((_text(m.group(1)), article[m.end():end if end > 0 else None]))
    return out


def _sections(block_html: str) -> dict[str, str]:
    """<h2>NAME</h2><p>TEXT</p> pairs inside one definition block."""
    out: dict[str, str] = {}
    for m in re.finditer(r"<h2>(.*?)</h2>\s*(.*?)(?=<h2>|$)", block_html, re.S):
        name = _text(m.group(1))
        if name and name not in out:
            out[name] = _text(m.group(2))
    return out


def _languages(html: str) -> list[str]:
    """Language codes the entry is offered in («متوفرة باللغات التالية»)."""
    i = html.find('id="related"')
    if i < 0:
        return []
    seg = html[i:i + 20000]
    return sorted(set(re.findall(
        r'href="https://islamic-content\.com/dictionary/word/\d+/([a-z]{2,3})"', seg)))


def _breadcrumb_categories(html: str) -> list[str]:
    m = re.search(r'<div class="breadcrumbs">(.*?)</ol>', html, re.S)
    if not m:
        return []
    return [_text(x) for x in re.findall(
        r'<a href="https://islamic-content\.com/dictionary/term/\d+">(.*?)</a>',
        m.group(1), re.S)]


def _short(text: str, limit: int = 300) -> str:
    """First sentence(s) up to `limit` chars, cut at a sentence end if possible."""
    text = text.strip().lstrip("-–—:•· ").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for stop in (".", "؛", "،"):
        k = cut.rfind(stop)
        if k > limit * 0.5:
            return cut[:k + 1].strip()
    return cut.rstrip() + "…"


# Section preference for the short definition: the technical meaning first,
# then the plain definition. Language-of-origin notes are not definitions.
_DEF_PREFERENCE = ("المعنى الاصطلاحي", "التعريف", "الشرح المختصر")


def _preferred_section(blocks: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    """(section name, full text) of the first preferred definition section."""
    for _, block in blocks:
        secs = _sections(block)
        for name in _DEF_PREFERENCE:
            if secs.get(name):
                return name, secs[name]
    return None, None


def parse_arabic_entry(html: str) -> dict:
    art = _article(html)
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", art, re.S)
    blocks = _definition_blocks(art)
    section, definition = _preferred_section(blocks)
    if not definition and blocks:
        # «معجم المصطلحات الشرعية» blocks are plain paragraphs, no <h2>.
        body = re.split(r'<div class="footnotes">', blocks[0][1])[0]
        definition = _text(body) or None
    return {
        "title_vocalized": _text(h1.group(1)) if h1 else None,
        "categories": _breadcrumb_categories(html),
        "definition_ar": _short(definition) if definition else None,
        "section": section,
        "section_text": definition if section else None,
        "sources": [name for name, _ in blocks],
        "languages": _languages(html),
    }


# A dictionary-attested transliteration: the quoted word that OPENS an
# English section, e.g.  "Tawheed" (monotheism)  /  "Salah" (prayer)  /
# "Sawm": it is derived ...  /  "`Ibaadah", or worship ...
_TRANSLIT = re.compile(r'^["“]([^"”]{2,40})["”]')


def parse_english_entry(html: str) -> dict:
    art = _article(html)
    h1 = re.search(r"<h1[^>]*>(.*?)<br\s*/?>", art, re.S)
    title = _text(h1.group(1)) if h1 else None
    blocks = _definition_blocks(art)
    section, definition = _preferred_section(blocks)
    translits: list[str] = []
    for _, block in blocks:
        secs = _sections(block)
        for body in secs.values():
            m = _TRANSLIT.match(body)
            if m:
                cand = m.group(1).strip()
                # Latin script, at most three words: a term, not a sentence.
                if (re.fullmatch(r"[A-Za-zÀ-ɏḀ-ỿ'‘’`ʿʾ\- ]+", cand)
                        and len(cand.split()) <= 3
                        and cand.lower() != (title or "").lower()
                        and cand not in translits):
                    translits.append(cand)
    return {
        "title_en": title,
        "definition_en": _short(definition) if definition else None,
        "transliterations": translits,
        "section": section,
        "section_text": definition,
    }


# ---------------------------------------------------------------------------
# Seed resolution
# ---------------------------------------------------------------------------

def parse_seeds(text: str = SEEDS) -> list[dict]:
    seeds, theme = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("##"):
            theme = line.lstrip("#").split("—")[0].strip()
            continue
        line = re.split(r"\s#\s", f" {line} ", maxsplit=1)[0].strip()  # "# comment"
        ambiguous = line.startswith("~")
        line = line.lstrip("~").strip()
        match_override: list[str] = []
        if "@" in line:
            line, forms = line.split("@", 1)
            match_override = [f.strip() for f in forms.split(",") if f.strip()]
        pins = [int(x) for x in re.findall(r"#(\d+)", line)]
        line = re.sub(r"#\d+", "", line)
        alts = [a.strip() for a in line.split("|") if a.strip()]
        if alts:
            seeds.append({"seed": alts[0], "alternatives": alts, "theme": theme,
                          "ambiguous": ambiguous, "match_ar": match_override,
                          "pins": pins})
    # A seed listed twice (e.g. as a core term and as an alternative) is
    # resolved once.
    seen, out = set(), []
    for s in seeds:
        k = fold_key(s["seed"])
        if k not in seen:
            seen.add(k)
            out.append(s)
    return out


def build_title_index(entries: dict[int, dict]) -> tuple[dict, dict]:
    exact: dict[str, list[int]] = {}
    bare: dict[str, list[int]] = {}
    for wid, e in entries.items():
        k = fold_key(e["title"])
        exact.setdefault(k, []).append(wid)
        bare.setdefault(drop_article(k), []).append(wid)
    return exact, bare


def resolve(seed: dict, entries: dict, exact: dict, bare: dict,
            max_senses: int) -> tuple[list[int], str | None]:
    """
    Word ids for this seed, best first, and the alternative that matched.

    Exact folded title first, then article-insensitive; vowel-incompatible
    homographs are dropped. Ranking: entries filed under more subject
    categories first (the general sense), then lowest id (oldest entry).
    """
    if seed.get("pins"):
        return [w for w in seed["pins"] if w in entries][:max_senses], seed["seed"]
    for alt in seed["alternatives"]:
        k = fold_key(alt)
        for pool in (exact.get(k, []), bare.get(drop_article(k), [])):
            ids = [w for w in pool if vowel_compatible(alt, entries[w]["title"])]
            if ids:
                ids.sort(key=lambda w: (-len(entries[w]["categories"]), w))
                return ids[:max_senses], alt
    return [], None


def match_forms(seed: dict, via: str | None, title: str) -> list[str]:
    """
    Arabic surface forms terms.py should look for (see the SEEDS header).

    Explicit "@" forms win. Otherwise the dictionary's own title when the
    primary seed matched it, else the primary seed itself — because an
    alternative is usually a SHORTER, more generic entry than the phrase we
    actually want to find in prose.
    """
    if seed["match_ar"]:
        return [strip_harakat(f) for f in seed["match_ar"]]
    if via is None or fold_key(via) == fold_key(seed["seed"]):
        return [strip_harakat(title)]
    return [strip_harakat(seed["seed"])]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def collect_listings(fetcher: Fetcher) -> tuple[dict[int, dict], dict[int, str]]:
    landing = fetcher.get(DICTIONARY)
    if not landing:
        sys.exit("could not read /dictionary landing page (or not cached)")
    categories = parse_landing(landing)
    entries: dict[int, dict] = {}
    for cid, name in sorted(categories.items()):
        page = fetcher.get(f"{DICTIONARY}/term/{cid}")
        if not page:
            continue
        for wid, title in parse_listing(page):
            e = entries.setdefault(wid, {"title": title, "categories": []})
            if name not in e["categories"]:
                e["categories"].append(name)
    return entries, categories


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--offline", action="store_true",
                    help="rebuild from data/glossary/raw only; no network")
    ap.add_argument("--plan", action="store_true",
                    help="resolve seeds against cached listings and stop")
    ap.add_argument("--max-senses", type=int, default=MAX_SENSES)
    ap.add_argument("--inspect", type=int, nargs="+", metavar="WORD_ID",
                    help="print an entry's Arabic and English title and "
                         "definition (through the same polite fetcher), to "
                         "verify or propose a REVIEW line; writes nothing else")
    args = ap.parse_args(argv)

    started = now_iso()
    # --plan still re-reads robots live (one request): the rules are checked
    # at the start of EVERY run that might be followed by a crawl.
    policy, robots_text, robots_prov = read_robots(args.offline)
    fetcher = Fetcher(policy, offline=args.offline or args.plan)
    if not (fetcher.allowed(DICTIONARY) and fetcher.allowed(f"{DICTIONARY}/word/1")):
        sys.exit(f"robots.txt now disallows the dictionary for us: {policy['disallow']}")
    print(f"robots ({robots_prov}): groups {policy['groups_applied']} · "
          f"disallow {policy['disallow']} · delay {policy['delay_used']:.1f}s")

    if args.inspect:
        for wid in args.inspect:
            page = fetcher.get(f"{DICTIONARY}/word/{wid}")
            if not page:
                print(f"{wid}: not available")
                continue
            ar = parse_arabic_entry(page)
            en = (parse_english_entry(fetcher.get(f"{DICTIONARY}/word/{wid}/en") or "")
                  if "en" in ar["languages"] else {})
            print(f"{wid}: {ar['title_vocalized']} {ar['categories']}\n"
                  f"   AR: {ar['definition_ar']}\n"
                  f"   EN: {en.get('title_en')} {en.get('transliterations')} — "
                  f"{en.get('definition_en')}")
        fetcher.save_index()
        return 0

    entries, categories = collect_listings(fetcher)
    exact, bare = build_title_index(entries)
    print(f"listings: {len(categories)} categories · {len(entries)} unique entries")

    seeds = parse_seeds()
    plan = []
    for s in seeds:
        ids, via = resolve(s, entries, exact, bare, args.max_senses)
        plan.append((s, ids, via))
    resolved = [p for p in plan if p[1]]
    n_pages = sum(len(p[1]) for p in resolved)
    print(f"seeds: {len(seeds)} · resolved {len(resolved)} · "
          f"entries to read {n_pages} · unresolved {len(seeds) - len(resolved)}")

    if args.plan:
        for s, ids, via in plan:
            shown = ", ".join(f"{w}:{entries[w]['title']}" for w in ids) or "—"
            alt = f" (via {via})" if via and via != s["seed"] else ""
            print(f"  {s['seed']}{alt}: {shown}")
        fetcher.save_index()
        return 0

    pkg_by_key = {fold_key(p["term_ar"]): p for p in PACKAGE_TERMS}
    records: list[dict] = []
    parallel: list[dict] = []
    for n, (s, ids, via) in enumerate(resolved, 1):
        for sense, wid in enumerate(ids):
            url_ar = f"{DICTIONARY}/word/{wid}"
            page_ar = fetcher.get(url_ar)
            if not page_ar:
                continue
            ar = parse_arabic_entry(page_ar)
            en = {"title_en": None, "definition_en": None, "transliterations": [],
                  "section": None, "section_text": None}
            url_en = None
            if "en" in ar["languages"]:
                url_en = f"{url_ar}/en"
                page_en = fetcher.get(url_en)
                if page_en:
                    en = parse_english_entry(page_en)
                else:
                    url_en = None
            approved = ([en["title_en"]] + en["transliterations"]
                        if en["title_en"] else None)
            title_v = ar["title_vocalized"] or entries[wid]["title"]
            cats = ar["categories"] or entries[wid]["categories"]
            rec = {
                "term_ar": strip_harakat(title_v),
                "approved_en": approved,
                "definition_ar": ar["definition_ar"],
                "category": "، ".join(cats),
                "source_url": url_en or url_ar,
                "retrieved_at": fetcher.retrieved_at(url_en or url_ar),
                # --- provenance and context (beyond the required fields) ---
                "word_id": wid,
                "term_ar_vocalized": title_v,
                "seed_ar": s["seed"],
                "matched_via": via,
                "sense_rank": sense,
                "theme": s["theme"],
                "definition_en": en["definition_en"],
                "source_url_ar": url_ar,
                "english_available": url_en is not None,
                "other_languages": [l for l in ar["languages"]
                                    if l not in ("en", "ar")],
                "package_term": fold_key(s["seed"]) in pkg_by_key,
                "match_ar": match_forms(s, via, title_v),
                "ambiguous_ar": s["ambiguous"],
                "review": ({"flag": REVIEW[wid][0], "reason": REVIEW[wid][1]}
                           if wid in REVIEW else None),
                "pinned": wid in s.get("pins", []),
            }
            records.append(rec)
            if (ar["section"] and ar["section"] == en["section"]
                    and ar["section_text"] and en["section_text"]
                    and wid not in {p["word_id"] for p in parallel}):
                parallel.append({"word_id": wid, "section": ar["section"],
                                 "ar": ar["section_text"],
                                 "en": en["section_text"],
                                 "source_url": url_en})
        if n % 10 == 0:
            print(f"  … {n}/{len(resolved)} seeds · {fetcher.stats['network']} "
                  f"requests · {len(records)} records", flush=True)
            fetcher.save_index()
    fetcher.save_index()

    os.makedirs(GLOSSARY_DIR, exist_ok=True)
    with open(OUT_JSONL, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(OUT_PARALLEL, "w", encoding="utf-8") as f:
        for p in parallel:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    # Package terms, linked to the Jamhara entry that the same seed resolved to.
    by_seed: dict[str, dict] = {}
    for r in records:
        if r["sense_rank"] == 0:
            by_seed.setdefault(fold_key(r["seed_ar"]), r)
    package_out = []
    for p in PACKAGE_TERMS:
        j = by_seed.get(fold_key(p["term_ar"]))
        package_out.append({
            **p,
            "approved_en": [x.strip() for x in p["approved_en_raw"].split("/")],
            "jamhara": ({"word_id": j["word_id"], "approved_en": j["approved_en"],
                         "source_url": j["source_url"]} if j else None),
        })
    with open(OUT_PACKAGE, "w", encoding="utf-8") as f:
        json.dump({"source": PACKAGE_SOURCE,
                   "note": ("usage_rule_ar is verbatim from the package. "
                            "usage_rule_en is MIZAN's translation. "
                            "discouraged_en is MIZAN's narrow reading of each "
                            "rule (see 'basis'); it is NOT package text."),
                   "terms": package_out}, f, ensure_ascii=False, indent=2)

    unresolved = [s["seed"] for s, ids, _ in plan if not ids]
    seeds_with_en = {fold_key(r["seed_ar"]) for r in records if r["approved_en"]}
    manifest = {
        "started": started, "finished": now_iso(),
        "user_agent": USER_AGENT,
        "robots": {"provenance": robots_prov, **policy},
        "requests": fetcher.stats,
        "blocked_urls": fetcher.blocked,
        "errors": fetcher.errors,
        "listings": {"categories": {str(k): v for k, v in sorted(categories.items())},
                     "unique_entries": len(entries)},
        "seeds": len(seeds),
        "seeds_resolved": len(resolved),
        "seeds_unresolved": unresolved,
        "seeds_with_english": len(seeds_with_en),
        "seeds_without_english": sorted(
            {r["seed_ar"] for r in records} - {r["seed_ar"] for r in records
                                               if r["approved_en"]}),
        "records": len(records),
        "records_with_english": sum(1 for r in records if r["approved_en"]),
        "records_english_null": sum(1 for r in records if not r["approved_en"]),
        "max_senses": args.max_senses,
        "parallel_definitions": len(parallel),
        "review_flags": {str(r["word_id"]): r["review"] for r in records
                         if r["review"]},
        # After review: English a reader can actually use for the seed's sense.
        # wrong_sense records contribute nothing; a suspect_title record
        # contributes only the dictionary's own transliteration, if any.
        "seeds_with_usable_english": len({
            fold_key(r["seed_ar"]) for r in records if r["approved_en"] and (
                not r["review"] or (r["review"]["flag"] == "suspect_title"
                                    and len(r["approved_en"]) > 1))}),
    }
    with open(OUT_MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"\nwrote {len(records)} records ({manifest['records_with_english']} "
          f"with English, {manifest['records_english_null']} null) for "
          f"{len(resolved)} seeds -> {os.path.relpath(OUT_JSONL, ROOT)}")
    print(f"requests: {fetcher.stats}")
    if unresolved:
        print(f"unresolved seeds ({len(unresolved)}): {' · '.join(unresolved)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
