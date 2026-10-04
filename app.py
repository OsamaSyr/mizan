#!/usr/bin/env python3
"""
MIZAN — local attribution server.

Serves the web UI and a small JSON API over the deterministic attribution
engine. Everything runs locally: the approved-translation corpus is a SQLite
file inside the repo, and no request path makes a network call. Python 3.11
standard library only — the ML extra is never required.

Run:
    python3 app.py                      # http://127.0.0.1:8000
    MIZAN_PORT=8777 python3 app.py

What this server does NOT do: it does not answer questions, does not
translate, does not correct scripture and does not issue any ruling. It
attributes a quotation to an approved published translation, or says it
matches none and must be referred to a person.

Routes
------
    GET  /                                  web/index.html
    GET  /review                            web/review.html (reviewer page)
    GET  /api/health                        index metadata, features, limits
    GET  /api/languages                     indexed languages and translations
    POST /api/check                         {translation, arabic?, lang, level?}
    POST /api/clarity                       {check_id, translation, level}
    GET  /api/check/<id>                    a saved check (JSON)
    GET  /api/check/<id>/export.json        the same, as a download
    GET  /report/<id>.html                  self-contained Arabic report (?download=1)
    GET  /api/verse?ref=S:A&lang=xx         approved renderings of one verse
    POST /api/review                        {check_id, finding_idx | finding_ids}
    GET  /api/review?status=open|closed|all the review queue
    GET  /api/review/log                    the append-only audit log
    GET  /api/review/<item>                 one item with its full history
    POST /api/review/<item>/decision        {decision, reviewer, note, replacement_book_id?}

Configuration (environment)
---------------------------
    MIZAN_HOST            bind address            (127.0.0.1)
    MIZAN_ALLOWED_HOSTS   public hostnames accepted behind a reverse proxy, comma-separated
    MIZAN_PORT            port                    (8000)
    MIZAN_REVIEW_DB       review database path    (data/review.sqlite)
    MIZAN_RETENTION_DAYS  days an unreviewed check is kept (30; 0 = forever)
    MIZAN_CHECK_TIMEOUT   seconds before a check is abandoned (90)
    MIZAN_MAX_CHARS       longest translation / Arabic text accepted (20000)
    MIZAN_CLARITY_MODEL   "1" asks Panel 3 to try its model adapter (off)
    MIZAN_TERMS_MODULE    module providing check_terms()     (mizan.terms)
    MIZAN_CLARITY_MODULE  module providing adapt_document()  (mizan.clarity)
    MIZAN_SEMANTIC        "0" turns the AI tier (BGE-M3) off      (on when installed)
    MIZAN_DEVICE          cpu / mps / cuda for the AI tier       (auto)
    MIZAN_SEM_TOKEN_BUDGET  tokens the AI tier may embed per check (1024)
    (setup: MIZAN_ACCEPT_NEW_DUMP=1 accepts a Quranpedia dump newer than the pinned one)
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import html
import importlib
import importlib.util
import json
import os
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
WEB = os.path.join(HERE, "web")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from mizan.engine import Mizan, normalize, sim, verbatim, T_MATCH, T_NEAR  # noqa: E402
from mizan.review import (  # noqa: E402
    DECISIONS,
    REPLACE,
    ReviewError,
    ReviewStore,
    new_check_id,
    utc_now,
)

# ===========================================================================
# Configuration — every tunable in one place
# ===========================================================================

HOST = os.environ.get("MIZAN_HOST", "127.0.0.1")
PORT = int(os.environ.get("MIZAN_PORT", "8000"))

MAX_BODY_BYTES = 512 * 1024          # whole request body
# Measured on real English da'wah prose with the lexical detector: ~2 s at 500
# words, ~10 s at 2,000, ~18 s at 3,000, and a 6,800-word document blew through
# the 90 s timeout. 20,000 characters (~3,400 words) keeps a worst-case check
# well inside the timeout; longer documents are checked in parts.
MAX_TRANSLATION_CHARS = int(os.environ.get("MIZAN_MAX_CHARS", "20000"))
MAX_ARABIC_CHARS = MAX_TRANSLATION_CHARS
SOCKET_TIMEOUT_S = 30                # a client that stalls mid-request is dropped
CHECK_TIMEOUT_S = float(os.environ.get("MIZAN_CHECK_TIMEOUT", "90"))
RETENTION_DAYS = int(os.environ.get("MIZAN_RETENTION_DAYS", "30"))
USE_CLARITY_MODEL = os.environ.get("MIZAN_CLARITY_MODEL") == "1"
# Optional panels, by module name. Overridable so a test (or a deployment that
# ships its own glossary) can point a panel elsewhere without editing code.
TERMS_MODULE = os.environ.get("MIZAN_TERMS_MODULE", "mizan.terms")
CLARITY_MODULE = os.environ.get("MIZAN_CLARITY_MODULE", "mizan.clarity")

QURANPEDIA = "https://quranpedia.net"
# The mushaf id in Quranpedia URLs. 1 is the Madani Hafs mushaf, the one every
# translation page hangs off. Verified live 2026-10-03 (see source_urls()).
QURANPEDIA_MUSHAF = 1

# ===========================================================================
# Engine and index metadata
# ===========================================================================

ENGINE = Mizan()
STORE = ReviewStore()

# The Arabic source text (Tanzil) is what quotations are detected FROM, not an
# approved translation to attribute TO. If it is ever indexed as a translation
# row it is excluded from the language picker and from scoring.
SOURCE_LANG = "ar"


def _corpus_version() -> str:
    """Dump version of the approved corpus (the majority version, not row 1)."""
    row = ENGINE.con.execute(
        "SELECT version, COUNT(*) c FROM translations WHERE language != ? "
        "GROUP BY version ORDER BY c DESC LIMIT 1",
        (SOURCE_LANG,),
    ).fetchone()
    return row["version"] if row else ENGINE.version


INDEX_VERSION = _corpus_version()


def target_languages() -> Dict[str, List[int]]:
    """Languages that can be attributed to — every indexed language but the source."""
    return {code: ids for code, ids in ENGINE.languages().items() if code != SOURCE_LANG}


def _surah_offsets() -> List[int]:
    """offsets[s] = number of verses before surah s, for 1-based global verse ids."""
    rows = []
    for table, col in (("arabic_ayat", "ayah"), ("ayat", "ayah")):
        try:
            rows = ENGINE.con.execute(
                f"SELECT surah, MAX({col}) n FROM {table} GROUP BY surah ORDER BY surah"
            ).fetchall()
        except Exception:  # noqa: BLE001 - table may be absent in a test index
            rows = []
        if len(rows) == 114:
            break
    counts = {r["surah"]: r["n"] for r in rows}
    offsets, total = [0] * 116, 0
    for s in range(1, 115):
        offsets[s] = total
        total += counts.get(s, 0)
    offsets[115] = total
    return offsets


_OFFSETS = _surah_offsets()


def _health_counts() -> Dict[str, int]:
    """Corpus counts. Computed once: the index is read-only while serving."""
    n_verses = ENGINE.con.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT surah, ayah FROM ayat)"
    ).fetchone()[0]
    n_texts = ENGINE.con.execute("SELECT COUNT(*) FROM ayat").fetchone()[0]
    return {"n_verses": n_verses, "n_texts": n_texts}


HEALTH_COUNTS = _health_counts()

# ===========================================================================
# Source links — verified against the live site, not guessed
# ===========================================================================
#
# Fetched 2026-10-03:
#   https://quranpedia.net/surah/1/51/book/13638      200, "ترجمة سورة الذاريات ..."
#     every verse on that page is <a ... ayah="56" surah="51" id="verse-4731">,
#     where 4731 is the verse's 1-based position in the whole mushaf. Checked on
#     books 13604 (2:255 -> verse-262), 1967 (3:59 -> verse-352), 27833,
#     13625 (51:56 -> verse-4731) and 1986 (112:1 -> verse-6222).
#   https://quranpedia.net/surah/1/2/255              302 -> /tafsir/al-baqara/255
#     the verse's own page, used when no single translation is attributed.
#   /surah/1/51/56/book/13638                         404 — there is no
#     per-verse-per-translation page, hence the anchor.


def global_verse_id(surah: int, ayah: int) -> int:
    return _OFFSETS[surah] + ayah


def translation_url(book_id: int, surah: int, ayah: int) -> str:
    """The approved translation's page on Quranpedia, scrolled to the verse."""
    return (f"{QURANPEDIA}/surah/{QURANPEDIA_MUSHAF}/{surah}/book/{book_id}"
            f"#verse-{global_verse_id(surah, ayah)}")


def verse_url(surah: int, ayah: int) -> str:
    """The verse's own page on Quranpedia (no single translation)."""
    return f"{QURANPEDIA}/surah/{QURANPEDIA_MUSHAF}/{surah}/{ayah}"


# ===========================================================================
# Display vocabulary — descriptive, never "wrong"
# ===========================================================================

LANG_NAMES_AR = {
    "en": "الإنجليزية",
    "ur": "الأردية",
    "bn": "البنغالية",
    "hi": "الهندية",
    "tl": "الفلبينية (تجالوج)",
}
LANG_DIR = {"ur": "rtl", "ar": "rtl"}

# Dominant script per language. Used to refuse a cross-script comparison, and
# to decide whether an English-only readability score means anything.
LANG_SCRIPT = {
    "en": "latin", "tl": "latin", "ur": "arabic", "ar": "arabic",
    "bn": "bengali", "hi": "devanagari",
}

STATE_LABELS = {
    "MATCH": "مطابق لترجمة معتمدة",
    "NEAR": "قريب من ترجمة معتمدة، والكلمات مختلفة",
    "UNATTRIBUTED": "لا يُسنَد إلى أي ترجمة معتمدة مفهرسة",
    "NO_APPROVED_TRANSLATION": "لا توجد ترجمة معتمدة مفهرسة بهذه اللغة",
    "UNRESOLVED": "لم نتعرّف على الاقتباس",
}

# States that never pull the document to REFER. Anything else does — including
# a state this file has never heard of, so a new state from the detection layer
# can only make the gate stricter, never looser. Mirrors report._CLEARING_STATES.
# NEAR refers (2026-10-04): a quotation whose words differ from every approved
# translation is not passed, however close — «عدم البناء على النص المحرف».
CLEARING_STATES = {"MATCH", "NO_APPROVED_TRANSLATION"}

# Document-context checks from the report layer. Each refers its finding.
FLAG_LABELS = {
    "extra_words_in_quote": "علامات التنصيص تضمّ كلمات ليست من نص الآية",
    "citation_mismatch": "الرقم المطبوع بجانب الاقتباس يشير إلى آية غير التي وُجد نصها",
    "cited_text_not_found": "نصٌّ بين علامتي تنصيص منسوب إلى هذه الآية، ولا يشبه أي ترجمة معتمدة لها",
    "no_such_verse": "الرقم المطبوع لا يطابق آية موجودة في المصحف",
}

VERDICT_LABELS = {
    "REFER": "يُحال للمراجعة",
    "ATTRIBUTED_WITH_EDITS": "مُسنَد مع تعديل",
    "ATTRIBUTED": "مُسنَد بالكامل",
    "NO_APPROVED_TRANSLATION": "لا ترجمة معتمدة بهذه اللغة",
    "NO_QUOTES": "لم يُعثر على اقتباس",
}

# How a quotation was found. Shown on every finding: the reader must be able to
# tell a printed reference from a text match from a model's suggestion.
TIER_LABELS: Dict[Optional[str], Tuple[str, str]] = {
    "reference": ("مرجع مطبوع",
                  "وُجد عبر مرجع (سورة:آية) طبعه الكاتب بجوار الاقتباس."),
    "arabic": ("من النص العربي",
               "حدّده النص العربي المصدر، ثم بُحث عن موضعه في الترجمة."),
    "lexical": ("مطابقة نصية",
                "وُجد بمطابقة كلمات نادرة مع ترجمة معتمدة مفهرسة."),
    "lexical+semantic": ("مطابقة نصية باقتراح دلالي — ذكاء اصطناعي",
                         "اقترح نموذجُ تضمينٍ هذه الآية، ثم وُجد موضعها بمطابقة الكلمات مع "
                         "ترجمة معتمدة. الحالة نفسها مقارنة نصية حتمية، ولا سلطة للنموذج عليها."),
    "semantic": ("مرشَّح دلالي — ذكاء اصطناعي",
                 "اقترح نموذجُ تضمينٍ هذه الآية مرشَّحةً بالمعنى. الحالة نفسها "
                 "مقارنة نصية حتمية مع النص المعتمد، ولا سلطة للنموذج عليها."),
    None: ("من النص العربي — بلا موضع في الترجمة",
           "اقتُبس في النص العربي، ولم يُعثر على موضعه في الترجمة."),
}
# Any tier whose name contains one of these came from a model.
AI_TIER_KEYS = ("semantic", "embed", "rerank", "model", "llm")

AI_DISCLOSURE = (
    "اقترحت طبقةٌ دلالية مدعومة بالذكاء الاصطناعي موضعَ بندٍ أو أكثر في هذا الفحص. "
    "وظيفتها اقتراح الآية المرشَّحة فقط؛ أما حالة الإسناد فمقارنة نصية حتمية مع "
    "النص المعتمد المنشور، ولا يولّد النموذج نصًا ولا يملك سلطة على الحكم."
)

# Panel 3 audience levels, labelled for the UI. The codes are clarity.LEVELS.
LEVELS = [
    {"code": "curious", "label_ar": "مستكشف",
     "description_ar": "قارئ غير مسلم بلا خلفية سابقة: أقصر الجمل، ويُقترح تعريف "
                       "كل مصطلح شرعي عند أول وروده."},
    {"code": "new_muslim", "label_ar": "مسلم جديد",
     "description_ar": "يعرف ملامح الدين ولم يألف مصطلحاته بعد: تُقترح تعريفات "
                       "المصطلحات وتُبسَّط البنية."},
    {"code": "practising", "label_ar": "مسلم ممارس",
     "description_ar": "يقرأ هذه المادة بانتظام: المصطلحات مألوفة، وتُرفع مشكلات "
                       "الوضوح البنيوية فقط."},
    {"code": "daee", "label_ar": "داعية",
     "description_ar": "الداعية الذي يصوغ النص: أخفّ تدخّل، ولا تُقسَّم إلا الجمل "
                       "المتصلة حقًا."},
]
LEVEL_CODES = [lvl["code"] for lvl in LEVELS]
DEFAULT_LEVEL = "practising"

PRIVACY_NOTE = (
    "لا حسابات، ولا تتبّع، ولا تحليلات. لا يُحفظ نص الوثيقة ولا النص العربي؛ "
    "يُحفظ من كل فحص ما يلزم للتقرير والمراجعة فقط: المقاطع المقتبسة، والنصوص "
    "المعتمدة المقابلة، والحالة، وبصمة SHA-256 للترجمة. الفحص الذي لم يُرسل منه "
    "بند للمراجعة يُحذف بعد %s يومًا. ويُحفظ في سجل المراجعة اسمُ المراجع كما "
    "يكتبه هو، وقرارُه، وملاحظته، ووقتُه."
)

# ===========================================================================
# Errors — Arabic, and always saying what to do next
# ===========================================================================


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail


def _err_translation_required() -> ApiError:
    return ApiError(400, "TRANSLATION_REQUIRED",
                    "الترجمة مطلوبة. الصق نص الترجمة كما سيُنشر في الحقل الثاني، ثم أعد الفحص.")


def _err_check_not_found() -> ApiError:
    return ApiError(404, "CHECK_NOT_FOUND",
                    "لا يوجد فحص بهذا المعرّف — ربما انتهت مدة حفظه. أعد فحص الوثيقة من "
                    "الصفحة الرئيسية للحصول على تقرير جديد.")


# ===========================================================================
# Optional modules — written on other tracks, loaded defensively
# ===========================================================================

_optional_cache: Dict[str, Any] = {}
_optional_lock = threading.Lock()


def _optional(name: str) -> Any:
    """
    Import an optional module, or return None if it is absent or broken.

    A successful import is cached. A failed one is retried on the next call,
    so a module that lands (or is fixed) while the server runs is picked up
    without a restart.
    """
    with _optional_lock:
        if name in _optional_cache:
            return _optional_cache[name]
        try:
            mod = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - ImportError, SyntaxError mid-edit, ...
            if not isinstance(exc, ModuleNotFoundError):
                sys.stderr.write(f"  [optional] {name} failed to import: "
                                 f"{type(exc).__name__}: {exc}\n")
            return None
        _optional_cache[name] = mod
        return mod


def _module_present(name: str) -> bool:
    """True if a module exists on disk, without importing it (no heavy deps)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _check_terms_fn() -> Optional[Callable[..., Any]]:
    mod = _optional(TERMS_MODULE)
    fn = getattr(mod, "check_terms", None) if mod else None
    return fn if callable(fn) else None


def _adapt_document_fn() -> Optional[Callable[..., Any]]:
    mod = _optional(CLARITY_MODULE)
    fn = getattr(mod, "adapt_document", None) if mod else None
    return fn if callable(fn) else None


def features() -> Dict[str, Any]:
    return {
        "clarity": _adapt_document_fn() is not None,
        "terms": _check_terms_fn() is not None,
        "semantic_module": _module_present("mizan.semantic"),
        "review": True,
        "clarity_model_requested": USE_CLARITY_MODEL,
    }


# ===========================================================================
# Engine work runs on ONE worker thread
# ===========================================================================
#
# The engine holds one SQLite connection and an in-memory verse cache, neither
# designed for concurrent use. Serialising engine work on a single worker is
# the simplest correct answer for a local tool, and it gives every check a hard
# timeout: the HTTP thread stops waiting after CHECK_TIMEOUT_S and tells the
# user what to do, instead of hanging.

_WORKER = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="mizan-engine")


def on_engine(fn: Callable[..., Any], *args: Any, timeout: float = CHECK_TIMEOUT_S,
              **kwargs: Any) -> Any:
    future = _WORKER.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        # A check still waiting in the queue is dropped, so a client that has
        # given up does not hold the single engine worker hostage. A check that
        # is already running cannot be interrupted; it finishes and is discarded.
        future.cancel()
        raise ApiError(
            504, "CHECK_TIMEOUT",
            "استغرق الفحص أكثر من %d ثانية فأُوقف انتظاره. قسّم الوثيقة إلى أجزاء أقصر "
            "وافحص كل جزء على حدة، أو انتظر دقيقة ثم أعد المحاولة." % int(timeout),
        ) from None


# Pre-built detectors, one per language. Building one costs 0.2-1.5 s; reusing
# it brings a check down to well under a second. Only touched on the worker.
_DETECTORS: Dict[str, Any] = {}
_ARABIC_DETECTOR: List[Any] = []


def _detector_for(lang: str) -> Any:
    if lang in _DETECTORS:
        return _DETECTORS[lang]
    try:
        from mizan.detect import SpanDetector  # type: ignore
        det = SpanDetector(ENGINE, lang)
    except Exception as exc:  # noqa: BLE001 - let check_document build its own
        sys.stderr.write(f"  [detect] could not pre-build {lang} detector: {exc}\n")
        return None
    _DETECTORS[lang] = det
    return det


def _arabic_detector() -> Any:
    if _ARABIC_DETECTOR:
        return _ARABIC_DETECTOR[0]
    try:
        from mizan.arabic import ArabicDetector  # type: ignore
        det = ArabicDetector()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"  [arabic] could not pre-build detector: {exc}\n")
        return None
    _ARABIC_DETECTOR.append(det)
    return det


def _warm_up() -> None:
    """
    Load everything a first request would otherwise wait for.

    The detectors and, when the ML extra is installed, the bge-m3 model and
    its per-language vector indexes. Without this the first judge to open the
    demo waits 3-10 s for the model to load. When the extra is absent,
    semantic.warmup() is a no-op and the app runs on the deterministic tiers.
    """
    langs = target_languages()
    if "en" in langs:
        _detector_for("en")
    _arabic_detector()
    try:
        from mizan import semantic
        st = semantic.warmup(langs)
        print(f"  semantic     {'on — ' + st.get('model', '') if st.get('semantic_available') else 'off (deterministic tiers only)'}",
              flush=True)
    except Exception as exc:   # noqa: BLE001 — the AI tier is optional by design
        print(f"  semantic     off ({type(exc).__name__}); deterministic tiers only",
              flush=True)


# ===========================================================================
# Fallback quote detection — the degraded path
# ===========================================================================
#
# Used only when the full pipeline (report.check_document) is missing or
# raises. Deterministic and deliberately narrow: it finds quoted spans carrying
# a printed (surah:ayah) reference. An unreferenced span is UNRESOLVED, never
# guessed at. The UI says plainly when this path was used.

_REF_PATTERNS = [
    re.compile(
        r"[\(\[]\s*(?:Qur['’]?an|Quran|Koran)?\s*[:\s]?\s*"
        r"(\d{1,3})\s*[:ۖ]\s*(\d{1,3})\s*[\)\]]",
        re.IGNORECASE,
    ),
    re.compile(r"(?:Qur['’]?an|Quran|Surah)\s+(\d{1,3})\s*:\s*(\d{1,3})", re.IGNORECASE),
]
_QUOTE_SPAN = re.compile(r"[\"“”«۝﴾]([^\"“”»﴿]{8,600}?)[\"“”»﴿]")

_SCRIPT_RANGES = (
    ("arabic", 0x0600, 0x06FF),
    ("bengali", 0x0980, 0x09FF),
    ("devanagari", 0x0900, 0x097F),
    ("latin", 0x0041, 0x024F),
)


def _dominant_script(text: str) -> Optional[str]:
    tally: Dict[str, int] = {}
    for ch in text:
        if not ch.isalpha():
            continue
        cp = ord(ch)
        for name, lo, hi in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                tally[name] = tally.get(name, 0) + 1
                break
    return max(tally, key=lambda k: tally[k]) if tally else None


def _find_ref_near(text: str, start: int, end: int) -> Optional[Tuple[int, int]]:
    """A printed (surah:ayah) just after a quoted span — or just before it."""
    tail = text[end:end + 90]
    for pat in _REF_PATTERNS:
        m = pat.search(tail)
        if m:
            return int(m.group(1)), int(m.group(2))
    head = text[max(0, start - 90):start]
    for pat in _REF_PATTERNS:
        found = list(pat.finditer(head))
        if found:
            return int(found[-1].group(1)), int(found[-1].group(2))
    return None


def _best_window(quote: str, approved: str) -> float:
    """Score a quote against the best word-window of an approved verse."""
    q_words = normalize(quote).split()
    a_words = normalize(approved).split()
    if not q_words or not a_words:
        return 0.0
    n = len(q_words)
    if n >= len(a_words):
        return sim(quote, approved)
    q_norm, best = " ".join(q_words), 0.0
    for width in {n, max(1, n - 2), max(1, n - 1), n + 1, n + 2}:
        if width > len(a_words):
            continue
        for i in range(len(a_words) - width + 1):
            best = max(best, sim(q_norm, " ".join(a_words[i:i + width])))
    return best


def _diff_ops(published: str, approved: str) -> List[Dict[str, Any]]:
    """Word diff in report.py's shape: [{op, published:[...], approved:[...]}]."""
    from difflib import SequenceMatcher

    pub, app = published.split(), (approved or "").split()
    sm = SequenceMatcher(None, [normalize(w) for w in pub], [normalize(w) for w in app],
                         autojunk=False)
    return [{"op": tag, "published": pub[i1:i2], "approved": app[j1:j2]}
            for tag, i1, i2, j1, j2 in sm.get_opcodes()]


def _fallback_findings(translation: str, lang: str) -> List[Dict[str, Any]]:
    """Quoted spans with a printed reference, attributed by window scan."""
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for m in _QUOTE_SPAN.finditer(translation):
        quote = m.group(1).strip()
        ref = _find_ref_near(translation, m.start(), m.end())
        if not quote or (quote, ref) in seen:
            continue
        seen.add((quote, ref))
        start_word = len(translation[:m.start()].split())
        end_word = max(start_word + 1, len(translation[:m.end()].split()))
        base = {"published_text": quote, "lang": lang, "tier": "reference",
                "start_word": start_word, "end_word": end_word, "score": 0.0,
                "n_compared": 0, "runners_up": [], "diff": []}
        if not ref:
            out.append(dict(base, state="UNRESOLVED", surah=None, ayah=None))
            continue
        surah, ayah = ref
        base.update(surah=surah, ayah=ayah)
        script, expected = _dominant_script(quote), LANG_SCRIPT.get(lang)
        if script and expected and script != expected:
            out.append(dict(base, state="NO_APPROVED_TRANSLATION", reason="script_mismatch"))
            continue
        scored = []
        for bid in ENGINE.editions(lang):
            approved = ENGINE.verse(bid, surah, ayah)
            if approved:
                scored.append((_best_window(quote, approved), bid, approved))
        if not scored:
            out.append(dict(base, state="NO_APPROVED_TRANSLATION"))
            continue
        scored.sort(key=lambda r: -r[0])
        score, bid, approved = scored[0]
        state = "MATCH" if score >= T_MATCH else "NEAR" if score >= T_NEAR else "UNATTRIBUTED"
        if state == "MATCH":
            # MATCH means the words ARE an approved translation's words.
            exact = next((b for s_, b, a_ in scored if verbatim(quote, a_)), None)
            if exact is None:
                state = "NEAR"
            else:
                bid = exact
                approved = ENGINE.verse(bid, surah, ayah)
        out.append(dict(
            base, state=state, score=round(score, 3), approved_text=approved,
            attributed_to=bid if state != "UNATTRIBUTED" else None,
            attributed_title=ENGINE.books[bid]["title"] if state != "UNATTRIBUTED" else None,
            n_compared=len(scored),
            runners_up=[(b, round(s, 3)) for s, b, _ in scored[1:4]],
            diff=_diff_ops(quote, approved),
        ))
    return out


# ===========================================================================
# The check pipeline
# ===========================================================================


def _have_full_pipeline() -> bool:
    mod = _optional("mizan.report")
    return callable(getattr(mod, "check_document", None))


def _attribute(translation: str, arabic: str, lang: str
               ) -> Tuple[List[Dict[str, Any]], Optional[str], str, Optional[str]]:
    """
    Run attribution. Returns (raw findings, report gate, pipeline, note).

    The full pipeline is report.check_document (Arabic resolution, span
    detection, boundary refinement, attribution). If it is missing or raises,
    the fallback runs and the response says so — a degraded answer is labelled
    as degraded, never passed off as the real one.
    """
    report_mod = _optional("mizan.report")
    if report_mod is not None and callable(getattr(report_mod, "check_document", None)):
        kwargs: Dict[str, Any] = {"arabic_text": arabic or None}
        if lang in target_languages():
            det = _detector_for(lang)
            if det is not None:
                kwargs["detector"] = det
        if arabic:
            adet = _arabic_detector()
            if adet is not None:
                kwargs["arabic_detector"] = adet
        try:
            rep = report_mod.check_document(ENGINE, translation, lang, **kwargs)
            d = rep.as_dict() if hasattr(rep, "as_dict") else dict(rep)
            return list(d.get("findings") or []), d.get("verdict"), "full", None
        except Exception as exc:  # noqa: BLE001 - degrade, loudly
            traceback.print_exc()
            note = ("تعذّر تشغيل مسار الكشف الكامل (%s)، فاستُخدم المسار الاحتياطي الحتمي: "
                    "يفحص الاقتباسات المحاطة بعلامات تنصيص والتي تحمل مرجعًا مطبوعًا فقط."
                    % type(exc).__name__)
            return _fallback_findings(translation, lang), None, "fallback", note
    note = ("مسار الكشف الكامل غير متاح، فاستُخدم المسار الاحتياطي الحتمي: يفحص الاقتباسات "
            "المحاطة بعلامات تنصيص والتي تحمل مرجعًا مطبوعًا فقط.")
    return _fallback_findings(translation, lang), None, "fallback", note


def _tier_info(tier: Optional[str]) -> Tuple[str, str, bool]:
    """(label, explanation, came_from_a_model) for a detection tier."""
    is_ai = bool(tier) and any(k in tier.lower() for k in AI_TIER_KEYS)
    if tier in TIER_LABELS:
        label, why = TIER_LABELS[tier]
    elif is_ai:
        label, why = TIER_LABELS["semantic"]
    else:
        label, why = ("طريقة كشف: %s" % tier, "")
    return label, why, is_ai


def _book_ref(book_id: Optional[int], surah: Optional[int], ayah: Optional[int]
              ) -> Optional[Dict[str, Any]]:
    if not book_id or book_id not in ENGINE.books:
        return None
    out = {"book_id": book_id, "title": ENGINE.books[book_id]["title"]}
    if surah and ayah:
        out["source_url"] = translation_url(book_id, surah, ayah)
    return out


def _closest_book(lang: str, surah: int, ayah: int, approved: Optional[str]) -> Optional[int]:
    """Which edition's text is `approved`? (report.py gives the text, not the id,
    when nothing is attributed.)"""
    if not approved:
        return None
    for bid in ENGINE.editions(lang):
        if ENGINE.verse(bid, surah, ayah) == approved:
            return bid
    return None


def canonical_finding(idx: int, f: Dict[str, Any], lang: str) -> Dict[str, Any]:
    """
    One finding in the shape the UI, the report and the review queue all use,
    whichever pipeline produced it.
    """
    surah = f.get("surah") or None
    ayah = f.get("ayah") or None
    if not (surah and ayah):
        surah = ayah = None
    state = str(f.get("state") or "UNRESOLVED")
    approved = f.get("approved_text")
    tier = f.get("tier")
    tier_label, tier_why, tier_ai = _tier_info(tier)

    attributed_id = f.get("attributed_to")
    # Only a MATCH is attributed. A NEAR names its closest approved
    # translation for comparison, never as the source of the published words.
    attributed = _book_ref(attributed_id, surah, ayah) if state == "MATCH" else None
    closest_id = attributed_id or (_closest_book(lang, surah, ayah, approved) if surah else None)
    closest = _book_ref(closest_id, surah, ayah)

    runners = []
    for r in f.get("runners_up") or []:
        if isinstance(r, dict):
            bid, score = r.get("book_id"), r.get("score")
        else:
            bid, score = r[0], r[1]
        if bid in ENGINE.books:
            runners.append({"book_id": bid, "title": ENGINE.books[bid]["title"],
                            "score": score})

    return {
        "idx": idx,
        "ref": f"{surah}:{ayah}" if surah else None,
        "surah": surah,
        "ayah": ayah,
        "state": state,
        "state_label": STATE_LABELS.get(state, state),
        "refer": state not in CLEARING_STATES or bool(f.get("flags")),
        "flags": list(f.get("flags") or []),
        "flag_messages": [_flag_message(x, (f.get("flag_detail") or {}).get(x), surah, ayah)
                          for x in (f.get("flags") or [])],
        "lang": f.get("lang") or lang,
        "score": float(f.get("score") or 0.0),
        "published_text": f.get("published_text") or f.get("quote") or "",
        "approved_text": approved,
        "attributed": attributed,
        "closest": closest,
        "verse_url": verse_url(surah, ayah) if surah else None,
        "tier": tier,
        "tier_label": tier_label,
        "tier_explanation": tier_why,
        "ai_tier": tier_ai,
        "start_word": f.get("start_word"),
        "end_word": f.get("end_word"),
        "n_compared": int(f.get("n_compared") or 0),
        "runners_up": runners,
        "diff": f.get("diff") or [],
        "n_edits": f.get("n_edits"),
        "reason": f.get("reason"),
    }


def _flag_message(code: str, detail: Optional[str], surah: Optional[int],
                  ayah: Optional[int]) -> str:
    base = FLAG_LABELS.get(code, code)
    if code == "extra_words_in_quote" and detail:
        return f"{base}: «{detail}»"
    if code == "citation_mismatch" and detail:
        return f"{base}: المطبوع {detail}، والنص الموجود نص الآية {surah}:{ayah}"
    if code == "no_such_verse" and detail:
        return f"{base}: {detail}"
    return base


def document_verdict(findings: List[Dict[str, Any]], gate: Optional[str]) -> str:
    """
    The display verdict. One referred item pulls the whole document to REFER —
    and if the report layer's own gate says REFER, so does this, whatever the
    findings look like. A gate whose result can be diluted is not a gate.
    """
    if gate == "REFER" or any(f["refer"] for f in findings):
        return "REFER"
    if not findings:
        return "NO_QUOTES"
    states = {f["state"] for f in findings}
    if "NO_APPROVED_TRANSLATION" in states:
        return "NO_APPROVED_TRANSLATION"
    return "ATTRIBUTED"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _terms(arabic: str, translation: str, lang: str) -> Optional[Dict[str, Any]]:
    """Panel 2, when mizan.terms is present. None means: hide the section."""
    fn = _check_terms_fn()
    if fn is None:
        return None
    try:
        rep = fn(arabic or None, translation, lang)
        d = rep.as_dict() if hasattr(rep, "as_dict") else dict(rep)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return {"available": True, "findings": [], "counts": {},
                "error": "تعذّر فحص المصطلحات (%s). نتيجة الإسناد أعلاه لم تتأثر." % type(exc).__name__}
    keys = ("term_ar", "approved_en", "status", "found", "suggestion", "source_url")
    findings = [{k: f.get(k) for k in keys} for f in (d.get("findings") or [])
                if isinstance(f, dict)]
    out = {"available": True, "findings": findings, "counts": d.get("counts") or {}}
    # Additive context the module may offer (glossary provenance, scope notes).
    for extra in ("glossary", "notes", "mode"):
        if d.get(extra):
            out[extra] = d[extra]
    return out


def _as_text(value: Any) -> str:
    """A contract field that may arrive as a string or a list of strings."""
    if isinstance(value, (list, tuple)):
        return " / ".join(str(v) for v in value if v)
    return "" if value is None else str(value)


def _clarity(translation: str, level: str, word_spans: List[Tuple[int, int]],
             lang: str = "en") -> Optional[Dict[str, Any]]:
    """
    Panel 3, when mizan.clarity is present. Every located quotation is passed
    in as a frozen span, so the adapter never sees scripture.
    """
    fn = _adapt_document_fn()
    if fn is None:
        return None
    frozen = [SimpleNamespace(start_word=s, end_word=e) for s, e in word_spans]
    try:
        # lang matters: the ruling screen exists for English only, and the
        # scripture guard compares against that language's approved texts.
        try:
            rep = fn(translation, level, findings=frozen, use_model=USE_CLARITY_MODEL,
                     lang=lang)
        except TypeError:          # an adapter without the lang parameter
            rep = fn(translation, level, findings=frozen, use_model=USE_CLARITY_MODEL)
        d = rep.as_dict() if hasattr(rep, "as_dict") else dict(rep)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return {"available": True, "level": level,
                "error": "تعذّر تشغيل لوحة الوضوح (%s). نتيجة الإسناد أعلاه لم تتأثر، "
                         "ولم يُعدَّل أي نص." % type(exc).__name__}
    d["available"] = True
    d["model_requested"] = USE_CLARITY_MODEL
    return d


def _word_spans(findings: List[Dict[str, Any]]) -> List[Tuple[int, int]]:
    return [(f["start_word"], f["end_word"]) for f in findings
            if isinstance(f.get("start_word"), int) and isinstance(f.get("end_word"), int)]


AI_TIER_PARTIAL_NOTE = ("النص أطول من حدّ الفحص بالذكاء الاصطناعي: فُحص النص كله بالمطابقة "
                        "النصية، وفُحص جزء منه فقط بالطبقة الدلالية. لفحص كامل، قسّم النص "
                        "إلى أجزاء أقصر.")


def _ai_tier_complete(lang: str, pipeline: str) -> Optional[bool]:
    """Did the AI tier see the whole text? None when it did not run at all."""
    if pipeline != "full" or lang not in target_languages():
        return None
    det = _detector_for(lang)
    sem = getattr(det, "last_semantic", None) if det is not None else None
    if not isinstance(sem, dict) or not sem.get("used"):
        return None
    return bool(sem.get("complete", True))


def run_check(translation: str, arabic: str, lang: str, level: str) -> Dict[str, Any]:
    """The whole check, on the engine worker. Persists a minimal record."""
    t0 = time.perf_counter()
    raw, gate, pipeline, note = _attribute(translation, arabic, lang)
    ai_tier_complete = _ai_tier_complete(lang, pipeline)
    if ai_tier_complete is False:
        note = " ".join(x for x in (note, AI_TIER_PARTIAL_NOTE) if x)
    findings = [canonical_finding(i, f, lang) for i, f in enumerate(raw)]
    verdict = document_verdict(findings, gate)
    counts: Dict[str, int] = {}
    for f in findings:
        counts[f["state"]] = counts.get(f["state"], 0) + 1
    tiers_used = sorted({str(f["tier"]) for f in findings if f["tier"]})
    ai_assisted = any(f["ai_tier"] for f in findings)
    terms = _terms(arabic, translation, lang)

    record: Dict[str, Any] = {
        "schema": "mizan.check/1",
        "check_id": new_check_id(),
        "created_at": utc_now(),
        "lang": lang,
        "lang_name_ar": LANG_NAMES_AR.get(lang, lang),
        "lang_dir": LANG_DIR.get(lang, "ltr"),
        "document_verdict": verdict,
        "verdict_label": VERDICT_LABELS[verdict],
        "gate": "REFER" if verdict == "REFER" else "CLEAR",
        "refer_refs": [f["ref"] or "؟" for f in findings if f["refer"]],
        "counts": counts,
        "n_editions": len(ENGINE.editions(lang)),
        "index_version": INDEX_VERSION,
        "source": "quranpedia.net",
        "source_url": QURANPEDIA,
        "pipeline": pipeline,
        "pipeline_note": note,
        "tiers_used": tiers_used,
        "ai_assisted": ai_assisted,
        # Did the AI tier even run? False means a deterministic-only answer,
        # which must be labelled as such rather than look like a full check.
        "ai_tier_available": _semantic_available(lang),
        # None: the AI tier did not run. False: it ran on part of the text
        # only (token budget), and pipeline_note says so.
        "ai_tier_complete": ai_tier_complete,
        "ai_disclosure": AI_DISCLOSURE if ai_assisted else None,
        "had_arabic_source": bool(arabic),
        "findings": findings,
        "terms": terms,
    }
    if RETENTION_DAYS > 0:
        STORE.purge_checks(RETENTION_DAYS)
    STORE.save_check(record, text_sha256=_sha256(translation))

    response = dict(record)
    response["clarity"] = _clarity(translation, level, _word_spans(findings), lang)
    response["links"] = check_links(record["check_id"])
    response["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return response


def check_links(check_id: str) -> Dict[str, str]:
    return {
        "report_html": f"/report/{check_id}.html",
        "report_download": f"/report/{check_id}.html?download=1",
        "export_json": f"/api/check/{check_id}/export.json",
    }


# ===========================================================================
# Payloads
# ===========================================================================


def languages_payload() -> Dict[str, Any]:
    langs = []
    for code, book_ids in target_languages().items():
        langs.append({
            "code": code,
            "name_ar": LANG_NAMES_AR.get(code, code),
            "direction": LANG_DIR.get(code, "ltr"),
            "script": LANG_SCRIPT.get(code),
            "n_translations": len(book_ids),
            "translations": [{"book_id": b, "title": ENGINE.books[b]["title"]} for b in book_ids],
        })
    langs.sort(key=lambda row: -row["n_translations"])
    return {"languages": langs, "index_version": INDEX_VERSION, "source": "quranpedia.net"}


def health_payload() -> Dict[str, Any]:
    pipeline = "full" if _have_full_pipeline() else "fallback"
    return {
        "status": "ok",
        "index_version": INDEX_VERSION,
        "n_translations": sum(len(ids) for ids in target_languages().values()),
        "n_languages": len(target_languages()),
        "n_verses": HEALTH_COUNTS["n_verses"],
        "n_texts": HEALTH_COUNTS["n_texts"],
        "source": "quranpedia.net",
        "source_url": QURANPEDIA,
        "pipeline": pipeline,
        "thresholds": {"match": T_MATCH, "near": T_NEAR},
        "features": features(),
        "levels": LEVELS,
        "default_level": DEFAULT_LEVEL,
        "limits": {
            "max_body_bytes": MAX_BODY_BYTES,
            "max_translation_chars": MAX_TRANSLATION_CHARS,
            "max_arabic_chars": MAX_ARABIC_CHARS,
            "check_timeout_s": CHECK_TIMEOUT_S,
        },
        "review": STORE.counts(),
        "retention_days": RETENTION_DAYS,
        "privacy": PRIVACY_NOTE % RETENTION_DAYS if RETENTION_DAYS > 0 else PRIVACY_NOTE.replace(
            "الفحص الذي لم يُرسل منه بند للمراجعة يُحذف بعد %s يومًا. ", ""),
        "offline": True,
        "semantic": _semantic_status(),
    }


def _semantic_status() -> Dict[str, Any]:
    """Whether the AI tier is installed and loaded — for monitors and the UI."""
    try:
        from mizan import semantic
        return semantic.status()
    except Exception as exc:   # noqa: BLE001 — optional extra
        return {"semantic_available": False, "reason": type(exc).__name__}


def _semantic_available(lang: str) -> bool:
    try:
        from mizan import semantic
        return bool(semantic.available(lang))
    except Exception:          # noqa: BLE001
        return False


def export_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    """A saved check, plus its review history, as one traceable document."""
    rec = {k: v for k, v in record.items() if k != "text_sha256"}
    rec["translation_sha256"] = record.get("text_sha256")
    rec["review"] = [_with_links(it) for it in STORE.items_for_check(record["check_id"])]
    rec["exported_at"] = utc_now()
    rec["arabic_text_attribution"] = {
        "source": "Tanzil.net", "url": "https://tanzil.net",
        "license": "Creative Commons Attribution 3.0 — verbatim, no modification",
    }
    rec["data_attribution"] = {
        "source": "Quranpedia.net",
        "url": QURANPEDIA,
        "dump_version": record.get("index_version"),
        "note": "نصوص الترجمات المعتمدة منقولة حرفيًا من تفريغ Quranpedia.net دون تعديل، "
                "وتبقى ملكًا لناشريها.",
    }
    rec["tool"] = {
        "name": "MIZAN — مِيزان",
        "statement": "لا يُفتي · لا يترجم · يُسنِد فقط",
        "nature": "أداة آلية مدعومة بالذكاء الاصطناعي، وليست مختصًا بشريًا. تُسنِد الاقتباس "
                  "إلى ترجمة معتمدة منشورة ولا تحكم على صحة نص.",
    }
    return rec


# ===========================================================================
# Report — self-contained Arabic RTL HTML
# ===========================================================================


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _report_css() -> str:
    try:
        with open(os.path.join(WEB, "report.css"), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return "body{font-family:system-ui,sans-serif;direction:rtl;max-width:900px;margin:auto;padding:16px}"


def _diff_html(ops: List[Dict[str, Any]], side: str) -> str:
    out = []
    for op in ops or []:
        words = op.get(side) or []
        if not words:
            continue
        text = _e(" ".join(words))
        if op.get("op") == "equal":
            out.append(text)
        else:
            out.append(f"<mark class='{'pub' if side == 'published' else 'app'}'>{text}</mark>")
    return " ".join(out)


def render_report_html(record: Dict[str, Any], items: List[Dict[str, Any]]) -> str:
    """The downloadable report. No script, no external resource of any kind."""
    lang_dir = record.get("lang_dir") or "ltr"
    by_idx = {it["finding_idx"]: it for it in items}
    verdict = record.get("document_verdict", "")
    v_cls = {"REFER": "refer", "ATTRIBUTED": "ok"}.get(verdict, "near")
    f_html = []

    for f in record.get("findings") or []:
        state = f.get("state", "")
        s_cls = {"MATCH": "match", "NEAR": "near"}.get(state, "refer" if f.get("refer") else "none")
        rows = []
        rows.append(f"<tr><th>كيف وُجد</th><td>{_e(f.get('tier_label'))}"
                    + (" <span class='ai'>ذكاء اصطناعي</span>" if f.get("ai_tier") else "")
                    + "</td></tr>")
        if f.get("score"):
            rows.append(f"<tr><th>درجة التشابه</th><td class='num'>{float(f['score']):.3f}</td></tr>")
        if f.get("attributed"):
            a = f["attributed"]
            rows.append(f"<tr><th>الترجمة المُسنَد إليها</th><td><b>{_e(a['title'])}</b>"
                        f" <span class='muted'>(معرّف {a['book_id']} · نسخة {_e(record.get('index_version'))})</span></td></tr>")
        elif f.get("closest"):
            c = f["closest"]
            rows.append(f"<tr><th>أقرب ترجمة معتمدة</th><td>{_e(c['title'])} "
                        f"<span class='muted'>(للمقارنة فقط — لا إسناد)</span></td></tr>")
        pub = f.get("published_text") or ""
        rows.append(
            "<tr><th>النص المنشور</th><td>"
            + (f"<div class='txt' dir='{lang_dir}'>{_diff_html(f.get('diff'), 'published') or _e(pub)}</div>"
               if pub else "<span class='muted'>— لم يُعثر على موضعه في الترجمة</span>")
            + "</td></tr>")
        if f.get("approved_text"):
            label = "النص المعتمد" if f.get("attributed") else "نص أقرب ترجمة معتمدة"
            rows.append(
                f"<tr><th>{label}<div class='muted small'>منقول حرفيًا من الفهرس</div></th>"
                f"<td><div class='txt approved' dir='{lang_dir}'>"
                f"{_diff_html(f.get('diff'), 'approved') or _e(f['approved_text'])}</div></td></tr>")
        links = []
        src = (f.get("attributed") or f.get("closest") or {}).get("source_url")
        if src:
            links.append(f"<a href='{_e(src)}' rel='noopener noreferrer'>افتح الترجمة في المصدر ↗</a>")
        if f.get("verse_url"):
            links.append(f"<a href='{_e(f['verse_url'])}' rel='noopener noreferrer'>الآية في المصدر ↗</a>")
        if links:
            rows.append("<tr><th>المصدر</th><td>" + " · ".join(links) + "</td></tr>")

        if f.get("flag_messages"):
            rows.append("<tr><th>سبب الإحالة</th><td>"
                        + "<br>".join(_e(m) for m in f["flag_messages"]) + "</td></tr>")
        if state == "NEAR":
            rows.append("<tr><th>سبب الإحالة</th><td>كلمات الاقتباس تختلف عن أقرب ترجمة "
                        "معتمدة؛ أي تغيير في نص آية، ولو كلمة، يُعرض على مختص قبل النشر.</td></tr>")

        it = by_idx.get(f.get("idx"))
        if it:
            hist = "".join(
                f"<li><b>{_e(ev['decision_label'])}</b> — {_e(ev['reviewer'])} "
                f"<span class='muted num'>{_e(ev['created_at'])}</span>"
                + (f"<div class='note'>{_e(ev['note'])}</div>" if ev.get("note") else "")
                + (f"<div class='txt approved' dir='{lang_dir}'>{_e(ev['replacement']['text'])}"
                   f"<div class='muted small'>{_e(ev['replacement']['title'])}</div></div>"
                   if ev.get("replacement") else "")
                + "</li>" for ev in it["events"])
            rows.append(f"<tr><th>المراجعة البشرية</th><td>بند مراجعة #{it['id']} — "
                        f"<b>{_e(it['status_label'])}</b>"
                        + (f"<ol class='hist'>{hist}</ol>" if hist else "") + "</td></tr>")
        elif f.get("refer"):
            rows.append("<tr><th>المراجعة البشرية</th><td class='muted'>لم يُرسَل إلى طابور "
                        "المراجعة بعد.</td></tr>")

        f_html.append(
            f"<section class='finding'><header><span class='ref'>{_e(f.get('ref') or '؟')}</span>"
            f"<span class='state {s_cls}'>{_e(f.get('state_label'))}</span>"
            f"<span class='muted small'>بند {int(f.get('idx', 0)) + 1}</span></header>"
            f"<table>{''.join(rows)}</table></section>")

    terms = record.get("terms") or {}
    t_html = ""
    if terms.get("findings"):
        t_rows = []
        for t in terms["findings"]:
            link = (f"<a href='{_e(t['source_url'])}' rel='noopener noreferrer'>المصدر ↗</a>"
                    if t.get("source_url") else "")
            t_rows.append(
                f"<tr><td>{_e(t.get('term_ar'))}</td><td dir='ltr'>{_e(_as_text(t.get('approved_en')))}</td>"
                f"<td>{_e(TERM_STATUS_AR.get(t.get('status'), t.get('status')))}</td>"
                f"<td dir='{lang_dir}'>{_e(_as_text(t.get('found')) or '—')}</td>"
                f"<td dir='ltr'>{_e(_as_text(t.get('suggestion')))}</td><td>{link}</td></tr>")
        glossary = terms.get("glossary") or {}
        ref_line = (f"<p class='muted small'>المرجع: {_e(glossary.get('reference'))}"
                    f" — {_e(glossary.get('terms'))} مصطلحًا. الملاحظات قواعد حتمية لا يولّدها نموذج.</p>"
                    if glossary.get("reference") else "")
        t_html = ("<h2>المصطلحات</h2><table class='terms'><tr><th>المصطلح</th><th>المعتمد</th>"
                  "<th>الحالة</th><th>الموجود</th><th>ملاحظة مِيزان</th><th></th></tr>"
                  f"{''.join(t_rows)}</table>{ref_line}")

    counts = record.get("counts") or {}
    count_bits = " · ".join(f"{_e(STATE_LABELS.get(k, k))}: <b>{v}</b>" for k, v in counts.items())
    ai_line = (f"<p class='ai-note'><b>إفصاح:</b> {_e(record.get('ai_disclosure'))}</p>"
               if record.get("ai_assisted") else
               "<p class='muted'>لم تُستخدم أي طبقة ذكاء اصطناعي في كشف بنود هذا الفحص؛ "
               "كل بند وُجد بمرجع مطبوع أو بمطابقة نصية حتمية.</p>")
    pipe_line = (f"<p class='warn'>{_e(record.get('pipeline_note'))}</p>"
                 if record.get("pipeline_note") else "")
    refer_line = ""
    if verdict == "REFER":
        refs = "، ".join(f"<span class='num'>{_e(r)}</span>" for r in record.get("refer_refs") or [])
        refer_line = f"<p>سببه: {refs}. بندُ إحالة واحد يسحب الوثيقة كلها إلى المراجعة.</p>"

    return f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>تقرير إسناد مِيزان — {_e(record.get('check_id'))}</title>
<style>{_report_css()}</style>
</head>
<body>
<header class="top">
  <div class="brand">مِيزان<span>.</span></div>
  <div class="sub">تقرير إسناد الاقتباس القرآني</div>
  <div class="badge">لا يُفتي · لا يترجم · يُسنِد فقط</div>
</header>
<main>
<section class="verdict {v_cls}">
  <div class="vlabel">حكم الوثيقة كاملة — لا حالة بندٍ مفرد</div>
  <div class="big">{_e(record.get('verdict_label'))}</div>
  {refer_line}
</section>
<table class="meta">
  <tr><th>معرّف الفحص</th><td class="num">{_e(record.get('check_id'))}</td></tr>
  <tr><th>وقت الفحص (UTC)</th><td class="num">{_e(record.get('created_at'))}</td></tr>
  <tr><th>لغة الترجمة</th><td>{_e(record.get('lang_name_ar'))} <span class="muted">({_e(record.get('lang'))})</span></td></tr>
  <tr><th>نسخة الفهرس</th><td class="num">{_e(record.get('index_version'))}</td></tr>
  <tr><th>مصدر البيانات</th><td><a href="{QURANPEDIA}" rel="noopener noreferrer">Quranpedia.net</a> — نصوص الترجمات المعتمدة منقولة حرفيًا من تفريغ الموقع (نسخة {_e(record.get('index_version'))}) دون تعديل، وتبقى ملكًا لناشريها.</td></tr>
  <tr><th>النص العربي</th><td><a href="https://tanzil.net" rel="noopener noreferrer">Tanzil.net</a> — Creative Commons Attribution 3.0، منقول حرفيًا دون تعديل.</td></tr>
  <tr><th>قورِن بـ</th><td>{_e(record.get('n_editions'))} ترجمة معتمدة بهذه اللغة</td></tr>
  <tr><th>البنود</th><td>{count_bits or 'لا شيء'}</td></tr>
  <tr><th>بصمة الترجمة</th><td class="num small hash">SHA-256 {_e(record.get('text_sha256'))}</td></tr>
</table>
{ai_line}
{pipe_line}
<h2>البنود</h2>
{''.join(f_html) or "<p class='muted'>لم يُعثر على اقتباس قرآني في الترجمة.</p>"}
{t_html}
<footer>
  <p><b>ما يعنيه هذا التقرير:</b> مِيزان يُسنِد كل اقتباس إلى ترجمة معتمدة منشورة بمقارنة نصية حتمية،
  ولا يحكم على صحة نص. «لا يُسنَد» لا تعني «خطأ»: قد يكون النص منقولًا عن ترجمة معتمدة خارج هذا الفهرس،
  ولذلك يُعرض على مختص قبل النشر.</p>
  <p><b>طبيعة الأداة:</b> أداة آلية مدعومة بالذكاء الاصطناعي، وليست مختصًا بشريًا ولا مفتيًا. لا تولّد نصًا شرعيًا ولا تصحّحه.</p>
  <p class="muted small">لم يُحفظ نص الوثيقة كاملًا؛ البصمة أعلاه تثبت أي نص فُحص دون الاحتفاظ به.</p>
</footer>
</main>
</body>
</html>
"""


TERM_STATUS_AR = {
    "APPROVED": "معتمد",
    "VARIANT": "صيغة بديلة",
    "MISSING": "غائب عن الترجمة",
    "NOT_IN_GLOSSARY": "خارج المعجم",
}

# ===========================================================================
# Request handling — framework-free
# ===========================================================================


@dataclass
class Request:
    method: str
    path: str
    query: Dict[str, str]
    body: Any = None
    params: Dict[str, str] = field(default_factory=dict)


@dataclass
class Response:
    status: int
    body: bytes
    ctype: str = "application/json; charset=utf-8"
    headers: Dict[str, str] = field(default_factory=dict)


def json_response(obj: Any, status: int = 200, headers: Optional[Dict[str, str]] = None) -> Response:
    return Response(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                    headers=headers or {})


def _body(req: Request) -> Dict[str, Any]:
    if not isinstance(req.body, dict):
        raise ApiError(400, "BAD_JSON",
                       "جسم الطلب يجب أن يكون كائن JSON، مثل {\"translation\": \"...\", \"lang\": \"en\"}.")
    return req.body


def _str_field(body: Dict[str, Any], name: str) -> str:
    value = body.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiError(400, "BAD_FIELD", f"الحقل «{name}» يجب أن يكون نصًّا. أرسله سلسلة نصية.")
    return value


_LANG_RE = re.compile(r"^[a-z]{2,3}$")


def _lang(body: Dict[str, Any]) -> str:
    lang = (_str_field(body, "lang") or "en").strip().lower()
    if not _LANG_RE.match(lang):
        raise ApiError(400, "BAD_LANG",
                       "رمز اللغة غير صالح. استخدم رمزًا من حرفين مثل en أو ur، واختره من قائمة اللغات.")
    return lang


def _level(body: Dict[str, Any]) -> str:
    level = (_str_field(body, "level") or DEFAULT_LEVEL).strip()
    if level not in LEVEL_CODES:
        raise ApiError(400, "BAD_LEVEL",
                       "مستوى الجمهور غير معروف. اختر واحدًا من: " + "، ".join(
                           f"{lvl['label_ar']} ({lvl['code']})" for lvl in LEVELS) + ".")
    return level


def _translation(body: Dict[str, Any]) -> str:
    translation = _str_field(body, "translation").strip()
    if not translation:
        raise _err_translation_required()
    if len(translation) > MAX_TRANSLATION_CHARS:
        raise ApiError(413, "TRANSLATION_TOO_LONG",
                       f"الترجمة أطول من الحد المسموح ({len(translation):,} حرفًا، والحد "
                       f"{MAX_TRANSLATION_CHARS:,}). قسّمها إلى أجزاء وافحص كل جزء على حدة.")
    return translation


def _check_id(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{16}", value or ""):
        raise _err_check_not_found()
    return value


def h_health(req: Request) -> Response:
    return json_response(health_payload())


def h_languages(req: Request) -> Response:
    return json_response(languages_payload())


def h_check(req: Request) -> Response:
    body = _body(req)
    translation = _translation(body)
    arabic = _str_field(body, "arabic").strip()
    if len(arabic) > MAX_ARABIC_CHARS:
        raise ApiError(413, "ARABIC_TOO_LONG",
                       f"النص العربي أطول من الحد المسموح ({MAX_ARABIC_CHARS:,} حرف). "
                       "اختصره إلى الجزء المقابل للترجمة، أو اتركه فارغًا — فهو اختياري.")
    lang, level = _lang(body), _level(body)
    if lang not in target_languages() and not arabic.strip():
        # With no approved translation indexed in this language, only the
        # Arabic can establish which verses are quoted. Without it the answer
        # would be "no quotations" — a CLEAR the tool has not earned.
        raise ApiError(400, "LANG_NOT_INDEXED",
                       "لا توجد ترجمة معتمدة مفهرسة بهذه اللغة، فلا يمكن فحص الاقتباسات "
                       "إلا بالنص العربي المصدر. أضف النص العربي، أو اختر لغة من القائمة: "
                       + "، ".join(sorted(target_languages())) + ".")
    return json_response(on_engine(run_check, translation, arabic, lang, level))


def h_clarity(req: Request) -> Response:
    """Re-run Panel 3 at another audience level without re-checking.

    The frozen spans come from the SAVED check, never from the client: the
    client sends the text again (it is not stored), and its SHA-256 must match
    the one recorded at check time, so the word positions still line up."""
    body = _body(req)
    translation = _translation(body)
    level = _level(body)
    record = STORE.get_check(_check_id(_str_field(body, "check_id")))
    if record is None:
        raise _err_check_not_found()
    if _sha256(translation) != record.get("text_sha256"):
        raise ApiError(409, "TEXT_CHANGED",
                       "نص الترجمة تغيّر منذ الفحص، فمواضع الاقتباسات المجمَّدة لم تعد مضمونة. "
                       "أعد الفحص أولًا ثم غيّر مستوى الجمهور.")
    spans = _word_spans(record.get("findings") or [])
    clarity = on_engine(_clarity, translation, level, spans, record.get("lang") or "en")
    if clarity is None:
        raise ApiError(404, "CLARITY_UNAVAILABLE",
                       "لوحة الوضوح غير متاحة في هذا التثبيت. نتيجة الإسناد صالحة كما هي.")
    return json_response({"check_id": record["check_id"], "clarity": clarity})


def _load_check(check_id: str) -> Dict[str, Any]:
    record = STORE.get_check(_check_id(check_id))
    if record is None:
        raise _err_check_not_found()
    return record


def h_get_check(req: Request) -> Response:
    return json_response(export_payload(_load_check(req.params["id"])))


def h_export(req: Request) -> Response:
    rec = export_payload(_load_check(req.params["id"]))
    return json_response(rec, headers={
        "Content-Disposition": f'attachment; filename="mizan-check-{rec["check_id"]}.json"'})


def h_report(req: Request) -> Response:
    record = _load_check(req.params["id"])
    body = render_report_html(record, STORE.items_for_check(record["check_id"])).encode("utf-8")
    headers = {"Content-Security-Policy":
               "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'"}
    if req.query.get("download") == "1":
        headers["Content-Disposition"] = (
            f'attachment; filename="mizan-report-{record["check_id"]}.html"')
    return Response(200, body, "text/html; charset=utf-8", headers)


def _parse_ref(ref: str) -> Tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,3})\s*:\s*(\d{1,3})\s*", ref or "")
    if not m or not (1 <= int(m.group(1)) <= 114) or int(m.group(2)) < 1:
        raise ApiError(400, "BAD_REF", "مرجع الآية غير صالح. اكتبه بالصيغة سورة:آية، مثل 51:56.")
    return int(m.group(1)), int(m.group(2))


def approved_renderings(surah: int, ayah: int, lang: str) -> List[Dict[str, Any]]:
    """Every approved rendering of one verse in one language, verbatim."""
    out = []
    for bid in ENGINE.editions(lang):
        text = ENGINE.verse(bid, surah, ayah)
        if text:
            out.append({"book_id": bid, "title": ENGINE.books[bid]["title"], "text": text,
                        "source_url": translation_url(bid, surah, ayah)})
    return out


def h_verse(req: Request) -> Response:
    surah, ayah = _parse_ref(req.query.get("ref", ""))
    lang = _lang({"lang": req.query.get("lang", "en")})
    renderings = on_engine(approved_renderings, surah, ayah, lang, timeout=20)
    return json_response({
        "ref": f"{surah}:{ayah}", "lang": lang, "renderings": renderings,
        "verse_url": verse_url(surah, ayah), "index_version": INDEX_VERSION,
        "source": "quranpedia.net",
    })


def _with_links(item: Dict[str, Any]) -> Dict[str, Any]:
    """Add the verified source link and the report link to a review item."""
    if item.get("surah") and item.get("ayah"):
        item["verse_url"] = verse_url(item["surah"], item["ayah"])
    item["report_url"] = f"/report/{item['check_id']}.html"
    return item


def h_enqueue(req: Request) -> Response:
    body = _body(req)
    check_id = _check_id(_str_field(body, "check_id"))
    if "finding_ids" in body:
        ids = body.get("finding_ids")
    else:
        ids = [body.get("finding_idx")]
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids):
        raise ApiError(400, "BAD_FINDING",
                       "حدّد رقم البند المراد إرساله (finding_idx)، أو قائمة أرقام (finding_ids).")
    items = [_with_links(STORE.enqueue(check_id, i)) for i in ids]
    created = any(it.get("created") for it in items)
    return json_response({"items": items}, status=201 if created else 200)


def h_list_review(req: Request) -> Response:
    status = req.query.get("status", "open")
    return json_response({"status": status,
                          "items": [_with_links(it) for it in STORE.list_items(status)],
                          "counts": STORE.counts(), "decisions": DECISIONS})


def h_review_log(req: Request) -> Response:
    try:
        limit = int(req.query.get("limit", "500"))
    except ValueError:
        limit = 500
    return json_response({"events": STORE.audit_log(limit), "append_only": True})


def _item_id(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise ApiError(404, "ITEM_NOT_FOUND", "رقم بند المراجعة غير صالح.") from None


def h_get_item(req: Request) -> Response:
    item = STORE.get_item(_item_id(req.params["item"]))
    if item is None:
        raise ApiError(404, "ITEM_NOT_FOUND",
                       "لا يوجد بند مراجعة بهذا الرقم. حدّث صفحة المراجعة لترى الطابور الحالي.")
    return json_response(_with_links(item))


def h_decide(req: Request) -> Response:
    body = _body(req)
    item_id = _item_id(req.params["item"])
    item = STORE.get_item(item_id)
    if item is None:
        raise ApiError(404, "ITEM_NOT_FOUND",
                       "لا يوجد بند مراجعة بهذا الرقم. حدّث صفحة المراجعة لترى الطابور الحالي.")
    decision = _str_field(body, "decision")
    replacement = None
    if decision == REPLACE:
        bid = body.get("replacement_book_id")
        if not isinstance(bid, int) or bid not in ENGINE.books:
            raise ApiError(400, "REPLACEMENT_REQUIRED",
                           "اختر الترجمة المعتمدة التي يُستبدل بها النص المنشور، ثم أعد الإرسال.")
        if not (item.get("surah") and item.get("ayah")):
            raise ApiError(400, "NO_VERSE",
                           "هذا البند لم يُحلّ إلى آية، فلا نص معتمد يُستبدل به. اختر «إحالة إلى عالم مختص».")
        if ENGINE.books[bid]["language"] != item["lang"]:
            raise ApiError(400, "REPLACEMENT_LANG",
                           "الترجمة المختارة بلغة غير لغة الوثيقة. اختر ترجمة معتمدة باللغة نفسها.")
        text = on_engine(ENGINE.verse, bid, item["surah"], item["ayah"], timeout=20)
        if not text:
            raise ApiError(400, "REPLACEMENT_MISSING",
                           "لا نص لهذه الآية في الترجمة المختارة. اختر ترجمة معتمدة أخرى.")
        replacement = {"book_id": bid, "title": ENGINE.books[bid]["title"], "text": text}
    updated = STORE.record_decision(item_id, decision, _str_field(body, "reviewer"),
                                    _str_field(body, "note"), replacement=replacement)
    return json_response(_with_links(updated), status=201)


ROUTES: List[Tuple[str, "re.Pattern[str]", Callable[[Request], Response]]] = [
    (m, re.compile(p), h) for m, p, h in [
        ("GET", r"/api/health", h_health),
        ("GET", r"/api/languages", h_languages),
        ("POST", r"/api/check", h_check),
        ("POST", r"/api/clarity", h_clarity),
        ("GET", r"/api/check/(?P<id>[^/]+)/export\.json", h_export),
        ("GET", r"/api/check/(?P<id>[^/]+)", h_get_check),
        ("GET", r"/report/(?P<id>[^/]+)\.html", h_report),
        ("GET", r"/api/verse", h_verse),
        ("POST", r"/api/review", h_enqueue),
        ("GET", r"/api/review", h_list_review),
        ("GET", r"/api/review/log", h_review_log),
        ("GET", r"/api/review/(?P<item>[^/]+)", h_get_item),
        ("POST", r"/api/review/(?P<item>[^/]+)/decision", h_decide),
    ]
]

_PAGES = {"/": "index.html", "/index.html": "index.html",
          "/review": "review.html", "/review.html": "review.html"}

_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}

_PAGE_CSP = ("default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; "
             "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
             "font-src 'self' https://fonts.gstatic.com; base-uri 'none'; form-action 'self'; "
             "frame-ancestors 'none'")


def _static(path: str) -> Optional[Response]:
    name = _PAGES.get(path, path.lstrip("/"))
    safe = os.path.normpath("/" + name).lstrip("/")
    full = os.path.join(WEB, safe)
    if not os.path.abspath(full).startswith(os.path.abspath(WEB) + os.sep):
        return None
    if not os.path.isfile(full):
        return None
    ctype = _STATIC_TYPES.get(os.path.splitext(full)[1], "application/octet-stream")
    with open(full, "rb") as fh:
        body = fh.read()
    headers = {"Content-Security-Policy": _PAGE_CSP} if ctype.startswith("text/html") else {}
    return Response(200, body, ctype, headers)


def dispatch(req: Request) -> Response:
    """Route one request. Every failure becomes an Arabic JSON error."""
    try:
        allowed = []
        for method, pattern, handler in ROUTES:
            m = pattern.fullmatch(req.path)
            if not m:
                continue
            if method != req.method:
                allowed.append(method)
                continue
            req.params = m.groupdict()
            return handler(req)
        if allowed:
            raise ApiError(405, "METHOD_NOT_ALLOWED",
                           f"هذا المسار يقبل {' أو '.join(sorted(set(allowed)))} فقط.")
        if req.method == "GET" and not req.path.startswith("/api/"):
            found = _static(req.path)
            if found is not None:
                return found
            return Response(404, "الصفحة غير موجودة. ابدأ من الصفحة الرئيسية /".encode("utf-8"),
                            "text/plain; charset=utf-8")
        raise ApiError(404, "NOT_FOUND",
                       "مسار غير معروف. المسارات المتاحة موثّقة في رأس الملف app.py.")
    except ApiError as exc:
        return json_response({"error": exc.message, "code": exc.code}, status=exc.status)
    except ReviewError as exc:
        return json_response({"error": exc.message, "code": exc.code}, status=exc.status)
    except Exception as exc:  # noqa: BLE001 - never leak a traceback page
        traceback.print_exc()
        return json_response({
            "error": "حدث خطأ داخلي ولم يُحفظ شيء. أعد المحاولة؛ وإن تكرّر فأبلغ المسؤول "
                     "بوقت الخطأ ليراجع سجلّ الخادم.",
            "code": "INTERNAL", "detail": type(exc).__name__}, status=500)


# ===========================================================================
# HTTP layer — stdlib http.server
# ===========================================================================

_LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1", "[::1]"}

# Behind a reverse proxy (Caddy) the app binds to loopback but every request
# arrives with the PUBLIC hostname in its Host header. List those hostnames
# here, comma-separated, e.g. MIZAN_ALLOWED_HOSTS=mizan.example.org . The
# DNS-rebinding guard still refuses every other name.
_ALLOWED_HOSTS = _LOOPBACK_NAMES | {
    h.strip().lower() for h in os.environ.get("MIZAN_ALLOWED_HOSTS", "").split(",") if h.strip()
}


def _host_name(host_header: str) -> str:
    if host_header.startswith("["):
        return host_header.split("]")[0] + "]"
    return host_header.rsplit(":", 1)[0] if ":" in host_header else host_header


def make_handler():
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        server_version = "MIZAN/1.1"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = SOCKET_TIMEOUT_S  # applies to every socket read

        def log_message(self, fmt: str, *args: Any) -> None:
            # Method, path and status only. No client address, no user agent.
            sys.stderr.write("  %s\n" % (fmt % args))

        def log_request(self, code: Any = "-", size: Any = "-") -> None:
            path = self.path.split("?", 1)[0]
            self.log_message('"%s %s" %s', self.command, path, str(code))

        def _write(self, resp: Response) -> None:
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.ctype)
            self.send_header("Content-Length", str(len(resp.body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in resp.headers.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(resp.body)

        def _guard(self) -> Optional[Response]:
            """Refuse requests a local tool should never serve."""
            if HOST in _LOOPBACK_NAMES:
                # DNS-rebinding guard: a page on another site must not be able
                # to read the review log by pointing its hostname at 127.0.0.1.
                if _host_name(self.headers.get("Host", "")).lower() not in _ALLOWED_HOSTS:
                    return json_response({"error": "رُفض الطلب: اسم المضيف غير محلي. افتح مِيزان "
                                                   "من http://127.0.0.1 مباشرة.",
                                          "code": "BAD_HOST"}, status=403)
            if self.command == "POST":
                origin = self.headers.get("Origin")
                if origin and urlsplit(origin).netloc != self.headers.get("Host", ""):
                    return json_response({"error": "رُفض الطلب لأنه صادر من موقع آخر. استخدم "
                                                   "واجهة مِيزان المحلية مباشرة.",
                                          "code": "FOREIGN_ORIGIN"}, status=403)
            return None

        def _request(self, body: Any = None) -> Request:
            parts = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(parts.query).items()}
            method = "GET" if self.command == "HEAD" else self.command
            return Request(method, parts.path or "/", query, body)

        def do_GET(self) -> None:  # noqa: N802
            self._write(self._guard() or dispatch(self._request()))

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_POST(self) -> None:  # noqa: N802
            refused = self._guard()
            if refused:
                return self._write(refused)
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                self.close_connection = True
                return self._write(json_response(
                    {"error": "أرسل الطلب بترويسة Content-Length؛ الإرسال المجزّأ غير مدعوم.",
                     "code": "LENGTH_REQUIRED"}, status=411))
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self.close_connection = True
                return self._write(json_response(
                    {"error": "أرسل الطلب بترويسة Content-Type: application/json.",
                     "code": "UNSUPPORTED_MEDIA_TYPE"}, status=415))
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                self.close_connection = True
                return self._write(json_response(
                    {"error": "ترويسة Content-Length غير صالحة. أعد إرسال الطلب.",
                     "code": "BAD_LENGTH"}, status=400))
            if length > MAX_BODY_BYTES:
                # Do not read it. Close the connection so the unread body
                # cannot be mistaken for the next request.
                self.close_connection = True
                return self._write(json_response({
                    "error": f"الطلب أكبر من الحد المسموح ({MAX_BODY_BYTES // 1024} ك.ب). "
                             "قسّم الوثيقة إلى أجزاء وافحص كل جزء على حدة.",
                    "code": "BODY_TOO_LARGE"}, status=413))
            try:
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (TimeoutError, OSError):
                self.close_connection = True
                return self._write(json_response({
                    "error": "انقطع إرسال الطلب قبل اكتماله. أعد المحاولة.",
                    "code": "REQUEST_TIMEOUT"}, status=408))
            except (ValueError, UnicodeDecodeError, RecursionError):
                # RecursionError: a body of deeply nested brackets.
                return self._write(json_response({
                    "error": "تعذّرت قراءة الطلب: الجسم ليس JSON صالحًا بترميز UTF-8. "
                             "أرسل كائن JSON مثل {\"translation\": \"...\", \"lang\": \"en\"}.",
                    "code": "BAD_JSON"}, status=400))
            self._write(dispatch(self._request(body)))

    return Handler


def serve(host: str = HOST, port: int = PORT) -> None:
    from http.server import ThreadingHTTPServer

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 32

    httpd = Server((host, port), make_handler())
    _WORKER.submit(_warm_up)
    _banner(host, httpd.server_address[1])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def _banner(host: str, port: int) -> None:
    h = health_payload()
    feats = h["features"]
    print("مِيزان — attribution gate (local)")
    print(f"  index        {h['index_version']} · {h['n_translations']} approved "
          f"translations · {h['n_languages']} languages · source quranpedia.net")
    print(f"  corpus       {h['n_texts']:,} texts · {h['n_verses']:,} verses")
    print(f"  pipeline     {h['pipeline']}")
    print(f"  panels       attribution · terms={'on' if feats['terms'] else 'absent'}"
          f" · clarity={'on' if feats['clarity'] else 'absent'}")
    print(f"  review db    {STORE.path}  (retention {RETENTION_DAYS or '∞'} days)")
    print(f"  listening    http://{host}:{port}")
    print("  لا يُفتي · لا يترجم · يُسنِد فقط", flush=True)


if __name__ == "__main__":
    serve()
