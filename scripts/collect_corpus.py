#!/usr/bin/env python3
"""
MIZAN — real-world corpus collector.

Why this exists
---------------
Every measurement so far has fed the index's own text back to itself. That
proves the comparison mechanism works; it says nothing about whether the thing
survives real human writing — curly quotes, ellipses mid-verse, footnote
markers glued to the last word, "God" where the index says "Allah", partial
verses, inline references in a dozen formats.

This script goes and gets that writing, from publishers who put readable prose
in the page. It caches every byte under data/real/raw/ so the measurement
re-runs offline and reproduces exactly.

Crawl ethics (not optional)
---------------------------
* /robots.txt is fetched and parsed for every host BEFORE any other request.
  A disallowed path is never fetched. The decision is recorded in the manifest.
* Crawl-delay is honoured. With none stated we use DEFAULT_DELAY (>= 2s).
* One host at a time. No threads, no parallelism, ever.
* MAX_PAGES_PER_HOST caps the footprint.
* The User-Agent names the project and a contact address.

Stdlib only, Python 3.11.

Usage
-----
    python3 scripts/collect_corpus.py              # crawl (uses cache)
    python3 scripts/collect_corpus.py --offline    # rebuild corpus from cache
    python3 scripts/collect_corpus.py --max 40     # cap pages per host
"""
from __future__ import annotations

import argparse
import hashlib
import html as html_mod
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.robotparser import RobotFileParser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REAL = os.path.join(ROOT, "data", "real")
RAW = os.path.join(REAL, "raw")
CORPUS = os.path.join(REAL, "corpus.jsonl")
NEGATIVES = os.path.join(REAL, "negatives.jsonl")
MANIFEST = os.path.join(REAL, "manifest.json")

USER_AGENT = ("Mizan-Research/0.1 (Islamic AI Challenge 2026; "
              "contact osamaabdullh2002@gmail.com)")
DEFAULT_DELAY = 2.5          # seconds between requests to the same host
MAX_PAGES_PER_HOST = 40
TIMEOUT = 30

# ---------------------------------------------------------------------------
# Sources.
#
# islamreligion.com publishes the same editorial material in fifteen
# languages under /<lang>/ paths, numbering articles with ONE id space: the
# English /articles/1190/ and the Hindi /hi/articles/1190/ are the same piece.
# That is worth a lot here. It means a per-language score compares the same
# house style and the same citation convention across languages, instead of
# conflating "this language is hard" with "this publisher is different".
#
# Verified live 2026-10-02: the language switcher offers
# ar cn de en es fr he hi it jp ko pt ru sw tl.
# There is NO Urdu and NO Bengali edition on this host, so those two languages
# have to come from somewhere else (see EXTRA_SOURCES) or not at all. We
# report the real number rather than padding it.
#
# Category listings only exist on the English site; the localised category
# pages return near-empty lists. So we enumerate ids from English listings and
# request the same ids under each language prefix. Pages that do not exist in
# a language answer with a short stub, which the extractor drops naturally.
#
# robots.txt is re-checked at run time for every host regardless of this
# table. This list is a starting point, not an authorisation.
# ---------------------------------------------------------------------------
IR = "https://www.islamreligion.com"

# English category listings -> the shared article id space.
IR_CATEGORIES = [
    f"{IR}/category/48/beliefs-of-islam/",
    f"{IR}/category/59/hereafter/",
    f"{IR}/category/75/holy-quran/",
    f"{IR}/category/55/worship-and-practice/",
    f"{IR}/category/33/evidence-islam-is-truth/",
    f"{IR}/category/79/prophet-muhammad/",
]

# language code -> URL prefix on islamreligion.com ("" means the English root)
IR_LANGS = {"en": "", "hi": "hi", "tl": "tl"}

# islamqa.info — the only verified source for Urdu and Bengali prose that is
# served as readable HTML rather than a PDF stub. Verified live 2026-10-02:
# robots.txt disallows only /_next/, article paths are permitted, and the
# answer body is server-rendered inside data-sut="answer-text".
#
# Its citation style is harder than islamreligion's and that is precisely why
# it is worth having: the reference is a surah NAME plus, in Bengali, digits
# in Bengali script — "(সূরা নিসা: ১১৬)" rather than "(4:116)". If MIZAN is
# only ever tested against publishers who print "2:255", the measurement is
# flattering. See SURAH_NAMES / _ASCII_DIGITS below.
#
# Driven from the per-language sitemaps: a 200 on islamqa does NOT mean the
# translation exists. Untranslated answers render full chrome plus a "this
# page is available in: English" notice, so sequential ids would quietly fill
# the corpus with soft-404s.
# islamqa is the only verified Urdu/Bengali source, and its per-page yield is
# low, so it gets a larger page budget than the da'wah article hosts.
ISLAMQA_BUDGET_MULTIPLIER = 4

ISLAMQA_SITEMAPS = {
    "ur": "https://islamqa.info/sitemaps/ur/answers/1/sitemap.xml",
    "bn": "https://islamqa.info/sitemaps/bn/answers/1/sitemap.xml",
    "hi": "https://islamqa.info/sitemaps/hi/answers/1/sitemap.xml",
}

# Additional seeds on other hosts. Kept separate so the id-sharing trick
# above stays readable.
EXTRA_SOURCES: list[dict] = []

# Non-ASCII digit shapes that appear inside printed references.
_ASCII_DIGITS = {}
for _base, _zero in (("٠", 0), ("۰", 0), ("০", 0),
                     ("०", 0)):
    for _i in range(10):
        _ASCII_DIGITS[chr(ord(_base) + _i)] = str(_i)


def ascii_digits(s: str) -> str:
    """Arabic-Indic, Persian, Bengali and Devanagari digits -> ASCII."""
    return "".join(_ASCII_DIGITS.get(c, c) for c in s)


# Surah name -> number, in the spellings these publishers actually print.
# Deliberately partial: a name we do not know yields printed_ref = None, and
# that item simply does not count toward the reference-accuracy denominator.
# Guessing a number from a name we are unsure of would corrupt the ground
# truth, which is the one thing in this corpus that has to be trustworthy.
SURAH_NAMES: dict[str, int] = {}


def _register(num: int, *names: str) -> None:
    for n in names:
        SURAH_NAMES[_norm_name(n)] = num


def _norm_name(n: str) -> str:
    n = n.lower().strip()
    n = re.sub(r"^(?:surah?|surat|soorah?|s[uū]rah?|"
               r"سورة|سورۃ|"
               r"سورت|"
               r"সূরা|"
               r"सूरा|सूरतुल)"
               r"[\s\-:]*", "", n)
    n = re.sub(r"^(?:al|ar|as|ash|at|an|az|ad)[\s\-'’]+", "", n)
    n = re.sub(r"^(?:ال)", "", n)                  # Arabic "al-"
    n = re.sub(r"[\s\-'’ً-ْ\.]", "", n)
    return n


# The surahs these publishers quote most. Arabic / Urdu / Bengali / Hindi.
_register(1, "الفاتحة", "الفاتحه", "فاتحہ", "ফাতিহা", "फ़ातिहा", "Fatihah")
_register(2, "البقرة", "البقرۃ", "بقرہ", "বাকারা", "বাক্বারা", "बक़रा", "Baqarah")
_register(3, "آل عمران", "ال عمران", "আলে ইমরান", "Imran")
_register(4, "النساء", "النسا", "নিসা", "निसा", "Nisa")
_register(5, "المائدة", "المائدہ", "মায়েদা", "माइदा", "Maidah")
_register(6, "الأنعام", "الانعام", "আনআম", "Anam")
_register(7, "الأعراف", "الاعراف", "আরাফ", "Araf")
_register(8, "الأنفال", "আনফাল")
_register(9, "التوبة", "التوبہ", "তাওবা", "Tawbah")
_register(10, "يونس", "ইউনুস")
_register(11, "هود", "হুদ")
_register(12, "يوسف", "ইউসুফ", "Yusuf")
_register(13, "الرعد", "রাদ")
_register(14, "إبراهيم", "ابراهيم", "ইবরাহীম")
_register(15, "الحجر", "হিজর")
_register(16, "النحل", "নাহল", "Nahl")
_register(17, "الإسراء", "الاسراء", "ইসরা", "Isra")
_register(18, "الكهف", "কাহাফ", "Kahf")
_register(19, "مريم", "মারইয়াম")
_register(20, "طه", "ত্বহা")
_register(21, "الأنبياء", "الانبياء", "আম্বিয়া")
_register(22, "الحج", "হজ্জ")
_register(23, "المؤمنون", "মুমিনুন")
_register(24, "النور", "নূর", "Nur")
_register(25, "الفرقان", "ফুরকান")
_register(26, "الشعراء", "শুআরা")
_register(27, "النمل", "নামল")
_register(28, "القصص", "কাসাস")
_register(29, "العنكبوت", "আনকাবুত")
_register(30, "الروم", "রূম")
_register(31, "لقمان", "লুকমান")
_register(32, "السجدة", "সাজদা")
_register(33, "الأحزاب", "الاحزاب", "আহযাব", "अहज़ाब", "Ahzab")
_register(34, "سبأ", "সাবা")
_register(35, "فاطر", "ফাতির")
_register(36, "يس", "ইয়াসীন")
_register(37, "الصافات", "সাফফাত")
_register(38, "ص", "সোয়াদ")
_register(39, "الزمر", "যুমার")
_register(40, "غافر", "গাফির")
_register(41, "فصلت", "ফুসসিলাত")
_register(42, "الشورى", "শূরা")
_register(43, "الزخرف", "যুখরুফ")
_register(44, "الدخان", "দুখান")
_register(45, "الجاثية", "জাসিয়া")
_register(46, "الأحقاف", "আহকাফ")
_register(47, "محمد", "মুহাম্মাদ")
_register(48, "الفتح", "ফাতহ")
_register(49, "الحجرات", "হুজুরাত")
_register(50, "ق", "কাফ")
_register(51, "الذاريات", "যারিয়াত")
_register(53, "النجم", "নাজম")
_register(55, "الرحمن", "রহমান")
_register(56, "الواقعة", "ওয়াকিয়া")
_register(57, "الحديد", "হাদীদ")
_register(58, "المجادلة", "মুজাদালা")
_register(59, "الحشر", "হাশর")
_register(62, "الجمعة", "জুমুআ")
_register(63, "المنافقون", "মুনাফিকুন")
_register(64, "التغابن", "তাগাবুন")
_register(65, "الطلاق", "তালাক")
_register(66, "التحريم", "তাহরীম")
_register(67, "الملك", "মুলক")
_register(73, "المزمل", "মুযযাম্মিল")
_register(74, "المدثر", "মুদ্দাসসির")
_register(76, "الإنسان", "الانسان", "ইনসান")
_register(80, "عبس", "আবাসা")
_register(85, "البروج", "বুরুজ")
_register(86, "الطارق", "তারিক")
_register(87, "الأعلى", "الاعلى", "আলা")
_register(89, "الفجر", "ফজর")
_register(90, "البلد", "বালাদ")
_register(91, "الشمس", "শামস")
_register(92, "الليل", "লাইল")
_register(93, "الضحى", "দুহা")
_register(94, "الشرح", "ইনশিরাহ")
_register(95, "التين", "তীন")
_register(96, "العلق", "আলাক")
_register(97, "القدر", "কদর")
_register(98, "البينة", "বাইয়িনা")
_register(99, "الزلزلة", "যিলযাল")
_register(102, "التكاثر", "তাকাসুর")
_register(103, "العصر", "আসর")
_register(107, "الماعون", "মাউন")
_register(108, "الكوثر", "কাওসার")
_register(109, "الكافرون", "কাফিরুন")
_register(110, "النصر", "নাসর")
_register(112, "الإخلاص", "الاخلاص", "ইখলাস", "Ikhlas")
_register(113, "الفلق", "ফালাক")
_register(114, "الناس", "নাস")

# "(সূরা নিসা: ১১৬)" / "[النساء: 122]" / "(सूरतुल-अहज़ाब : 32)"
_NAMED_REF = re.compile(
    r"[\(\[（]\s*([^()\[\]:：\d]{2,40}?)\s*[:：]\s*"
    r"([\d٠-٩۰-۹০-৯०-९]{1,3})"
    r"(?:\s*[-–]\s*[\d٠-٩۰-۹০-৯०-९]{1,3})?"
    r"\s*[\)\]）]")

LICENSE_NOTE = (
    "Quoted for non-commercial research measurement under fair-use / fair-"
    "dealing. Each record stores only the quoted span and a short surrounding "
    "context, with the source URL retained for attribution. Not redistributed "
    "as a reading edition."
)


# ---------------------------------------------------------------------------
# Polite HTTP
# ---------------------------------------------------------------------------
class Fetcher:
    """One-host-at-a-time fetcher that obeys robots.txt and caches to disk."""

    def __init__(self, offline: bool = False, max_pages: int = MAX_PAGES_PER_HOST):
        self.offline = offline
        self.max_pages = max_pages
        self._robots: dict[str, RobotFileParser | None] = {}
        self._delay: dict[str, float] = {}
        self._last_hit: dict[str, float] = {}
        self._count: dict[str, int] = {}
        self.robots_log: dict[str, dict] = {}

    # -- cache ------------------------------------------------------------
    @staticmethod
    def cache_path(url: str) -> str:
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
        host = urllib.parse.urlsplit(url).netloc.replace(":", "_")
        d = os.path.join(RAW, host)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{h}.html")

    # -- robots -----------------------------------------------------------
    def robots_ok(self, url: str) -> bool:
        """Fetch (once) and consult robots.txt. Unreachable robots => refuse."""
        host = urllib.parse.urlsplit(url).netloc
        if host not in self._robots:
            self._load_robots(url, host)
        rp = self._robots[host]
        if rp is None:
            return False
        return rp.can_fetch(USER_AGENT, url)

    def _load_robots(self, url: str, host: str) -> None:
        parts = urllib.parse.urlsplit(url)
        robots_url = f"{parts.scheme}://{host}/robots.txt"
        cache = self.cache_path(robots_url)
        body = None
        from_cache = False
        if os.path.exists(cache):
            body = open(cache, encoding="utf-8", errors="replace").read()
            from_cache = True
        elif not self.offline:
            try:
                req = urllib.request.Request(
                    robots_url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    body = r.read().decode("utf-8", "replace")
                with open(cache, "w", encoding="utf-8") as fh:
                    fh.write(body)
            except Exception as exc:                     # noqa: BLE001
                self.robots_log[host] = {
                    "robots_url": robots_url, "status": "unreachable",
                    "detail": str(exc), "decision": "SKIP HOST",
                }
                self._robots[host] = None
                return

        if body is None:
            self.robots_log[host] = {"robots_url": robots_url,
                                     "status": "not cached (offline)",
                                     "decision": "SKIP HOST"}
            self._robots[host] = None
            return

        rp = RobotFileParser()
        rp.parse(body.splitlines())
        self._robots[host] = rp

        delay = None
        try:
            delay = rp.crawl_delay(USER_AGENT)
        except Exception:                                # noqa: BLE001
            delay = None
        if delay is None:
            m = re.search(r"(?im)^\s*crawl-delay\s*:\s*([\d.]+)", body)
            if m:
                delay = float(m.group(1))
        self._delay[host] = max(float(delay or 0), DEFAULT_DELAY)

        self.robots_log[host] = {
            "robots_url": robots_url,
            # Recorded either way. The audit trail has to show that robots.txt
            # was consulted for every host on every run, not only on the run
            # that happened to download it.
            "status": "from cache" if from_cache else "fetched",
            "crawl_delay_stated": delay,
            "delay_used_seconds": self._delay[host],
            "disallow_lines": [ln.strip() for ln in body.splitlines()
                               if ln.strip().lower().startswith("disallow")][:12],
            "decision": "CRAWL (per-URL check still applied)",
        }

    # -- get --------------------------------------------------------------
    def get(self, url: str) -> str | None:
        # robots.txt is consulted for EVERY url, including ones answered from
        # cache. A cached page is still a page we are using, and the audit
        # trail should record the permission under which we hold it — not
        # just the permission for the one run that downloaded it.
        allowed = self.robots_ok(url)

        cache = self.cache_path(url)
        if os.path.exists(cache):
            if not allowed:
                print(f"    robots.txt now disallows, cached copy not used: "
                      f"{url}", file=sys.stderr)
                return None
            return open(cache, encoding="utf-8", errors="replace").read()
        if self.offline:
            return None

        if not allowed:
            print(f"    robots.txt disallows -> skipped: {url}", file=sys.stderr)
            return None

        host = urllib.parse.urlsplit(url).netloc
        delay = self._delay.get(host, DEFAULT_DELAY)
        since = time.time() - self._last_hit.get(host, 0.0)
        if since < delay:
            time.sleep(delay - since)

        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en,ur,bn,hi,tl;q=0.8",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
                enc = r.headers.get_content_charset() or "utf-8"
                body = raw.decode(enc, "replace")
        except urllib.error.HTTPError as exc:
            self._last_hit[host] = time.time()
            self._count[host] = self._count.get(host, 0) + 1
            print(f"    HTTP {exc.code}: {url}", file=sys.stderr)
            return None
        except Exception as exc:                          # noqa: BLE001
            self._last_hit[host] = time.time()
            print(f"    error {exc}: {url}", file=sys.stderr)
            return None

        self._last_hit[host] = time.time()
        self._count[host] = self._count.get(host, 0) + 1
        with open(cache, "w", encoding="utf-8") as fh:
            fh.write(body)
        return body


# ---------------------------------------------------------------------------
# HTML -> text
# ---------------------------------------------------------------------------
_SCRIPTS = re.compile(r"(?is)<(script|style|noscript)\b.*?</\1>")
_TAG = re.compile(r"(?s)<[^>]+>")


def strip_tags(frag: str) -> str:
    frag = _SCRIPTS.sub(" ", frag)
    frag = re.sub(r"(?i)<br\s*/?>", " ", frag)
    txt = _TAG.sub("", frag)
    txt = html_mod.unescape(txt)
    txt = txt.replace("\xa0", " ").replace("‏", "").replace("‎", "")
    return " ".join(txt.split())


# Reference formats seen in the wild. Deliberately broad: the point of this
# corpus is that publishers do not agree on one.
_REF_PATTERNS = [
    # (Quran 2:255) / (Qur'an 2:255) / (Quran, 2:255) / [Quran 2:255]
    re.compile(r"[\(\[（][^()\[\]]{0,40}?\b(\d{1,3})\s*[:：]\s*(\d{1,3})[^()\[\]]{0,20}[\)\]）]"),
    # Quran 2:255 bare
    re.compile(r"(?i)\bqur'?’?an\b[^\d]{0,12}(\d{1,3})\s*[:：]\s*(\d{1,3})"),
    # Urdu/Hindi/Bengali often print just (2:255) — covered by the first.
    # Surah N, verse M
    re.compile(r"(?i)\bs[uū]rah?\b[^\d]{0,20}(\d{1,3})[^\d]{0,20}\b(?:verse|ayah|ayat)\b[^\d]{0,6}(\d{1,3})"),
]


def extract_printed_ref(s: str) -> tuple[int, int] | None:
    """
    Pull a surah:ayah that the PUBLISHER printed. Only the publisher's own.

    This is the ground truth for the whole measurement, so it is deliberately
    conservative: an unrecognised surah name returns None and the item drops
    out of the reference-accuracy denominator rather than entering it with a
    guess. A corrupted ground truth is worse than a smaller one.
    """
    s = ascii_digits(s)

    # numeric forms: (Quran 2:255), (क़ुरआन 2:30), bare 2:255 after "Quran"
    for pat in _REF_PATTERNS:
        for m in pat.finditer(s):
            try:
                su, ay = int(m.group(1)), int(m.group(2))
            except (TypeError, ValueError):
                continue
            if 1 <= su <= 114 and 1 <= ay <= 286:
                return su, ay

    # named forms: (সূরা নিসা: ১১৬), [النساء: 122], (सूरतुल-अहज़ाब : 32)
    #
    # Strictly table-driven. An earlier, looser version read "[یعنی :محسود]"
    # ("[that is: the envied one]") as surah 113 purely because a bracketed
    # phrase happened to precede a colon and a number. A single bad ground
    # truth entry silently corrupts the accuracy figure it feeds, so an
    # unrecognised name now yields nothing at all.
    for m in _NAMED_REF.finditer(s):
        num = SURAH_NAMES.get(_norm_name(m.group(1)))
        if num is None:
            continue
        try:
            ay = int(m.group(2))
        except ValueError:
            continue
        if 1 <= ay <= 286:
            return num, ay
    return None


_D = r"\d٠-٩۰-۹০-৯०-९"
_TRAIL_REF = re.compile(
    rf"\s*[\(\[（][^()\[\]]{{0,60}}[{_D}]{{1,3}}\s*[:：]\s*"
    rf"[{_D}]{{1,3}}[^()\[\]]{{0,25}}[\)\]）]\s*[.۔।]?\s*$")


def strip_trailing_ref(s: str) -> str:
    """
    Drop a trailing printed citation so the quoted span stays the quote.

    Left in place, "(Quran 2:255)" is four tokens of noise inside every single
    comparison against the approved text, which drags the similarity score
    down uniformly. Removing it is not cleaning the data in our favour: the
    citation is not part of what the publisher is claiming to quote.
    """
    return _TRAIL_REF.sub("", s).strip()


# Quote glyphs these publishers open and close scripture with. Includes the
# Urdu typographic pair ’’ ‘‘ (which islamqa uses doubled and
# REVERSED relative to English) and the ornate Quranic brackets ﴾ ﴿.
_OPENERS = "“”\"'«»‹›﴾﴿༺‘’„‚"


def strip_quote_marks(s: str) -> str:
    """Trim the surrounding quote glyphs, keeping any leading/trailing ellipsis."""
    return s.strip("".join(_OPENERS) + " \t").strip()


# Quote-ish wrappers, in priority order. The first is islamreligion.com's own
# semantic class, which is a gift: the publisher has already told us which
# paragraphs are scripture.
_QUOTE_BLOCKS = [
    ("w-quran", re.compile(r'(?is)<p[^>]*class="[^"]*\bw-quran\b[^"]*"[^>]*>(.*?)</p>')),
    ("blockquote", re.compile(r"(?is)<blockquote[^>]*>(.*?)</blockquote>")),
]

_BODY_PARA = re.compile(
    r'(?is)<p[^>]*class="[^"]*\bw-body-text(?:-\d)?\b[^"]*"[^>]*>(.*?)</p>')
_ANY_PARA = re.compile(r"(?is)<p[^>]*>(.*?)</p>")

def looks_like_quote(txt: str) -> bool:
    if len(txt) < 25:
        return False
    if txt[0] in _OPENERS or txt.lstrip("…. ")[:1] in _OPENERS:
        return True
    return "﴾" in txt or "﴿" in txt or "۝" in txt


def page_title(h: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", h)
    return strip_tags(m.group(1))[:200] if m else ""


def extract_paragraphs(h: str) -> list[tuple[str, str]]:
    """
    Return the page's paragraphs in document order as (kind, text).

    kind is "quote" when the markup itself marks the paragraph as scripture
    (a semantic class or a blockquote), otherwise "prose". Order matters:
    context_before / context_after come from the neighbours.
    """
    # Narrow to the article body when the template gives us a handle, so we do
    # not scrape navigation, related-article teasers or the footer.
    m = re.search(r'(?is)<div[^>]*(?:id|class)="[^"]*\b(?:article-text|'
                  r'article-content|entry-content|post-content|w-article)\b[^"]*"[^>]*>(.*)',
                  h)
    body = m.group(1) if m else h

    marked: dict[int, str] = {}
    for kind, pat in _QUOTE_BLOCKS:
        for mm in pat.finditer(body):
            marked[mm.start()] = kind

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for mm in _ANY_PARA.finditer(body):
        txt = strip_tags(mm.group(1))
        if len(txt) < 20:
            continue
        key = txt[:120]
        if key in seen:
            continue
        seen.add(key)
        cls = re.search(r'class="([^"]*)"', mm.group(0))
        clsv = cls.group(1) if cls else ""
        kind = "quote" if "w-quran" in clsv else "prose"
        out.append((kind, txt))

    # blockquote content, for templates without a scripture class
    for mm in _QUOTE_BLOCKS[1][1].finditer(body):
        txt = strip_tags(mm.group(1))
        if len(txt) >= 25:
            for i, (k, t) in enumerate(out):
                if t[:80] == txt[:80]:
                    out[i] = ("quote", t)
                    break
            else:
                out.append(("quote", txt))
    return out


# --- islamqa.info --------------------------------------------------------
# A different document shape, and a harder one. The quotation is not its own
# paragraph: it sits INLINE inside the author's sentence, as
#
#     <p>Allah says: <span class="aaya">ARABIC</span><br>
#        ”vernacular rendering“ [النساء: 122]</p>
#
# so the quoted span has to be cut out of the middle of running prose using
# the quote glyphs, with the author's lead-in kept as context_before. This is
# much closer to how most da'wah writing actually looks than a publisher who
# gives scripture its own CSS class, which is why the host is worth the extra
# parser.
_IQ_BODY = re.compile(r'(?is)<div[^>]*data-sut="answer-text"[^>]*>(.*)')

# Urdu shares the Arabic block but adds ی گ ٹ ڈ ڑ ں ہ ۃ ے.
_URDU_ONLY = set("یگٹڈڑںہۃےۂژپچ")


def _is_arabic_script(s: str) -> bool:
    """
    True for text in the Arabic script that is NOT Urdu.

    Used to tell an original verse apart from an Urdu rendering of it, since
    both sit in the same CSS class on islamqa. Urdu uses a handful of letters
    Arabic does not; if none of them appear in a long Arabic-script string,
    it is Arabic.
    """
    arab = sum(1 for c in s if "؀" <= c <= "ۿ")
    if arab < 0.5 * max(len(s.replace(" ", "")), 1):
        return False
    return not (_URDU_ONLY & set(s))
_IQ_AAYA = re.compile(r'(?is)<span[^>]*class="[^"]*\baaya\b[^"]*"[^>]*>(.*?)</span>')

# "“ ”" (en/bn/hi) and the reversed Urdu pair "’’ ‘‘".
_IQ_SPANS = [
    re.compile(r"“(.{20,1200}?)”", re.S),
    re.compile(r"’’(.{20,1200}?)‘‘", re.S),
    re.compile(r"”(.{20,1200}?)“", re.S),
]


def extract_islamqa(h: str) -> list[tuple[str, str, str, str]]:
    """
    Return (kind, quoted_text, context_before, context_after) for one answer.

    Only spans whose paragraph also carries a scripture citation are emitted
    as quotes. islamqa quotes scholars and hadith in the same typography as
    verses; without that gate the corpus would fill with material that has no
    approved Quran translation to be attributed to, and the detection rate
    would be measuring the wrong thing.
    """
    m = _IQ_BODY.search(h)
    if not m:
        return []
    body = m.group(1)
    out: list[tuple[str, str, str, str]] = []
    raw_paras = _ANY_PARA.findall(body)

    # The `aaya` span does NOT hold the same thing on every language edition.
    # On the Urdu site it holds the URDU RENDERING — which is exactly what we
    # want to attribute. On the Hindi site it holds the ARABIC, with the Hindi
    # rendering following outside the span. Telling them apart by script is
    # reliable and needs no per-host configuration: if the span is Arabic and
    # the page is not an Arabic edition, it is the original, not the quotation.
    for i, rp in enumerate(raw_paras):
        for span_html in _IQ_AAYA.findall(rp):
            span = strip_tags(span_html)
            if len(span) < 25 or _is_arabic_script(span):
                continue
            ctx = strip_tags(_IQ_AAYA.sub(" ", rp))
            before = strip_tags(_IQ_AAYA.sub(" ", raw_paras[i - 1])) if i else ""
            after = (strip_tags(_IQ_AAYA.sub(" ", raw_paras[i + 1]))
                     if i + 1 < len(raw_paras) else "")
            out.append(("aaya-span", span,
                        (before or ctx)[-400:], (ctx or after)[:400]))

    paras = [strip_tags(_IQ_AAYA.sub(" ", p)) for p in raw_paras]
    paras = [p for p in paras if len(p) >= 20]

    for i, p in enumerate(paras):
        if extract_printed_ref(p) is None:
            continue                      # no citation -> not provably Quran
        before = paras[i - 1] if i else ""
        after = paras[i + 1] if i + 1 < len(paras) else ""
        for pat in _IQ_SPANS:
            got = False
            for mm in pat.finditer(p):
                span = mm.group(1).strip()
                if len(span) < 20:
                    continue
                lead = (p[:mm.start()].strip() or before)[-400:]
                tail = (p[mm.end():].strip() or after)[:400]
                out.append(("inline-quote", span, lead, tail))
                got = True
            if got:
                break
    return out


# ---------------------------------------------------------------------------
# Link discovery
# ---------------------------------------------------------------------------
_ART_HREF = re.compile(r'href="([^"]*?/(?:[a-z]{2,8}/)?articles/\d+/[^"#?]*)"')


def discover_articles(h: str, base: str, lang_prefix: str | None) -> list[str]:
    urls: list[str] = []
    for m in _ART_HREF.finditer(h):
        u = urllib.parse.urljoin(base, m.group(1))
        u = u.split("#")[0].rstrip("/")
        if u.endswith("/viewall"):
            continue
        path = urllib.parse.urlsplit(u).path
        if lang_prefix:
            if not path.startswith(f"/{lang_prefix}/"):
                continue
        else:
            # English lives at /articles/..., with no language segment.
            if not path.startswith("/articles/"):
                continue
        # keep everything on the seed's own host
        if urllib.parse.urlsplit(u).netloc != urllib.parse.urlsplit(base).netloc:
            u = urllib.parse.urlunsplit(
                urllib.parse.urlsplit(base)[:2] + urllib.parse.urlsplit(u)[2:])
        if u not in urls:
            urls.append(u)
    return urls


# ---------------------------------------------------------------------------
# Corpus construction
# ---------------------------------------------------------------------------
def build_records(url: str, lang: str, h: str, retrieved_at: str
                  ) -> tuple[list[dict], list[dict]]:
    """Return (quotation records, negative-prose records) for one page."""
    title = page_title(h)
    host = urllib.parse.urlsplit(url).netloc

    quotes: list[dict] = []
    negatives: list[dict] = []

    if "islamqa" in host:
        for j, (kind, span, before, after) in enumerate(extract_islamqa(h)):
            published = strip_quote_marks(strip_trailing_ref(span))
            if len(published) < 20:
                continue
            # The citation usually trails the span rather than sitting inside
            # it, so look in the span first and fall back to what follows.
            ref = extract_printed_ref(span) or extract_printed_ref(after[:120])
            rid = hashlib.sha1(
                f"{url}|{j}|{published[:200]}".encode()).hexdigest()[:16]
            quotes.append({
                "id": rid,
                "source_url": url,
                "source_host": host,
                "source_title": title,
                "lang": lang,
                "published_text": published,
                "printed_ref": f"{ref[0]}:{ref[1]}" if ref else None,
                "printed_surah": ref[0] if ref else None,
                "printed_ayah": ref[1] if ref else None,
                "context_before": before[-400:],
                "context_after": after[:400],
                "retrieved_at": retrieved_at,
                "license_note": LICENSE_NOTE,
                "markup_hint": kind,
                "scripture_marked": True,
            })
        return quotes, negatives

    paras = extract_paragraphs(h)

    for i, (kind, txt) in enumerate(paras):
        before = paras[i - 1][1] if i > 0 else ""
        after = paras[i + 1][1] if i + 1 < len(paras) else ""

        is_quote = kind == "quote" or looks_like_quote(txt)
        if is_quote:
            ref = extract_printed_ref(txt)
            published = strip_trailing_ref(txt)
            if len(published) < 20:
                continue
            rid = hashlib.sha1(
                f"{url}|{i}|{published[:200]}".encode()).hexdigest()[:16]
            quotes.append({
                "id": rid,
                "source_url": url,
                "source_host": host,
                "source_title": title,
                "lang": lang,
                "published_text": published,
                "printed_ref": f"{ref[0]}:{ref[1]}" if ref else None,
                "printed_surah": ref[0] if ref else None,
                "printed_ayah": ref[1] if ref else None,
                "context_before": before[-400:],
                "context_after": after[:400],
                "retrieved_at": retrieved_at,
                "license_note": LICENSE_NOTE,
                "markup_hint": kind,
                # Publisher's own markup says this paragraph is scripture.
                # Quoted paragraphs WITHOUT that class are usually the author
                # quoting a scientist, a hadith or another book — real text,
                # but not a Quranic quotation, so they belong in a different
                # denominator. Scoring them as missed detections would be
                # wrong; ignoring them would waste a good negative control.
                "scripture_marked": kind == "quote",
            })
        else:
            # Negative control: author's own prose with no quotation in it.
            # Anything carrying a verse reference or quote marks is excluded —
            # a false-positive probe is worthless if the probe set is dirty.
            if (len(txt) < 180 or extract_printed_ref(txt)
                    or any(c in txt for c in _OPENERS)
                    or re.search(r"\d{1,3}\s*:\s*\d{1,3}", txt)):
                continue
            nid = hashlib.sha1(f"neg|{url}|{i}".encode()).hexdigest()[:16]
            negatives.append({
                "id": nid,
                "source_url": url,
                "source_host": host,
                "lang": lang,
                "text": txt,
                "retrieved_at": retrieved_at,
                "license_note": LICENSE_NOTE,
            })
    return quotes, negatives


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", action="store_true",
                    help="rebuild corpus from data/real/raw cache, no network")
    ap.add_argument("--max", type=int, default=MAX_PAGES_PER_HOST,
                    help="max article pages fetched per LANGUAGE. One host "
                         "serves three languages here, so the per-host total "
                         "is this times the number of languages, plus the "
                         "handful of category listings.")
    args = ap.parse_args()

    os.makedirs(RAW, exist_ok=True)
    fetcher = Fetcher(offline=args.offline, max_pages=args.max)
    retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    all_quotes: list[dict] = []
    all_negs: list[dict] = []
    seen_ids: set[str] = set()
    pages_done: set[str] = set()
    per_source: list[dict] = []

    # --- step 1: harvest the shared article id space from English listings --
    article_ids: list[str] = []
    for cat in IR_CATEGORIES:
        listing = fetcher.get(cat)
        if listing is None:
            print(f"[ids] listing unavailable: {cat}")
            continue
        found = re.findall(r"/articles/(\d+)/", listing)
        new = [i for i in dict.fromkeys(found) if i not in article_ids]
        article_ids.extend(new)
        print(f"[ids] {cat.rsplit('/', 2)[-2]}: +{len(new)} "
              f"(total {len(article_ids)})")
    article_ids.sort(key=int)

    # --- step 2: fetch the same ids under each language prefix --------------
    for lang, prefix in IR_LANGS.items():
        base = f"{IR}/{prefix}/articles/" if prefix else f"{IR}/articles/"
        print(f"\n[{lang}] {base}")
        n_art = n_q = n_stub = 0
        for aid in article_ids[:args.max]:
            u = f"{base}{aid}"
            if u in pages_done:
                continue
            h = fetcher.get(u)
            if h is None:
                continue
            pages_done.add(u)
            # A language that lacks this article answers with a short stub.
            if len(h) < 20000:
                n_stub += 1
                continue
            n_art += 1
            q, neg = build_records(u, lang, h, retrieved_at)
            for r in q:
                if r["id"] in seen_ids:
                    continue
                seen_ids.add(r["id"])
                all_quotes.append(r)
                n_q += 1
            all_negs.extend(neg)

        print(f"    {n_art} pages ({n_stub} not published in this language) "
              f"-> {n_q} quotations")
        per_source.append({"lang": lang, "seed": base, "status": "ok",
                           "articles": n_art, "missing_in_language": n_stub,
                           "quotes": n_q})

    # --- step 2b: islamqa.info, driven from the per-language sitemaps -------
    for lang, sm_url in ISLAMQA_SITEMAPS.items():
        print(f"\n[{lang}] islamqa.info")
        sm = fetcher.get(sm_url)
        if sm is None:
            print("    sitemap unavailable")
            per_source.append({"lang": lang, "seed": sm_url,
                               "status": "unavailable", "articles": 0,
                               "quotes": 0})
            continue
        urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sm)
        # Deterministic spread over the whole sitemap rather than the first N,
        # which would all be one topic and one scholar's house style.
        # islamqa answers are mostly fiqh and cite hadith rather than
        # verses, so the yield per page is far lower than a da'wah article's.
        # Sample proportionally more of them to get a usable count for the
        # two languages no other verified host serves.
        budget = args.max * ISLAMQA_BUDGET_MULTIPLIER
        step = max(1, len(urls) // max(budget, 1))
        picked = urls[::step][:budget]
        print(f"    sitemap {len(urls)} urls -> sampling {len(picked)}")

        n_art = n_q = 0
        for u in picked:
            if u in pages_done:
                continue
            h = fetcher.get(u)
            if h is None:
                continue
            pages_done.add(u)
            n_art += 1
            q, neg = build_records(u, lang, h, retrieved_at)
            for r in q:
                if r["id"] in seen_ids:
                    continue
                seen_ids.add(r["id"])
                all_quotes.append(r)
                n_q += 1
            all_negs.extend(neg)
        print(f"    {n_art} pages -> {n_q} quotations")
        per_source.append({"lang": lang, "seed": sm_url, "status": "ok",
                           "sitemap_urls": len(urls), "articles": n_art,
                           "quotes": n_q})

    # --- step 3: any extra hosts -------------------------------------------
    for src in EXTRA_SOURCES:
        lang, seed = src["lang"], src["seed"]
        prefix = src.get("prefix")
        print(f"\n[{lang}] seed {seed}")
        listing = fetcher.get(seed)
        if listing is None:
            print("    seed unavailable")
            per_source.append({"lang": lang, "seed": seed,
                               "status": "unavailable", "articles": 0,
                               "quotes": 0})
            continue
        arts = discover_articles(listing, seed, prefix)
        print(f"    {len(arts)} article links")
        n_art = n_q = 0
        for u in arts[:args.max]:
            if u in pages_done:
                continue
            h = fetcher.get(u)
            if h is None:
                continue
            pages_done.add(u)
            n_art += 1
            q, neg = build_records(u, lang, h, retrieved_at)
            for r in q:
                if r["id"] in seen_ids:
                    continue
                seen_ids.add(r["id"])
                all_quotes.append(r)
                n_q += 1
            all_negs.extend(neg)
        print(f"    {n_art} pages -> {n_q} quotations")
        per_source.append({"lang": lang, "seed": seed, "status": "ok",
                           "articles": n_art, "quotes": n_q})

    with open(CORPUS, "w", encoding="utf-8") as fh:
        for r in all_quotes:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(NEGATIVES, "w", encoding="utf-8") as fh:
        for r in all_negs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    by_lang: dict[str, int] = {}
    by_host: dict[str, int] = {}
    with_ref = 0
    for r in all_quotes:
        by_lang[r["lang"]] = by_lang.get(r["lang"], 0) + 1
        by_host[r["source_host"]] = by_host.get(r["source_host"], 0) + 1
        if r["printed_ref"]:
            with_ref += 1

    manifest = {
        "retrieved_at": retrieved_at,
        "user_agent": USER_AGENT,
        "default_delay_seconds": DEFAULT_DELAY,
        "max_pages_per_host": args.max,
        "robots": fetcher.robots_log,
        "sources": per_source,
        "pages_fetched": len(pages_done),
        "quotations": len(all_quotes),
        "quotations_with_printed_ref": with_ref,
        "negative_paragraphs": len(all_negs),
        "by_language": by_lang,
        "by_host": by_host,
        "license_note": LICENSE_NOTE,
    }
    with open(MANIFEST, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    print(f"\n{'='*64}")
    print(f"pages              {len(pages_done)}")
    print(f"quotations         {len(all_quotes)}  ({with_ref} with a printed ref)")
    print(f"negative prose     {len(all_negs)}")
    print(f"by language        {by_lang}")
    print(f"by host            {by_host}")
    print(f"written            {CORPUS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
