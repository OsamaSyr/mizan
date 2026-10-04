/* MIZAN — main page. Vanilla JS, no build step.
 *
 * It renders what the server returns and nothing more. It never decides a
 * state, never rewrites a quotation, and never calls a text "wrong".
 * Panels: (1) attribution, (2) terminology, (3) clarity & audience fit —
 * the third is hidden by default (SHOW_CLARITY below).
 */
(function () {
  'use strict';

  var esc = MZ.esc, safeUrl = MZ.safeUrl, api = MZ.api;

  /* Fixed, reviewed samples. Nothing is generated at runtime. */
  var SAMPLES = {
    s1: {
      lang: 'en',
      ar: 'ثم بيّن سبحانه أن الخلق لم يُترك سدى، فقال تعالى: ﴿وَمَا خَلَقْتُ الْجِنَّ وَالْإِنسَ إِلَّا لِيَعْبُدُونِ﴾ [الذاريات: ٥٦]. وهذه الآية أصلٌ في بيان الغاية من وجود الإنسان، وعليها يُبنى ما بعدها من أحكام.',
      en: 'Then He clarified that creation was not left purposeless, saying: "And I did not create the jinn and mankind except to serve Me." (Quran 51:56). This verse is a foundation for understanding the purpose of human existence.'
    },
    s2: {
      lang: 'en',
      ar: 'قال الله تعالى في شأن عيسى عليه السلام: ﴿إِنَّ مَثَلَ عِيسَىٰ عِندَ اللَّهِ كَمَثَلِ آدَمَ﴾ [آل عمران: ٥٩]، فالخلق بلا أب ليس دليلًا على الألوهية.',
      en: 'God said concerning Jesus, peace be upon him: "Indeed, the likeness of Jesus with God is as the likeness of Adam." (Quran 3:59). Being created without a father is no proof of divinity.'
    },
    s3: {
      lang: 'en',
      ar: 'ومن عظيم ما وصف الله به نفسه قوله: ﴿اللَّهُ لَا إِلَٰهَ إِلَّا هُوَ الْحَيُّ الْقَيُّومُ﴾ [البقرة: ٢٥٥]، وهي آية الكرسي.',
      en: 'Among the greatest of His descriptions of Himself: "Allah! There is no deity save Him, the Alive, the Eternal." (Quran 2:255). This is the Verse of the Throne.'
    }
  };

  /* Document-level verdict copy. One refer item pulls the whole document. */
  var VERDICT = {
    REFER: { cls: 'v-refer', big: 'يُحال للمراجعة',
      why: 'بندُ إحالة واحد يسحب الوثيقة كلها — بوابة يمكن تمييع نتيجتها ليست بوابة.' },
    ATTRIBUTED_WITH_EDITS: { cls: 'v-near', big: 'مُسنَد مع تعديل',
      why: 'نتيجة محفوظة من نسخة سابقة. منذ ٤ أكتوبر ٢٠٢٦ يُحال كل اقتباس تختلف كلماته عن الترجمة المعتمدة.' },
    ATTRIBUTED: { cls: 'v-ok', big: 'مُسنَد بالكامل',
      why: 'كل اقتباس يطابق ترجمة معتمدة منشورة مفهرسة.' },
    NO_APPROVED_TRANSLATION: { cls: 'v-near', big: 'لا ترجمة معتمدة بهذه اللغة',
      why: 'الاقتباس محلول، ولا يوجد نص معتمد مفهرس بهذه اللغة. استخدم العربي مع حاشية مترجم.' },
    NO_QUOTES: { cls: '', big: 'لم يُعثر على اقتباس',
      why: 'لم نجد آية مقتبسة في النص. مِيزان يفحص الآيات القرآنية فقط؛ كلام الكاتب والأحاديث خارج نطاقه.' }
  };

  /* Panel 3 vocabulary, in Arabic. The server's English text is the fallback. */
  var REFUSAL = {
    SCRIPTURE: ['نص شرعي', 'تحتوي نصًّا قرآنيًا أو تتداخل معه. مِيزان لا يعيد صياغة النص المنزَّل أبدًا.'],
    REFERENCE: ['إحالة مرجعية', 'تحمل إحالة إلى آية أو حديث؛ صياغتها جزء من الإسناد نفسه.'],
    NORMATIVE: ['لغة حكم شرعي', 'تستعمل صيغة حكم (يجب، يحرم، يجوز…). مِيزان لا يكتب الأحكام ولا يخففها ولا يعيد صياغتها.'],
    IMPLICIT_OBLIGATION: ['إلزام ضمني', 'تقرّر واجبًا بلا صيغة أمر صريحة؛ إعادة صياغتها قد تحوّل الحكم إلى عادة أو العكس.'],
    PREFERENCE: ['تفضيل ومفاضلة', 'تفاضل بين الأعمال، والمفاضلة حكم اجتهادي لا قرار تحريري.'],
    ATTRIBUTION: ['نسبة قول', 'تنسب قولًا إلى عالم أو مذهب أو مصدر؛ ألفاظها هي النسبة نفسها فلا تُرخى.'],
    CREED: ['مسألة عقدية', 'تقرّر مسألة في العقيدة مباشرة؛ تُفحص ولا تُكيَّف.'],
    TOO_SHORT: ['قصيرة جدًا', 'أقصر من أن تحمل مشكلة وضوح قابلة للقياس؛ تكييفها مخاطرة بلا فائدة.'],
    ROLLBACK: ['تراجع عن التكييف', 'كُيّفت ثم أُلغي التكييف لأن التحقق البايتي لم يتطابق؛ أُعيدت الجملة الأصلية كما هي.'],
    CONDITIONAL: ['جملة شرطية', 'فيها شرط («إذا»، «من»، «إلا»…)، وتقسيمها قد يفصل الحكم عن شرطه فيغيّر معناه.'],
    SCRIPTURE_LIKE: ['تشبه نص آية', 'تشترك في عبارة متصلة مع ترجمة معتمدة لآية، وإن لم تُكتشف اقتباسًا؛ قد تكون آية غير منصَّصة، فتُترك كما هي.'],
    SCRIPTURE_UNCHECKED: ['لم يُتحقق من خلوّها من نص قرآني', 'لا فهرس معتمد متاح لهذه اللغة للتأكد من أنها ليست آية، فلا تُمسّ.'],
    LANGUAGE_UNSCREENED: ['لغة بلا فحص للأحكام', 'كشف ألفاظ الأحكام متاح للإنجليزية فقط؛ في غيرها لا يُكيِّف مِيزان شيئًا ولا يقترح تعديلًا.']
  };
  var SUGGESTION = {
    SPLIT: 'تقسيم جملة طويلة',
    LENGTH: 'جملة أطول من المستهدف',
    PASSIVE: 'صيغة مبني للمجهول',
    NOUN_CHAIN: 'تتابع أسماء',
    GLOSS: 'مصطلح يُقترح تعريفه'
  };
  var TERM_STATUS = {
    APPROVED: ['معتمد', 's-match'],
    VARIANT: ['صيغة بديلة', 's-near'],
    MISSING: ['غائب عن الترجمة', 's-none'],
    NOT_IN_GLOSSARY: ['خارج المعجم', 's-nolang']
  };

  /* Panel 3 (clarity) is built and tested but hidden: it is English-only and,
     after the red-team fixes, adapts few sentences. Set true to show it. */
  var SHOW_CLARITY = false;

  var $ = function (id) { return document.getElementById(id); };
  var ar = $('ar'), en = $('en'), langSel = $('lang'), runBtn = $('run'), out = $('out');

  var health = null;
  var levels = [];
  var level = 'practising';
  var last = null;          // { data, translation }
  var langDir = {};
  var langScript = {};

  /* --- samples ----------------------------------------------------------- */

  var chips = ['s1', 's2', 's3'].map($).filter(Boolean);
  chips.forEach(function (c) {
    c.addEventListener('click', function () {
      var d = SAMPLES[c.id];
      if (!d) return;
      chips.forEach(function (o) { o.setAttribute('aria-pressed', String(o === c)); });
      ar.value = d.ar;
      en.value = d.en;
      if (d.lang) selectLang(d.lang);
      out.innerHTML = '';      // the user presses «افحص الإسناد» themselves
    });
  });

  function selectLang(code) {
    for (var i = 0; i < langSel.options.length; i++) {
      if (langSel.options[i].value === code) { langSel.selectedIndex = i; break; }
    }
    applyDir();
  }

  function applyDir() {
    var dir = langDir[langSel.value] || 'ltr';
    en.setAttribute('dir', dir);
    en.classList.toggle('ltr', dir === 'ltr');
  }
  langSel.addEventListener('change', applyDir);

  /* --- boot data --------------------------------------------------------- */

  function loadLanguages() {
    return api('/api/languages').then(function (d) {
      langSel.innerHTML = '';
      (d.languages || []).forEach(function (l) {
        langDir[l.code] = l.direction;
        langScript[l.code] = l.script;
        var o = document.createElement('option');
        o.value = l.code;
        o.textContent = l.name_ar + ' — ' + l.n_translations + ' ترجمة معتمدة مفهرسة';
        langSel.appendChild(o);
      });
      selectLang('en');
    });
  }

  function loadHealth() {
    return api('/api/health').then(function (h) {
      health = h;
      levels = h.levels || [];
      level = h.default_level || level;
      $('footstats').textContent =
        'فهرس ' + h.index_version + ' · ' + h.n_translations + ' ترجمة معتمدة بـ ' +
        h.n_languages + ' لغات · ' + h.n_texts.toLocaleString('en-US') + ' نصًّا مفهرسًا · ' +
        h.n_verses.toLocaleString('en-US') + ' آية';
      if (h.privacy) $('privacy').textContent = h.privacy;
      setNavCount(h.review && h.review.open);
    });
  }

  function setNavCount(n) {
    var b = $('navcount');
    if (!b) return;
    b.hidden = !n;
    b.textContent = n ? String(n) : '';
  }

  /* --- rendering helpers ------------------------------------------------- */

  function diffSide(ops, side) {
    if (!ops || !ops.length) return '';
    var tag = side === 'published' ? 'del' : 'ins';
    return ops.map(function (op) {
      var words = op[side] || [];
      if (!words.length) return '';
      var t = esc(words.join(' '));
      return op.op === 'equal' ? t : '<' + tag + '>' + t + '</' + tag + '>';
    }).filter(Boolean).join(' ');
  }

  function asText(v) {
    if (Array.isArray(v)) return v.filter(Boolean).join(' / ');
    return v == null ? '' : String(v);
  }

  function link(url, label) {
    return '<a href="' + safeUrl(url) + '" target="_blank" rel="noopener noreferrer">' + esc(label) + ' ↗</a>';
  }

  function stateCls(f) {
    return MZ.STATE_CLS[f.state] || (f.refer ? 's-none' : 's-nolang');
  }

  /* --- panel 1: attribution ---------------------------------------------- */
  /* Written for an editor, not an engineer: what each quotation is, where it
     comes from, and what to do. Scores, detection tiers, runner-up editions
     and ids stay in the downloadable report and the API. Two things are
     always shown because the binding standard requires them: the approved
     source (traceability) and an AI label when the model found the verse
     (transparency). */

  var BADGE = {
    MATCH: '✓ منقولة حرفيًا من ترجمة معتمدة',
    NEAR: 'كلماتها مختلفة عن الترجمة المعتمدة',
    UNATTRIBUTED: 'لا تطابق أي ترجمة معتمدة',
    NO_APPROVED_TRANSLATION: 'لا ترجمة معتمدة بهذه اللغة',
    UNRESOLVED: 'لم نتعرّف على الآية'
  };

  function nChanged(f) {
    return (f.diff || []).filter(function (op) { return op.op !== 'equal'; }).length;
  }

  function findingCard(f, d) {
    var dir = d.lang_dir || 'ltr';
    var hasDiff = f.diff && f.diff.some(function (op) { return op.op !== 'equal'; });
    var book = f.attributed || f.closest;
    var h = '<article class="finding" id="finding-' + f.idx + '">';

    h += '<div class="f-head">';
    h += '<span class="ref">' + esc(f.ref || '؟') + '</span>';
    h += '<span class="state ' + stateCls(f) + '">' + esc(BADGE[f.state] || f.state_label) + '</span>';
    if (f.ai_tier) h += '<span class="tier tier-ai">وجدها الذكاء الاصطناعي</span>';
    h += '</div>';

    // One plain sentence: what this is.
    var line = '';
    if (f.state === 'MATCH') {
      line = 'المصدر: <b>' + esc(book ? book.title : '') + '</b>' +
             (f.attributed && f.attributed.source_url ? ' — ' + link(f.attributed.source_url, 'افتحها في Quranpedia') : '');
    } else if (f.state === 'NEAR') {
      var n = nChanged(f);
      line = 'قريبة من <b>' + esc(book ? book.title : 'ترجمة معتمدة') + '</b>، لكن ' +
             (n === 1 ? 'فيها موضع مختلف واحد' : 'فيها ' + n + ' مواضع مختلفة') +
             ' (مظلَّل). أي تغيير في نص آية، ولو كلمة، يُراجَع قبل النشر.';
    } else if (f.state === 'UNATTRIBUTED') {
      line = f.published_text
        ? 'لا تطابق أيًّا من الترجمات المعتمدة المفهرسة. قد تكون من ترجمة غير مفهرسة، أو معدَّلة — يراجعها مختص.'
        : 'الآية مقتبسة في النص العربي، ولم نجدها في الترجمة؛ ربما حُذفت أو أعيدت صياغتها.';
    } else if (f.state === 'NO_APPROVED_TRANSLATION') {
      line = f.reason === 'script_mismatch'
        ? 'النص ليس بخط اللغة المختارة. اختر لغة الترجمة الصحيحة.'
        : 'لا توجد ترجمة معتمدة مفهرسة بهذه اللغة. استخدم النص العربي مع حاشية مترجم.';
    } else if (f.state === 'UNRESOLVED') {
      line = 'لم نتعرّف على هذا الاقتباس. أضف رقم الآية بجواره، أو أرسله للمراجعة.';
    }
    if (line) h += '<p class="f-line">' + line + '</p>';
    (f.flag_messages || []).forEach(function (m) {
      h += '<p class="f-line f-flag">⚠ ' + esc(m) + '</p>';
    });

    // The texts. A MATCH is shown once — both sides are the same words.
    if (f.published_text) {
      h += '<div class="textblock"><div class="tb-k">' +
           (f.state === 'MATCH' ? 'النص' : 'المنشور في المقال') + '</div>' +
           '<div class="tb-v" dir="' + dir + '">' +
           (hasDiff && f.state !== 'MATCH' ? diffSide(f.diff, 'published') : esc(f.published_text)) +
           '</div></div>';
    }
    if (f.approved_text && f.state !== 'MATCH') {
      h += '<div class="textblock approved"><div class="tb-k">' +
           (f.state === 'NEAR' ? 'النص المعتمد' : 'أقرب نص معتمد، للمقارنة') +
           (book ? ' — ' + esc(book.title) : '') + '</div>' +
           '<div class="tb-v" dir="' + dir + '">' +
           (hasDiff ? diffSide(f.diff, 'approved') : esc(f.approved_text)) + '</div></div>';
      if (book && book.source_url) {
        h += '<div class="src">' + link(book.source_url, 'افتح الترجمة في Quranpedia') + '</div>';
      }
    }

    if (f.refer) {
      h += '<div class="actions" id="ra-' + f.idx + '">' +
           '<button type="button" class="btn btn-strong" data-action="review" data-idx="' + f.idx + '">' +
           'أرسل للمراجعة</button>' +
           '<span class="muted">يقرّر المراجع: اعتمادها كما هي، أو استبدالها بالنص المعتمد، أو إحالتها إلى عالِم.</span></div>';
    }

    h += '</article>';
    return h;
  }

  function attributionPanel(d) {
    var findings = d.findings || [];
    var h = '<section class="psec" aria-labelledby="p1h"><h2 class="ph" id="p1h">الآيات المقتبسة في النص</h2>';
    if (!findings.length) {
      h += '<div class="empty">' + esc(VERDICT.NO_QUOTES.why) + '</div></section>';
      return h;
    }
    findings.forEach(function (f) { h += findingCard(f, d); });
    return h + '</section>';
  }

  /* --- panel 2: terminology ---------------------------------------------- */

  function termsPanel(t, d) {
    if (!t) return '';   // module absent: the section does not exist
    var dir = d.lang_dir || 'ltr';
    var rows = t.findings || [];
    var attention = rows.filter(function (r) { return r.status !== 'APPROVED'; }).length;
    var summary = t.error ? 'تعذّر فحص المصطلحات'
      : !rows.length ? 'لا مصطلحات من المعجم المعتمد في النص'
      : rows.length + (rows.length === 1 ? ' مصطلح' : ' مصطلحات') +
        (attention ? '، ' + attention + ' تحتاج انتباهًا' : '، كلها بالمقابل المعتمد');
    var h = '<details class="psec terms"' + (attention ? ' open' : '') + '><summary class="ph">' +
            'المصطلحات الشرعية <span class="muted small">— ' + esc(summary) + '</span></summary>';
    if (t.error) return h + '<div class="note">' + esc(t.error) + '</div></details>';
    if (!rows.length) {
      return h + '<div class="empty">لم يُعثر في النص على مصطلح من معجم الجمهرة المعتمد.</div></details>';
    }
    h += '<div class="tablewrap"><table class="tbl"><thead><tr><th>المصطلح</th><th>المعتمد</th>' +
         '<th>الحالة</th><th>ملاحظة</th><th></th></tr></thead><tbody>';
    rows.forEach(function (r) {
      var st = TERM_STATUS[r.status] || [r.status, 's-nolang'];
      h += '<tr><td class="term-ar">' + esc(r.term_ar) + '</td>' +
           '<td dir="ltr">' + esc(asText(r.approved_en) || '—') + '</td>' +
           '<td><span class="state ' + st[1] + '">' + esc(st[0]) + '</span></td>' +
           '<td dir="ltr" class="term-note">' + esc(asText(r.suggestion) || '—') + '</td>' +
           '<td>' + (r.source_url ? link(r.source_url, 'المصدر') : '') + '</td></tr>';
    });
    h += '</tbody></table></div>';
    var g = t.glossary || {};
    h += '<p class="muted small">' + (g.reference ? 'المرجع: ' + esc(g.reference) +
         (g.terms ? ' — ' + esc(g.terms) + ' مصطلحًا. ' : '. ') : '') +
         'الملاحظات قواعد حتمية من المعجم والحزمة العلمية، لا يولّدها نموذج، ولا يُغيَّر بها أي نص؛ القرار للمحرّر.</p>';
    if (t.notes && t.notes.length) {
      h += '<ul class="term-notes" dir="ltr">' + t.notes.map(function (n) {
        return '<li>' + esc(n) + '</li>';
      }).join('') + '</ul>';
    }
    return h + '</details>';
  }

  /* --- panel 3: clarity -------------------------------------------------- */

  function levelPicker() {
    var h = '<div class="seg" role="radiogroup" aria-label="الجمهور المستهدف">';
    levels.forEach(function (l) {
      var on = l.code === level;
      h += '<button type="button" role="radio" aria-checked="' + on + '" data-action="level" data-level="' +
           esc(l.code) + '">' + esc(l.label_ar) + '</button>';
    });
    h += '</div>';
    var cur = levels.filter(function (l) { return l.code === level; })[0];
    if (cur) h += '<p class="muted small level-desc">' + esc(cur.description_ar) + '</p>';
    return h;
  }

  function pct(x) { return Math.round((x || 0) * 100); }

  function readabilityTable(c, lang) {
    var b = c.readability_before || {}, a = c.readability_after || {};
    var latin = langScript[lang] === 'latin';
    var rows = [];
    if (latin && b.flesch != null) {
      rows.push(['سهولة القراءة (فليش) — الأعلى أسهل', b.flesch, a.flesch]);
    }
    rows.push(['متوسط الكلمات في الجملة', b.words_per_sentence, a.words_per_sentence]);
    rows.push(['أطول جملة (كلمات)', b.longest_sentence_words, a.longest_sentence_words]);
    rows.push(['عدد الجمل', b.n_sentences, a.n_sentences]);
    var h = '<table class="tbl rd"><thead><tr><th>المقياس</th><th>قبل</th><th>بعد</th></tr></thead><tbody>';
    rows.forEach(function (r) {
      h += '<tr><td>' + esc(r[0]) + '</td><td class="num">' + esc(r[1] == null ? '—' : r[1]) +
           '</td><td class="num">' + esc(r[2] == null ? '—' : r[2]) + '</td></tr>';
    });
    h += '</tbody></table>';
    if (!latin) {
      h += '<p class="muted small">مؤشر فليش مقياس إنجليزي، فلا يُعرض لهذه اللغة؛ طول الجمل هو المقياس هنا.</p>';
    }
    return h;
  }

  function clarityBody(c, d) {
    if (!c) return '';
    if (c.error) return '<div class="note">' + esc(c.error) + '</div>';
    var dir = d.lang_dir || 'ltr';
    var counts = c.counts || {};
    var h = '';

    // 1. The frozen-text guarantee, stated first.
    var fr = c.frozen || {};
    if (fr.verified_byte_exact && fr.n) {
      h += '<div class="frozen ok"><b>✓ النص المجمَّد تحقّق بايتيًا.</b> ' + esc(fr.n) +
           ' مقطعًا مجمَّدًا (اقتباسات قرآنية ومصطلحات معتمدة) أُعيد حرفًا بحرف، ولم يُعرض على أي مُكيِّف.</div>';
    } else if (fr.verified_byte_exact) {
      h += '<div class="frozen ok"><b>✓ لا نص مجمَّد هنا.</b> لم يُكيَّف أي مقطع يحمل اقتباسًا قرآنيًا أو مصطلحًا معتمدًا، ' +
           'فلا شيء احتاج إلى تحقق بايتي.</div>';
    } else {
      h += '<div class="frozen bad"><b>✗ فشل التحقق البايتي.</b> لم يُقبل أي تكييف، وأُعيد النص الأصلي كما هو.</div>';
    }

    // 1b. Languages without a ruling screen: say so before anything else.
    var ls = c.language_support || {};
    if (ls.ruling_screen === false) {
      h += '<div class="note"><b>تحسين الوضوح متاح للإنجليزية فقط.</b> ' +
           esc(ls.note_ar || 'في هذه اللغة لا يكشف مِيزان ألفاظ الأحكام، فلا يُكيِّف أي جملة ولا يقترح تعديلًا.') +
           '</div>';
    }

    // 2. Refusals, as the headline feature.
    var refused = counts.refused || 0, prose = counts.prose || 0;
    h += '<div class="refusals">';
    if (refused) {
      h += '<h3>امتنع مِيزان عن تكييف ' + refused + ' جملة من أصل ' + prose + ' جملة نثرية (' +
           pct(c.refusal_rate) + '٪)، وهذا سببها:</h3>';
    } else {
      h += '<h3>لم يمتنع مِيزان عن أي جملة نثرية في هذه الوثيقة.</h3>';
    }
    if (counts.quoted) {
      h += '<p class="muted">و' + counts.quoted + ' جملة تحمل نصًّا قرآنيًا، فهي مجمَّدة لا تُمسّ أصلًا ولا تُحسب امتناعًا.</p>';
    }
    var sents = c.sentences || [];
    (c.refusal_counts || []).forEach(function (r) {
      var ar = REFUSAL[r.code] || [r.code, r.message];
      var examples = sents.filter(function (s) {
        return s.kind === 'PROSE' && (s.refusal_reasons || []).some(function (x) { return x.code === r.code; });
      });
      h += '<details class="reason"><summary><b>' + esc(ar[0]) + '</b> — ' + r.n +
           (r.n === 1 ? ' جملة' : ' جمل') + '<span class="why">' + esc(ar[1]) + '</span></summary>';
      examples.forEach(function (s) {
        h += '<blockquote dir="' + dir + '">' + esc(s.original) + '</blockquote>';
      });
      h += '</details>';
    });
    if ((c.refusal_counts || []).length > 1) {
      h += '<p class="muted small">قد تجتمع في الجملة الواحدة أكثر من علّة، فيزيد مجموع العلل على عدد الجمل.</p>';
    }
    h += '</div>';

    // 3. Readability before/after.
    h += '<h3 class="sub3">مقاييس الوضوح قبل التكييف وبعده</h3>' + readabilityTable(c, d.lang);

    // 4. The adapted text, sentence by sentence, with what each one is.
    h += '<h3 class="sub3">النص بعد التكييف</h3>';
    h += '<div class="legend"><span class="cs cs-quoted">نص قرآني — مجمَّد</span>' +
         '<span class="cs cs-refused">امتنع عنها مِيزان</span>' +
         '<span class="cs cs-adapted">أعاد مِيزان تقسيمها آليًا</span>' +
         '<span class="cs cs-same">لم تتغيّر</span></div>';
    h += '<div class="adapted" dir="' + dir + '">';
    sents.forEach(function (s) {
      var cls = s.kind === 'QUOTED' ? 'cs-quoted'
        : s.adapted != null ? 'cs-adapted'
        : s.disposition === 'REFUSED' ? 'cs-refused' : 'cs-same';
      var title = s.kind === 'QUOTED' ? 'نص قرآني مجمَّد — لا يُمسّ'
        : (s.refusal_reasons || []).map(function (x) { return (REFUSAL[x.code] || [x.code])[0]; }).join('، ');
      h += '<span class="cs ' + cls + '"' + (title ? ' title="' + esc(title) + '"' : '') + '>' +
           esc(s.output) + '</span> ';
    });
    h += '</div>';

    var changed = sents.filter(function (s) { return s.adapted != null; });
    if (changed.length) {
      h += '<details class="changes"><summary>التغييرات المطبَّقة (' + changed.length + ')</summary>';
      changed.forEach(function (s) {
        h += '<div class="chg"><div class="k">قبل</div><div dir="' + dir + '">' + esc(s.original) + '</div>' +
             '<div class="k">بعد</div><div dir="' + dir + '">' + esc(s.adapted) + '</div></div>';
      });
      h += '</details>';
    }

    // 5. Suggestions to the editor — never applied, except SPLIT.
    var sugg = c.suggestions || [];
    h += '<h3 class="sub3">اقتراحات للمحرّر (' + sugg.length + ')</h3>';
    if (sugg.length) {
      h += '<ul class="sugg">';
      sugg.forEach(function (s) {
        h += '<li><span class="sg-code">' + esc(SUGGESTION[s.code] || s.code) + '</span>' +
             (s.applied ? '<span class="sg-applied">طُبِّق</span>' : '<span class="sg-open">للمحرّر</span>') +
             '<span class="sg-detail" dir="ltr">' + esc(s.detail) + '</span>' +
             (s.span ? '<span class="sg-span" dir="' + dir + '">«' + esc(s.span) + '»</span>' : '') + '</li>';
      });
      h += '</ul>';
    } else {
      h += '<p class="muted">لا اقتراحات عند هذا المستوى.</p>';
    }

    h += '<p class="muted small">' + (c.model_requested
      ? 'طُلب تكييف بنموذج لغوي (ذكاء اصطناعي). أي ناتج له لا يُقبل إلا بعد التحقق البايتي من النص المجمَّد وإعادة فحصه بالبوابة نفسها.'
      : 'التكييف هنا حتمي بلا أي نموذج: التحويل الوحيد المطبَّق تقسيم جملة طويلة عند حرف عطف، دون حذف كلمة أو إضافتها؛ وكل ما عداه اقتراح للمحرّر.') +
      '</p>';
    return h;
  }

  function clarityPanel(c, d) {
    if (!c) return '';   // module absent
    return '<section class="psec" aria-labelledby="p3h"><h2 class="ph" id="p3h">' +
           '<span class="pn">٣</span> الوضوح وملاءمة الجمهور — دون تغيير الجوهر</h2>' +
           levelPicker() + '<div id="p3body">' + clarityBody(c, d) + '</div></section>';
  }

  /* --- whole report ------------------------------------------------------ */

  function render(d) {
    var v = VERDICT[d.document_verdict] || { cls: '', big: d.verdict_label || d.document_verdict, why: '' };
    var fs = d.findings || [];
    var nRefer = fs.filter(function (f) { return f.refer; }).length;
    var nMatch = fs.filter(function (f) { return f.state === 'MATCH' && !f.refer; }).length;
    // Say what was FOUND: a quotation MIZAN did not detect is not vouched for.
    var plain = !fs.length ? v.why
      : fs.length === 1 ? (nRefer ? 'وجدنا آية واحدة، وتحتاج مراجعة قبل النشر.'
                                  : 'وجدنا آية واحدة، وهي منقولة حرفيًا من ترجمة معتمدة.')
      : nRefer ? 'وجدنا ' + fs.length + ' آيات: ' + (nRefer === 1 ? 'واحدة' : nRefer) +
                 ' تحتاج مراجعة قبل النشر' + (nMatch ? '، و' + nMatch + ' منقولة حرفيًا من ترجمات معتمدة.' : '.')
      : 'وجدنا ' + fs.length + ' آيات، كلها منقولة حرفيًا من ترجمات معتمدة.';
    if (fs.length && !nRefer) {
      plain += ' تأكّد أن كل آية في نصك ظاهرة في القائمة أدناه؛ الآية القصيرة جدًا بلا رقم قد لا تُكتشف.';
    }
    if (d.document_verdict === 'NO_APPROVED_TRANSLATION') plain = v.why;
    var h = '<div class="verdict ' + v.cls + '">' +
            '<div class="big">' + esc(v.big) + '</div>' +
            '<div class="why">' + esc(plain) + '</div>' +
            (nRefer > 1 ? '<div class="v-act"><button type="button" class="btn btn-strong" data-action="review-all">' +
                      'أرسل ' + (nRefer === 1 ? 'الآية' : 'الآيات') + ' للمراجعة</button></div>' : '') +
            '</div>';

    if (d.pipeline_note) h += '<div class="banner warn">' + esc(d.pipeline_note) + '</div>';
    if (d.ai_assisted) {
      h += '<div class="banner ai"><b>إفصاح:</b> ' + esc(d.ai_disclosure) + '</div>';
    }

    var L = d.links || {};
    h += '<div class="toolbar">' +
         '<a class="btn" href="' + safeUrl(L.report_html) + '" target="_blank" rel="noopener">عرض التقرير</a>' +
         '<a class="btn" href="' + safeUrl(L.report_download) + '">تنزيل التقرير</a></div>';

    h += attributionPanel(d);
    h += termsPanel(d.terms, d);
    if (SHOW_CLARITY) h += clarityPanel(d.clarity, d);
    out.innerHTML = h;
  }

  /* --- actions ----------------------------------------------------------- */

  function markQueued(item) {
    var box = $('ra-' + item.finding_idx);
    if (!box) return;
    box.innerHTML = '<span class="queued">في طابور المراجعة — بند #' + esc(item.id) + ' (' +
                    esc(item.status_label) + ')</span> <a href="/review#item-' + esc(item.id) + '">افتحه ↗</a>';
  }

  function sendToReview(ids, btn) {
    if (!last) return;
    if (btn) { btn.disabled = true; btn.textContent = '…يُرسل'; }
    api('/api/review', { method: 'POST', body: { check_id: last.data.check_id, finding_ids: ids } })
      .then(function (r) {
        (r.items || []).forEach(markQueued);
        if (btn && btn.dataset.action === 'review-all') btn.textContent = 'أُرسلت إلى الطابور';
        return api('/api/health').then(function (h) { setNavCount(h.review && h.review.open); });
      })
      .catch(function (e) {
        if (btn) { btn.disabled = false; btn.textContent = 'أعد المحاولة'; }
        var box = ids.length === 1 ? $('ra-' + ids[0]) : out.querySelector('.toolbar');
        if (box) {
          var m = box.querySelector('.inline-err') || document.createElement('span');
          m.className = 'inline-err';
          m.textContent = e.message;
          box.appendChild(m);
        }
      });
  }

  function setLevel(code) {
    level = code;
    if (!last || !last.data.clarity) return;
    var body = $('p3body');
    var group = out.querySelector('.seg');
    if (group) {
      Array.prototype.forEach.call(group.querySelectorAll('button'), function (b) {
        b.setAttribute('aria-checked', String(b.dataset.level === code));
      });
      var cur = levels.filter(function (l) { return l.code === code; })[0];
      var desc = out.querySelector('.level-desc');
      if (desc && cur) desc.textContent = cur.description_ar;
    }
    if (body) body.classList.add('loading');
    api('/api/clarity', { method: 'POST', timeoutMs: 60000,
      body: { check_id: last.data.check_id, translation: last.translation, level: code } })
      .then(function (r) {
        last.data.clarity = r.clarity;
        if (body) { body.innerHTML = clarityBody(r.clarity, last.data); body.classList.remove('loading'); }
      })
      .catch(function (e) {
        if (body) { body.innerHTML = '<div class="note">' + esc(e.message) + '</div>'; body.classList.remove('loading'); }
      });
  }

  out.addEventListener('click', function (ev) {
    var t = ev.target.closest('[data-action]');
    if (!t) return;
    var action = t.dataset.action;
    if (action === 'review') sendToReview([Number(t.dataset.idx)], t);
    else if (action === 'review-all' && last) {
      sendToReview(last.data.findings.filter(function (f) { return f.refer; })
        .map(function (f) { return f.idx; }), t);
    } else if (action === 'level') setLevel(t.dataset.level);
  });

  /* --- run --------------------------------------------------------------- */

  function run() {
    var translation = (en.value || '').trim();
    if (!translation) {
      out.innerHTML = '<div class="empty">الصق الترجمة المراد فحصها أولًا — النص العربي اختياري.</div>';
      en.focus();
      return;
    }
    var max = health && health.limits && health.limits.max_translation_chars;
    if (max && translation.length > max) {
      out.innerHTML = '<div class="empty err">الترجمة أطول من الحد المسموح (' +
        translation.length.toLocaleString('en-US') + ' حرفًا، والحد ' + max.toLocaleString('en-US') +
        '). قسّمها إلى أجزاء وافحص كل جزء على حدة.</div>';
      return;
    }
    runBtn.disabled = true;
    runBtn.textContent = '…يفحص';

    api('/api/check', {
      method: 'POST', timeoutMs: 120000,
      body: { arabic: ar.value || '', translation: translation, lang: langSel.value || 'en', level: level }
    })
      .then(function (d) {
        last = { data: d, translation: translation };
        render(d);
      })
      .catch(function (e) {
        out.innerHTML = '<div class="empty err">تعذّر الفحص: ' + esc(e.message) + '</div>';
      })
      .then(function () {
        runBtn.disabled = false;
        runBtn.textContent = 'افحص الإسناد';
      });
  }

  runBtn.addEventListener('click', run);

  /* Boot: load index metadata. The boxes start empty; a sample fills them. */
  Promise.all([loadLanguages(), loadHealth()]).catch(function (e) {
    out.innerHTML = '<div class="empty err">تعذّر تحميل الفهرس: ' + esc(e.message) + '</div>';
  });
})();
