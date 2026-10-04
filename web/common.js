/* MIZAN — helpers shared by index.html and review.html. No framework. */
(function () {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* Only http(s) links from the server are ever rendered as hrefs. */
  function safeUrl(u) {
    return /^(https?:\/\/|\/)/.test(String(u || '')) ? esc(u) : '#';
  }

  /* fetch + JSON + Arabic errors that say what to do. */
  function api(path, opts) {
    opts = opts || {};
    var ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    var ms = opts.timeoutMs || 30000;
    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, ms) : null;
    var init = { method: opts.method || 'GET', headers: {} };
    if (ctrl) init.signal = ctrl.signal;
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(function (r) {
      return r.text().then(function (txt) {
        var data = null;
        try { data = txt ? JSON.parse(txt) : {}; } catch (e) { data = null; }
        if (!r.ok) {
          var msg = data && data.error ? data.error
            : 'ردّ الخادم برمز ' + r.status + '. أعد المحاولة بعد لحظات.';
          var err = new Error(msg);
          err.status = r.status;
          err.code = data && data.code;
          throw err;
        }
        if (data === null) throw new Error('ردّ الخادم بصيغة غير متوقعة. حدّث الصفحة ثم أعد المحاولة.');
        return data;
      });
    }, function (e) {
      if (e && e.name === 'AbortError') {
        throw new Error('انتهت مهلة الانتظار (' + Math.round(ms / 1000) + ' ث). ' +
          'إن كانت الوثيقة طويلة فقسّمها إلى أجزاء، ثم أعد المحاولة.');
      }
      throw new Error('تعذّر الاتصال بالخادم المحلي. تأكد أن python3 app.py يعمل، ثم أعد المحاولة.');
    }).then(function (d) {
      if (timer) clearTimeout(timer);
      return d;
    }, function (e) {
      if (timer) clearTimeout(timer);
      throw e;
    });
  }

  /* Audit timestamps are shown exactly as stored: UTC, to the second. */
  function fmtTime(iso) {
    return String(iso || '').replace('T', ' ').replace('Z', ' UTC');
  }

  /* Shared state vocabulary. Descriptive, never a verdict of "wrong". */
  var STATE_CLS = {
    MATCH: 's-match', NEAR: 's-near', UNATTRIBUTED: 's-none',
    NO_APPROVED_TRANSLATION: 's-nolang', UNRESOLVED: 's-none'
  };

  window.MZ = { esc: esc, safeUrl: safeUrl, api: api, fmtTime: fmtTime, STATE_CLS: STATE_CLS };
})();
