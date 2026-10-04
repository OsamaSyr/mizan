/* MIZAN — reviewer page. A person decides; MIZAN records the decision.
 *
 * Decisions are append-only. This page never edits or deletes one: a changed
 * mind is a new decision, and the earlier one stays in the history.
 */
(function () {
  'use strict';

  var esc = MZ.esc, safeUrl = MZ.safeUrl, api = MZ.api, fmtTime = MZ.fmtTime;
  var $ = function (id) { return document.getElementById(id); };

  var DECISIONS = [
    ['approve_as_published', 'اعتماد كما نُشر', 'يبقى النص المنشور كما هو، على مسؤولية المراجع.'],
    ['replace_with_approved', 'استبدال بالنص المعتمد', 'يُستبدل بنص ترجمة معتمدة تختارها، منقولًا حرفيًا من الفهرس.'],
    ['escalate', 'إحالة إلى عالم مختص', 'يبقى البند مفتوحًا حتى يقرّر من له الأهلية.']
  ];
  var STATUS_CLS = {
    pending: 's-none', escalated: 's-near',
    approved_as_published: 's-match', replaced_with_approved: 's-match'
  };

  var status = 'open';
  var langDir = {};
  var items = {};
  var queue = $('queue'), logBox = $('log'), reviewerInput = $('reviewer');

  function dirFor(lang) { return langDir[lang] || 'ltr'; }

  function link(url, label) {
    return '<a href="' + safeUrl(url) + '" target="_blank" rel="noopener noreferrer">' + esc(label) + ' ↗</a>';
  }

  /* --- one item ---------------------------------------------------------- */

  function itemCard(it) {
    var dir = dirFor(it.lang);
    var h = '<article class="finding ritem" id="item-' + it.id + '">';
    h += '<div class="f-head"><span class="ref">' + esc(it.ref || '؟') + '</span>' +
         '<span class="state ' + (STATUS_CLS[it.status] || 's-nolang') + '">' + esc(it.status_label) + '</span>' +
         '<span class="muted small">بند #' + it.id + ' · أُرسل <span class="num">' + esc(fmtTime(it.created_at)) + '</span></span>' +
         '<a class="small" href="' + safeUrl(it.report_url) + '" target="_blank" rel="noopener">تقرير الفحص ↗</a></div>';

    h += '<div class="textblock"><div class="tb-k">النص المنشور <span>— كما في الترجمة</span></div>' +
         (it.published_text
           ? '<div class="tb-v" dir="' + dir + '">' + esc(it.published_text) + '</div>'
           : '<div class="tb-v muted">— اقتُبست الآية في النص العربي ولم يُعثر على موضعها في الترجمة</div>') +
         '</div>';
    if (it.closest) {
      h += '<div class="textblock approved"><div class="tb-k">أقرب نص معتمد — <b>' + esc(it.closest.title) +
           '</b> <span>— للمقارنة فقط، لا إسناد</span></div>' +
           '<div class="tb-v" dir="' + dir + '">' + esc(it.closest.text) + '</div></div>';
    }
    if (it.verse_url) h += '<div class="src">' + link(it.verse_url, 'الآية في المصدر') + '</div>';

    if (it.events && it.events.length) {
      h += '<ol class="hist">';
      it.events.forEach(function (ev) {
        h += '<li><b>' + esc(ev.decision_label) + '</b> — ' + esc(ev.reviewer) +
             ' <span class="muted num">' + esc(fmtTime(ev.created_at)) + '</span>' +
             (ev.note ? '<div class="evnote">' + esc(ev.note) + '</div>' : '') +
             (ev.replacement ? '<div class="textblock approved"><div class="tb-k">النص المعتمد المختار — <b>' +
               esc(ev.replacement.title) + '</b></div><div class="tb-v" dir="' + dir + '">' +
               esc(ev.replacement.text) + '</div></div>' : '') + '</li>';
      });
      h += '</ol>';
    }

    h += decisionForm(it);
    return h + '</article>';
  }

  function decisionForm(it) {
    var h = '<form class="decide" data-item="' + it.id + '" novalidate>';
    h += '<fieldset><legend>' + (it.events && it.events.length ? 'سجّل قرارًا جديدًا' : 'القرار') + '</legend>';
    DECISIONS.forEach(function (d) {
      var disabled = d[0] === 'replace_with_approved' && !(it.surah && it.ayah);
      h += '<label class="opt' + (disabled ? ' off' : '') + '"><input type="radio" name="decision" value="' + d[0] + '"' +
           (disabled ? ' disabled' : '') + '> <b>' + esc(d[1]) + '</b> <span class="muted small">' +
           esc(disabled ? 'غير متاح: البند لم يُحلّ إلى آية.' : d[2]) + '</span></label>';
    });
    h += '</fieldset>';
    h += '<div class="pick" hidden><label>الترجمة المعتمدة <select name="book"></select></label>' +
         '<div class="pick-preview"></div></div>';
    h += '<label class="notel">ملاحظة <span class="muted small">— اختيارية، تُحفظ في السجل</span>' +
         '<textarea name="note" maxlength="2000" rows="2"></textarea></label>';
    h += '<div class="form-row"><button type="submit" class="run">سجّل القرار</button>' +
         '<span class="formmsg" role="status"></span></div>';
    return h + '</form>';
  }

  /* --- replace: choose among approved renderings, verbatim --------------- */

  function loadRenderings(form, it) {
    var pick = form.querySelector('.pick');
    var sel = form.querySelector('select[name=book]');
    var prev = form.querySelector('.pick-preview');
    pick.hidden = false;
    if (sel.options.length) return;
    prev.textContent = '…تحميل الترجمات المعتمدة';
    api('/api/verse?ref=' + encodeURIComponent(it.surah + ':' + it.ayah) + '&lang=' + encodeURIComponent(it.lang))
      .then(function (d) {
        form._renderings = d.renderings || [];
        sel.innerHTML = form._renderings.map(function (r) {
          var isClosest = it.closest && it.closest.book_id === r.book_id;
          return '<option value="' + r.book_id + '"' + (isClosest ? ' selected' : '') + '>' +
                 esc(r.title) + (isClosest ? ' — الأقرب' : '') + '</option>';
        }).join('');
        showPreview(form, it);
      })
      .catch(function (e) { prev.textContent = e.message; });
    sel.addEventListener('change', function () { showPreview(form, it); });
  }

  function showPreview(form, it) {
    var sel = form.querySelector('select[name=book]');
    var prev = form.querySelector('.pick-preview');
    var r = (form._renderings || []).filter(function (x) { return String(x.book_id) === sel.value; })[0];
    prev.innerHTML = r
      ? '<div class="textblock approved"><div class="tb-k">منقول حرفيًا من الفهرس — لم يولَّد</div>' +
        '<div class="tb-v" dir="' + dirFor(it.lang) + '">' + esc(r.text) + '</div></div>' +
        '<div class="src">' + link(r.source_url, 'افتحها في المصدر') + '</div>'
      : '';
  }

  /* --- queue ------------------------------------------------------------- */

  function loadQueue() {
    return api('/api/review?status=' + status).then(function (d) {
      var c = d.counts || {};
      $('c-open').textContent = c.open || 0;
      $('c-closed').textContent = c.closed || 0;
      $('c-total').textContent = c.total || 0;
      setNavCount(c.open);
      items = {};
      (d.items || []).forEach(function (it) { items[it.id] = it; });
      if (!d.items || !d.items.length) {
        queue.innerHTML = '<div class="empty">' + (status === 'open'
          ? 'لا بنود مفتوحة. البنود المحالة تُرسَل إلى هنا من صفحة الفحص بزر «أرسل للمراجعة البشرية».'
          : 'لا بنود في هذا التصنيف.') + '</div>';
        return;
      }
      queue.innerHTML = d.items.map(itemCard).join('');
    }).catch(function (e) {
      queue.innerHTML = '<div class="empty err">تعذّر تحميل الطابور: ' + esc(e.message) + '</div>';
    });
  }

  function loadLog() {
    return api('/api/review/log').then(function (d) {
      var ev = d.events || [];
      if (!ev.length) { logBox.innerHTML = '<div class="empty">لم يُسجَّل أي قرار بعد.</div>'; return; }
      var h = '<div class="tablewrap"><table class="tbl"><thead><tr><th>#</th><th>الوقت (UTC)</th><th>البند</th>' +
              '<th>الآية</th><th>القرار</th><th>المراجع</th><th>الملاحظة</th></tr></thead><tbody>';
      ev.forEach(function (e) {
        h += '<tr><td class="num">' + e.id + '</td><td class="num">' + esc(fmtTime(e.created_at)) + '</td>' +
             '<td><a href="#item-' + e.item_id + '" data-goto="' + e.item_id + '">#' + e.item_id + '</a></td>' +
             '<td class="num">' + esc(e.ref || '؟') + '</td><td>' + esc(e.decision_label) +
             (e.replacement ? ' <span class="muted small">← ' + esc(e.replacement.title) + '</span>' : '') +
             '</td><td>' + esc(e.reviewer) + '</td><td>' + esc(e.note || '—') + '</td></tr>';
      });
      logBox.innerHTML = h + '</tbody></table></div>';
    }).catch(function (e) {
      logBox.innerHTML = '<div class="empty err">تعذّر تحميل السجل: ' + esc(e.message) + '</div>';
    });
  }

  function setNavCount(n) {
    var b = $('navcount');
    b.hidden = !n;
    b.textContent = n ? String(n) : '';
  }

  function setStatus(s) {
    status = s;
    Array.prototype.forEach.call(document.querySelectorAll('.filters button'), function (b) {
      b.setAttribute('aria-selected', String(b.dataset.status === s));
    });
    return loadQueue();
  }

  function focusHash() {
    var m = /^#item-(\d+)$/.exec(location.hash);
    if (!m) return;
    var el = $('item-' + m[1]);
    if (!el && status !== 'all') {
      setStatus('all').then(focusHash);
      return;
    }
    if (el) {
      el.classList.add('flash');
      el.scrollIntoView({ block: 'start' });
    }
  }

  /* --- events ------------------------------------------------------------ */

  document.querySelector('.filters').addEventListener('click', function (ev) {
    var b = ev.target.closest('button[data-status]');
    if (b) setStatus(b.dataset.status);
  });

  logBox.addEventListener('click', function (ev) {
    var a = ev.target.closest('[data-goto]');
    if (!a) return;
    ev.preventDefault();
    location.hash = 'item-' + a.dataset.goto;
    focusHash();
  });

  queue.addEventListener('change', function (ev) {
    if (ev.target.name !== 'decision') return;
    var form = ev.target.form;
    var it = items[form.dataset.item];
    if (ev.target.value === 'replace_with_approved') loadRenderings(form, it);
    else form.querySelector('.pick').hidden = true;
  });

  queue.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var form = ev.target;
    var msg = form.querySelector('.formmsg');
    var btn = form.querySelector('button[type=submit]');
    var choice = form.querySelector('input[name=decision]:checked');
    var reviewer = (reviewerInput.value || '').trim();
    msg.className = 'formmsg err';
    if (!choice) { msg.textContent = 'اختر قرارًا أولًا.'; return; }
    if (!reviewer) {
      msg.textContent = 'اكتب اسم المراجع في أعلى الصفحة؛ يُسجَّل القرار باسمه.';
      reviewerInput.focus();
      return;
    }
    var body = { decision: choice.value, reviewer: reviewer, note: form.note.value || '' };
    if (choice.value === 'replace_with_approved') {
      var bid = Number(form.querySelector('select[name=book]').value);
      if (!bid) { msg.textContent = 'اختر الترجمة المعتمدة التي يُستبدل بها النص.'; return; }
      body.replacement_book_id = bid;
    }
    btn.disabled = true;
    msg.className = 'formmsg';
    msg.textContent = '…يُسجَّل';
    api('/api/review/' + form.dataset.item + '/decision', { method: 'POST', body: body })
      .then(function (it) {
        items[it.id] = it;
        var el = $('item-' + it.id);
        if (el) {
          el.outerHTML = itemCard(it);
          var fresh = $('item-' + it.id);
          var m = fresh.querySelector('.formmsg');
          m.className = 'formmsg ok';
          m.innerHTML = 'سُجّل القرار: ' + esc(it.latest.decision_label) + ' — <span class="num">' +
                        esc(fmtTime(it.latest.created_at)) + '</span>. يبقى ظاهرًا هنا حتى تحدّث الصفحة.';
        }
        return loadLog().then(function () {
          return api('/api/review?status=all').then(function (d) {
            var c = d.counts || {};
            $('c-open').textContent = c.open || 0;
            $('c-closed').textContent = c.closed || 0;
            $('c-total').textContent = c.total || 0;
            setNavCount(c.open);
          });
        });
      })
      .catch(function (e) {
        btn.disabled = false;
        msg.className = 'formmsg err';
        msg.textContent = e.message;
      });
  });

  window.addEventListener('hashchange', focusHash);

  /* --- boot -------------------------------------------------------------- */

  Promise.all([
    api('/api/languages').then(function (d) {
      (d.languages || []).forEach(function (l) { langDir[l.code] = l.direction; });
    }),
    api('/api/health').then(function (h) {
      if (h.privacy) $('privacy').textContent = h.privacy;
      $('footstats').textContent = 'فهرس ' + h.index_version + ' · ' + h.n_translations +
        ' ترجمة معتمدة بـ ' + h.n_languages + ' لغات';
    })
  ]).catch(function () { /* the queue still loads; errors show there */ })
    .then(function () { return Promise.all([loadQueue(), loadLog()]); })
    .then(focusHash);
})();
