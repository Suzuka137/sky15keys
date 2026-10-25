/* 谱面编辑器（改谱子）
 * ------------------------------------------------------------------
 * 目标：像看图片一样看谱面，点了就能改，改完立刻能听。
 *
 * 三块东西：
 *   左：服务端渲染的谱面图（分页，不是长图）+ 一层 canvas 叠在上面画选中框/播放条
 *   右：15 键面板（点/按字母都能出声）+ 当前格子信息
 *   上：翻页、播放、速度、保存
 *
 * 坐标从哪来：GET /api/edit/<id> 里的 geometry —— 服务端用**和画图同一份**
 * 计算（render.page_geometry）给出的像素坐标，所以点哪儿就是哪儿。
 * 前端只做等比缩放：k = 图片显示宽度 / geometry.w。
 *
 * 键位：
 *   ← →   上/下一列          ↑ ↓   上/下一行（编辑模式下改成 第1/2/3 个音）
 *   Enter 进/出修改这一列     字母   在修改模式下填入音（也可以用鼠标点面板）
 *   Backspace 清掉当前这个音   空格   播放/暂停    Esc 关闭
 */
(function () {
  'use strict';

  var CHARS = 'DRMFSLX12345678';
  var SEMIS = [0, 2, 4, 5, 7, 9, 11];
  var KEY_MIDI = { C: 60, Db: 61, D: 62, Eb: 63, E: 64, F: 65,
                   Gb: 66, G: 67, Ab: 68, A: 69, Bb: 70, B: 71 };
  var KB_ROWS = [['y', 'u', 'i', 'o', 'p'],
                 ['h', 'j', 'k', 'l', ';'],
                 ['n', 'm', ',', '.', '/']];

  function $(s) { return document.querySelector(s); }
  function midiOfIndex(i, base) {
    if (i < 7) return base + SEMIS[i];
    if (i < 14) return base + 12 + SEMIS[i - 7];
    return base + 24;
  }
  function keyOfLetter(k) {
    for (var r = 0; r < 3; r++) {
      var c = KB_ROWS[r].indexOf(k);
      if (c >= 0) return r * 5 + c;
    }
    return -1;
  }
  function charsOf(slot) {
    var out = [];
    for (var i = 0; i < slot.length; i++) if (CHARS.indexOf(slot.charAt(i)) >= 0) out.push(slot.charAt(i));
    return out;
  }

  var ST = null;

  /* ---------------- 状态 ---------------- */
  function blank() {
    return {
      jid: '', title: '', meta: null, spb: 8, slots: [],
      geo: null, page: 1, pages: 1, dpi: 150, rows: 8,
      sel: 0, lane: 0, editing: false, phSlot: 0,
      rev: 0, loadedRev: -1, soundKey: '',
      dirty: false, saveTimer: 0, playing: false,
      player: null, pad: null, el: null, img: null, cv: null, ctx: null
    };
  }

  /* ---------------- HTTP ---------------- */
  function getJSON(url) {
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok || j.ok === false) throw new Error(j.error || ('HTTP ' + r.status));
        return j;
      });
    });
  }
  function postJSON(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok || j.ok === false) throw new Error(j.error || ('HTTP ' + r.status));
        return j;
      });
    });
  }

  /* ---------------- 打开 / 关闭 ---------------- */
  function open(jid, onSaved) {
    // 留着上一个 player 实例：每次开编辑器都新建一个的话，AudioContext 会一直挂着，
    // 浏览器上限就几个，开关几次之后就创建失败、播放没声音（2026-10-25 修）。
    var keepPlayer = (ST && ST.player) || null;
    try { if (keepPlayer) keepPlayer.pause(true); } catch (e) { }
    ST = blank();
    ST.player = keepPlayer;
    ST.jid = jid;
    ST.onSaved = typeof onSaved === 'function' ? onSaved : null;
    ST.saved = false;
    build();
    ST.el.classList.remove('hidden');
    document.body.classList.add('ed-open');
    loadPage(1, { adopt: true }).catch(fail);
  }

  function close() {
    if (!ST) return;
    var already = ST.saved;
    var p = ST.dirty ? save(true) : Promise.resolve();
    try { if (ST.playing) ST.player.pause(); } catch (e) { }
    ST.el.classList.add('hidden');
    document.body.classList.remove('ed-open');
    // 改过就通知外面刷新（主界面的谱面图/播放数据要跟着变）。
    // 必须等存完再通知，不然外面拿到的是改之前的谱。
    p.then(function () {
      if ((ST.saved || already) && ST.onSaved) { try { ST.onSaved(ST.jid); } catch (e) { } }
    }).catch(function () { });
  }

  function fail(e) {
    console.error(e);
    var box = $('#ed-now');
    if (box) box.textContent = '出错了：' + (e && e.message ? e.message : e);
  }

  /* 只往状态栏写一句，不加「出错了：」前缀、不抛 —— 播放这类可以重试的操作用它报原因。 */
  function status(msg) {
    var box = $('#ed-now');
    if (box) box.textContent = msg;
  }

  /* ---------------- DOM ---------------- */
  function build() {
    if ($('#ed')) { ST.el = $('#ed'); wire(); return; }
    var d = document.createElement('div');
    d.id = 'ed';
    d.className = 'ed hidden';
    d.innerHTML = [
      '<div class="ed-top">',
      '  <span class="ed-title" id="ed-title">改谱子</span>',
      '  <span class="ed-sub" id="ed-sub"></span>',
      '  <span class="ed-spacer"></span>',
      '  <button id="ed-play" class="btn small primary">播放</button>',
      '  <button id="ed-home" class="btn small ghost">回到开头</button>',
      '  <label class="ed-lb">速度<input id="ed-tempo" type="number" min="20" max="400"></label>',
      '  <label class="ed-lb">弹奏调性<select id="ed-key" class="tiny"></select></label>',
      '  <span class="ed-dirty" id="ed-dirty"></span>',
      '  <button id="ed-save" class="btn small">保存</button>',
      '  <button id="ed-close" class="btn small ghost">关闭</button>',
      '</div>',
      '<div class="ed-body">',
      '  <div class="ed-left">',
      '    <div class="ed-stage" id="ed-stage">',
      '      <div class="ed-page" id="ed-page"><img id="ed-img" alt="谱面"><canvas id="ed-cv"></canvas></div>',
      '    </div>',
      '    <div class="ed-tools">',
      '      <button id="ed-prev" class="btn small ghost">◀ 上一页</button>',
      '      <span class="ed-pageno" id="ed-pageno">1 / 1</span>',
      '      <button id="ed-next" class="btn small ghost">下一页 ▶</button>',
      '      <span class="ed-sep"></span>',
      '      <button id="ed-ins" class="btn small">插入空拍</button>',
      '      <button id="ed-del" class="btn small">删掉这一拍</button>',
      '    </div>',
      '  </div>',
      '  <div class="ed-right">',
      '    <div id="ed-pad" class="ed-pad"></div>',
      '    <div class="ed-now" id="ed-now">—</div>',
      '    <div class="ed-hint">← → 换列 · ↑ ↓ 换行 · Enter 改这一列（再按 Enter 退出）<br>',
      '      改的时候 ↑ ↓ 换第几个音，按字母或点面板填音，Backspace 清掉 · 空格播放/暂停 · E 红杠跳到选中的那一列</div>',
      '  </div>',
      '</div>'
    ].join('\n');
    document.body.appendChild(d);
    ST.el = d;
    wire();
  }

  var _wired = false;

  // 播放器每次打开编辑器都要有：open() 里 ST = blank() 会把它清成 null，
  // 而下面 wire() 的 _wired 守卫会让第二次以后的调用提前返回 ——
  // 所以创建放在守卫之前，否则第二次打开就报「播放器还没准备好」（2026-10-25 用户报的）。
  function ensurePlayer() {
    if (ST.player || !window.SkyPlayer) return;
    ST.player = new SkyPlayer({
      onTick: function (info) { onTick(info); },
      // 播放器自己发现没法播（比如谱里一个音都没有）时用它报一句
      onWarn: function (m) { status(m); },
      onState: function (on) {
        ST.playing = !!on;
        var b = $('#ed-play');
        if (b) b.textContent = on ? '暂停' : '播放';
        // 暂停不再把选中格挪到红杠上（用户 2026-10-25：选中列别跟着红杠跑）
        paint();
        paintNow();
      }
    });
  }

  function wire() {
    ST.el = $('#ed');
    ST.img = $('#ed-img');
    ST.cv = $('#ed-cv');
    ST.ctx = ST.cv.getContext('2d');
    ensurePlayer();
    // #ed / #ed-cv 就是页面里固定的那几个，每次打开编辑器都会再调一次 wire()。
    // 以前 image.load 与 window.resize 在这里无脑 addEventListener，
    // 每开关一次就多挂一份闭包 —— 改几十次谱子之后每次 resize 要重排几十遍。
    if (_wired) return;
    _wired = true;

    $('#ed-close').onclick = close;
    $('#ed-prev').onclick = function () { gotoPage(ST.page - 1); };
    $('#ed-next').onclick = function () { gotoPage(ST.page + 1); };
    $('#ed-ins').onclick = function () { insertSlots(1); };
    $('#ed-del').onclick = function () { deleteSlots(1); };
    $('#ed-save').onclick = function () { save(); };
    $('#ed-play').onclick = togglePlay;
    $('#ed-home').onclick = function () { seekSlot(0); };
    // 弹奏调性：只改发声，不动谱面
    var ks = $('#ed-key');
    if (ks) {
      ks.innerHTML = '<option value="">跟谱面</option>';
      var keys = Object.keys(KEY_MIDI);
      for (var qi = 0; qi < keys.length; qi++) {
        ks.appendChild(new Option(keys[qi] + ' 调', keys[qi]));
      }
      ks.onchange = function () {
        ST.soundKey = this.value || '';
        ST.rev += 1;                 // 换调要重载发声
        // refreshPlayer() 自己会把播放头放回原处，这里不要再 seek(ST.sel) ——
        // 那会把红杠拉到选中格上，和「红杠只由 E 控制」冲突（2026-10-25 修）。
        refreshPlayer();
      };
    }
    $('#ed-tempo').onchange = function () {
      var v = parseInt(this.value, 10);
      if (!v || v < 20 || v > 400) { this.value = ST.meta.tempo; return; }
      ST.meta.tempo = v;
      markDirty();
      refreshPlayer();
    };

    ST.img.addEventListener('load', function () { layoutCanvas(); paint(); });
    ST.cv.addEventListener('click', onCanvasClick);
    ST.cv.addEventListener('dblclick', onCanvasDblClick);
    window.addEventListener('resize', function () { if (isOpen()) layoutCanvas(); paint(); });

    document.addEventListener('keydown', onKey, true);

    if (!ST.pad && window.SkyPad) {
      ST.pad = new SkyPad($('#ed-pad'), function (i) { padTap(i); });
      ST.pad.bindKeys();
    }
  }

  function isOpen() { return ST && ST.el && !ST.el.classList.contains('hidden'); }

  /* ---------------- 取数据 ---------------- */
  function loadPage(n, opt) {
    opt = opt || {};
    if (!ST.jid) return Promise.reject(new Error('没有任务号'));
    var url = '/api/edit/' + ST.jid + '?page=' + n + '&dpi=' + ST.dpi +
              '&rows=' + ST.rows;
    return getJSON(url).then(function (d) {
      ST.geo = d.geometry;
      ST.page = d.geometry.page;
      ST.pages = d.geometry.pages;
      ST.spb = d.spb || ST.spb;
      ST.rows = d.rows || ST.rows;
      if (opt.adopt || !ST.dirty) {
        ST.slots = d.slots.slice();
        ST.meta = d.meta;
        ST.title = (d.meta && d.meta.title) || '';
        ST.dirty = false;
      }
      $('#ed-title').textContent = '改谱子 · ' + (ST.title || '未命名');
      $('#ed-pageno').textContent = ST.page + ' / ' + ST.pages;
      if ($('#ed-tempo').value !== String(ST.meta.tempo)) $('#ed-tempo').value = ST.meta.tempo;
      clampSel();
      paintDirty();
      paintNow();
      return loadImage();
    });
  }

  function loadImage() {
    var url = '/api/export/' + ST.jid + '?mode=page&page=' + ST.page +
              '&dpi=' + ST.dpi + '&rows=' + ST.rows +
              '&crop=1&t=' + Date.now();   // crop=1：裁掉本页下方空白
    return new Promise(function (res) {
      ST.img.onload = function () { layoutCanvas(); paint(); res(); };
      ST.img.onerror = function () { res(); };
      ST.img.src = url;
    });
  }

  function gotoPage(n) {
    n = Math.max(1, Math.min(ST.pages, n));
    if (n === ST.page) return;
    loadPage(n, { adopt: !ST.dirty }).then(function () {
      // 翻页后只在「选中格不在这页上」时才把选中格挪到本页第一格
      // （播放中/跨页连续按方向键时不能抢走选中，否则红杠跟着跑丢）
      var onPage = colAt(ST.sel);
      if (!onPage) {
        var g = ST.geo;
        if (g && g.rows.length && g.rows[0].cols.length) {
          ST.sel = g.rows[0].cols[0].slot;
        }
      }
      clampSel();
      paint();
    }).catch(fail);
  }

  /* ---------------- 画布 ---------------- */
  function layoutCanvas() {
    var page = $('#ed-page');
    if (!page || !ST.img) return;
    var w = ST.img.clientWidth || 0;
    var h = ST.img.clientHeight || 0;
    if (!w || !h) return;
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    ST.cv.width = Math.round(w * dpr);
    ST.cv.height = Math.round(h * dpr);
    ST.cv.style.width = w + 'px';
    ST.cv.style.height = h + 'px';
    ST.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ST.k = w / ST.geo.w;
  }

  function colAt(slot) {
    var g = ST.geo;
    if (!g) return null;
    for (var i = 0; i < g.rows.length; i++) {
      var r = g.rows[i];
      if (slot < r.slot_from || slot > r.slot_to) continue;
      for (var j = 0; j < r.cols.length; j++) {
        if (r.cols[j].slot === slot) return { row: r, col: r.cols[j] };
      }
    }
    return null;
  }

  function paint() {
    if (!ST || !ST.ctx || !ST.geo) return;
    var c = ST.ctx, k = ST.k || 1;
    c.clearRect(0, 0, ST.cv.width, ST.cv.height);

    // 播放条：只看 ST.phSlot（用户 2026-10-25 定：红杠不被选中列/暂停状态带走，
    // 只有按 E 才跳到选中的那一列）
    var ph = ST.phSlot;
    if (typeof ph === 'number' && ph >= 0) {
      var at = colAt(Math.round(ph));
      if (at) {
        var x = at.col.x * k;
        c.fillStyle = 'rgba(255,59,48,0.85)';
        c.fillRect(x + 1, at.row.bar_top * k - 6, 3, (at.row.bar_bot - at.row.bar_top) * k + 12);
      }
    }

    var hit = colAt(ST.sel);
    if (hit) {
      var cw = hit.col.w * k, ch = (hit.row.bar_bot - hit.row.bar_top) * k;
      var x0 = hit.col.x * k - cw / 2, y0 = hit.row.bar_top * k;
      c.lineWidth = 2;
      c.strokeStyle = ST.editing ? 'rgba(74,144,255,0.95)' : 'rgba(74,144,255,0.55)';
      c.fillStyle = ST.editing ? 'rgba(74,144,255,0.16)' : 'rgba(74,144,255,0.07)';
      roundRect(c, x0, y0, cw, ch, 6);
      c.fill();
      c.stroke();

      // 正在改第几个音：在那条轨下面画一小段横线
      if (ST.editing) {
        var lane = Math.min(2, ST.lane);
        var ly = (hit.col.y[lane] + ST.geo.note_h) * k;
        c.strokeStyle = '#ffd166';
        c.lineWidth = 3;
        c.beginPath();
        c.moveTo(x0 + cw * 0.18, ly + 3);
        c.lineTo(x0 + cw * 0.82, ly + 3);
        c.stroke();
      }
    }
  }

  function roundRect(c, x, y, w, h, r) {
    c.beginPath();
    c.moveTo(x + r, y);
    c.lineTo(x + w - r, y);
    c.quadraticCurveTo(x + w, y, x + w, y + r);
    c.lineTo(x + w, y + h - r);
    c.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    c.lineTo(x + r, y + h);
    c.quadraticCurveTo(x, y + h, x, y + h - r);
    c.lineTo(x, y + r);
    c.quadraticCurveTo(x, y, x + r, y);
    c.closePath();
  }

  /* ---------------- 鼠标 ---------------- */
  function hitSlot(ev) {
    if (!ST.geo) return -1;
    var rect = ST.cv.getBoundingClientRect();
    var x = (ev.clientX - rect.left) / (ST.k || 1);
    var y = (ev.clientY - rect.top) / (ST.k || 1);
    var g = ST.geo;
    for (var i = 0; i < g.rows.length; i++) {
      var r = g.rows[i];
      if (y < r.bar_top - 8 || y > r.bar_bot + 8) continue;
      for (var j = 0; j < r.cols.length; j++) {
        var col = r.cols[j];
        if (Math.abs(x - col.x) <= col.w / 2) return col.slot;
      }
    }
    return -1;
  }

  function onCanvasClick(ev) {
    var s = hitSlot(ev);
    if (s < 0) return;
    ST.sel = s;
    ST.editing = false;
    ST.lane = 0;
    // 点哪一格只改「选中」，不动红杠（红杠按 E 才跳）
    paint();
    paintNow();
  }

  function onCanvasDblClick(ev) {
    var s = hitSlot(ev);
    if (s < 0) return;
    ST.sel = s;
    ST.editing = true;
    ST.lane = 0;
    paint();
    paintNow();
  }

  /* ---------------- 键盘 ---------------- */
  function onKey(ev) {
    if (!isOpen()) return;
    var tag = (ev.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'select' || tag === 'textarea') {
      if (ev.key === 'Escape') ev.target.blur();
      return;
    }
    var k = ev.key;
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    // 吃的键一律拦下来：不然 pad.js 的 window 监听还会再响一次
    function stop() { ev.preventDefault(); ev.stopPropagation(); }

    if (k === 'Escape') {
      stop();
      if (ST.editing) { ST.editing = false; paint(); paintNow(); }
      else close();
      return;
    }
    if (k === ' ') { stop(); togglePlay(); return; }
    // E：把红杠挪到选中的那一列（播放中也生效，用户 2026-10-25 定的唯一跳转键）
    if (k === 'e' || k === 'E') { stop(); seekSlot(ST.sel); return; }

    if (k === 'ArrowLeft') { stop(); move(-1); return; }
    if (k === 'ArrowRight') { stop(); move(1); return; }
    if (k === 'ArrowUp') {
      stop();
      if (ST.editing) { ST.lane = Math.max(0, ST.lane - 1); paint(); paintNow(); }
      else move(-perRowSlots());
      return;
    }
    if (k === 'ArrowDown') {
      stop();
      if (ST.editing) { ST.lane = Math.min(2, ST.lane + 1); paint(); paintNow(); }
      else move(perRowSlots());
      return;
    }
    if (k === 'Enter') {
      stop();
      if (!ST.editing) { ST.editing = true; ST.lane = 0; }
      else { ST.editing = false; }
      paint();
      paintNow();
      return;
    }
    if (k === 'Backspace' || k === 'Delete') {
      stop();
      if (ST.editing) clearLane();
      return;
    }
    var letter = (k || '').toLowerCase();
    var idx = keyOfLetter(letter);
    if (idx >= 0) {
      stop();
      padTap(idx);
    }
  }

  function perRowSlots() {
    // 一行固定几格由排版决定（默认一行 4 小节 × 每小节 8 格 = 32），
    // 不能拿「这一行实际画了几格」来算 —— 短谱会把整行算成 2 格
    var g = ST.geo || {};
    var perRow = g.per_row || 4;
    var perBar = g.per_bar || ST.spb || 8;
    return perRow * perBar;
  }

  function move(d) {
    ST.sel = Math.max(0, Math.min(ST.slots.length - 1, ST.sel + d));
    if (ST.geo) {
      var at = colAt(ST.sel);
      if (!at) {
        // 跨页了：跳到那一格所在的页
        var target = pageOfSlot(ST.sel);
        if (target !== ST.page) { gotoPage(target); return; }
      }
    }
    paint();
    paintNow();
  }

  function pageOfSlot(slot) {
    var pm = (ST.geo && ST.geo.pages_map) || [];
    for (var i = 0; i < pm.length; i++) {
      if (slot >= pm[i][0] && slot <= pm[i][1]) return i + 1;
    }
    return ST.page;
  }

  function clampSel() {
    if (!ST.slots.length) { ST.sel = 0; return; }
    ST.sel = Math.max(0, Math.min(ST.slots.length - 1, ST.sel));
  }

  /* ---------------- 编辑 ---------------- */
  function setLane(ch) {
    var s = ST.sel;
    var cs = charsOf(ST.slots[s] || '');
    while (cs.length < 3) cs.push('');
    // 同一个音不能占两条轨：先把别的轨上的同一个音清掉，这样重复调用也不会抖
    for (var i = 0; i < 3; i++) if (i !== ST.lane && cs[i] === ch) cs[i] = '';
    cs[ST.lane] = ch;
    var out = cs.filter(function (c) { return c && CHARS.indexOf(c) >= 0; });
    out.sort(function (a, b) { return CHARS.indexOf(b) - CHARS.indexOf(a); });
    ST.slots[s] = ((out.join('') + '000').slice(0, 3));
    markDirty();
    paint();
    paintNow();
  }

  function clearLane() {
    var s = ST.sel;
    var cs = charsOf(ST.slots[s] || '');
    if (!cs.length) return;
    cs.splice(Math.min(ST.lane, cs.length - 1), 1);
    ST.slots[s] = ((cs.join('') + '000').slice(0, 3));
    markDirty();
    paint();
    paintNow();
  }

  function padTap(i) {
    try { ST.player.playMidi(midiOfIndex(i, soundBase())); } catch (e) { }
    try { ST.pad.flash(i); } catch (e) { }
    if (ST.editing) setLane(CHARS.charAt(i));
  }

  function soundBase() {
    // 弹奏调性只改发声，不改谱面（和主界面的「弹奏调性」一致）
    var k = ST.soundKey || (ST.meta && ST.meta.key) || 'C';
    var b = KEY_MIDI[k];
    if (b == null) b = 60;
    return (ST.meta && ST.meta.has8vb) ? b - 12 : b;
  }

  /* ---------------- 增删 ---------------- */
  // n=1 是「这一拍」，n=perRow 是「这一行」；整行时按行边界对齐，
  // 不然在行中间点「删掉这一行」会把下一行的开头也吃掉
  function insertSlots(n) {
    n = Math.max(1, n | 0);
    var at = n > 1 ? Math.floor(ST.sel / n) * n : ST.sel;
    var add = [];
    for (var i = 0; i < n; i++) add.push('000');
    ST.slots.splice.apply(ST.slots, [at, 0].concat(add));
    ST.sel = at;
    markDirty();
    save(true).then(function () {
      ST.sel = Math.min(ST.sel, ST.slots.length - 1);
      return loadPage(pageOfSlot(ST.sel), { adopt: true });
    }).catch(fail);
  }

  function deleteSlots(n) {
    n = Math.max(1, n | 0);
    if (ST.slots.length <= 1) return;
    var at = n > 1 ? Math.floor(ST.sel / n) * n : ST.sel;
    var cnt = Math.min(n, ST.slots.length - at);
    if (cnt <= 0) return;
    if (ST.slots.length - cnt < 1) cnt = ST.slots.length - 1;   // 至少留一格
    ST.slots.splice(at, cnt);
    if (!ST.slots.length) ST.slots = ['000'];
    ST.sel = Math.min(at, ST.slots.length - 1);
    clampSel();
    markDirty();
    save(true).then(function () {
      return loadPage(pageOfSlot(ST.sel), { adopt: true });
    }).catch(fail);
  }

  /* ---------------- 保存 ---------------- */
  function markDirty() {
    if (!ST) return;
    ST.dirty = true;
    ST.rev += 1;          // 谱面变了：让 refreshPlayer 知道要重新载入
    paintDirty();
    clearTimeout(ST.saveTimer);
    ST.saveTimer = setTimeout(function () { save(); }, 1200);
  }

  function paintDirty() {
    var b = $('#ed-dirty');
    if (b) b.textContent = ST.dirty ? '未保存' : '已保存';
    if (b) b.className = 'ed-dirty' + (ST.dirty ? ' warn' : '');
  }

  function save(quiet) {
    if (!ST || !ST.dirty) return Promise.resolve();
    clearTimeout(ST.saveTimer);
    var body = {
      slots: ST.slots, spb: ST.spb,
      meta: {
        tempo: ST.meta.tempo, time_sig: ST.meta.time_sig,
        key: ST.meta.key, has8vb: ST.meta.has8vb,
        title: ST.meta.title, difficulty: ST.meta.difficulty
      }
    };
    var before = ST.slots.length;
    return postJSON('/api/edit/' + ST.jid, body).then(function (r) {
      ST.dirty = false;
      ST.saved = true;
      paintDirty();
      if (!quiet) toast('已保存');
      // 长度变了 → 页数/排版可能变，重新取一页坐标和图
      if (r && r.stats && r.stats.slots !== before) {
        return loadPage(pageOfSlot(ST.sel), { adopt: true });
      }
      // 只改了音高/删了音：谱面图必须重画，不然用户盯着图看会觉得"按了没反应"
      scheduleImgRefresh();
    }).catch(function (e) { fail(e); });
  }

  // 改完音之后的图刷新：合并连续按键，避免每按一个字都去渲染一次
  function scheduleImgRefresh() {
    if (ST.imgTimer) clearTimeout(ST.imgTimer);
    ST.imgTimer = setTimeout(function () {
      ST.imgTimer = 0;
      if (!isOpen() || !ST.geo) return;
      loadImage();
    }, 220);
  }

  function toast(msg) {
    var b = $('#ed-dirty');
    if (!b) return;
    b.textContent = msg;
  }

  /* ---------------- 播放 ---------------- */
  // 只有谱面真的变了（rev 变大）才重新 load：
  // load() 会把播放头重置到第 0 格，所以「点暂停再点播放会回到开头」就是这么来的。
  function refreshPlayer() {
    if (!ST.player) return;
    if (ST.loadedRev === ST.rev) return;
    var base = soundBase();
    // load() 里面会 onTick(info(0))，把 ST.phSlot 冲成 0 —— 先存下来，load 完再补回去。
    // 不存的话「改完音符点继续播放」会从开头放（2026-10-25 用户报的）。
    var keep = ST.phSlot;
    ST.player.load({
      slots: ST.slots.map(function (s) {
        return charsOf(s).map(function (c) { return midiOfIndex(CHARS.indexOf(c), base); });
      }),
      idxSlots: ST.slots.map(function (s) {
        return charsOf(s).map(function (c) { return CHARS.indexOf(c); });
      }),
      perBar: ST.spb,
      beatsPerBar: Math.max(1, Math.round(ST.spb / 2)),
      tempo: ST.meta.tempo || 120
    });
    ST.loadedRev = ST.rev;
    ST.phSlot = keep;
    // 重新载入后把播放头放回原来的位置（不是选中格）——
    // 用户 2026-10-25 定：只有按 E 才跳到选中列，其它情况播放头自己待着。
    try { ST.player.seek(keep); } catch (e) { }
    try { ST.player.preload(padMidis(base)); } catch (e) { }
  }

  function padMidis(base) {
    var list = [];
    for (var i = 0; i < 15; i++) list.push(midiOfIndex(i, base));
    return list;
  }

  function togglePlay() {
    if (!ST.player) { status('播放器还没准备好，等一下再点。'); return; }
    var was = ST.playing;
    /* 这一串任何一处抛（音频设备被浏览器回收、谱面数据异常），异常都会冒到
       事件处理器里被浏览器吞掉，用户看到的就是「按空格、点播放完全没反应」。
       兜住它，至少把原因写到状态栏（2026-10-25 用户报的）。 */
    try {
      refreshPlayer();
      if (!was) {
        // 续播：从暂停时停下的那一格接着放；想从选中列开始要按 E（用户 2026-10-25）
        try { ST.player.seek(ST.phSlot); } catch (e) { }
      }
      ST.player.toggle();
    } catch (e) {
      try { console.error(e); } catch (e2) { }
      status('播放出错了：' + ((e && e.message) ? e.message : e)
        + '　点一下页面再试，还不行就刷新一下。');
    }
  }

  function seekSlot(s) {
    if (!ST.player) return;
    refreshPlayer();
    ST.player.seek(Math.max(0, Math.min(ST.slots.length - 1, s)));
    ST.phSlot = s;
    paint();
  }

  function onTick(info) {
    ST.phSlot = info.slot;
    if (ST.pad && info.idx) ST.pad.highlight(info.idx);
    // 播到别的页就自动翻过去
    var pg = pageOfSlot(info.slot);
    if (info.playing && pg !== ST.page) {
      gotoPage(pg);
      return;
    }
    paint();
    paintNow();
  }

  function paintNow() {
    var box = $('#ed-now');
    if (!box) return;
    var s = ST.sel;
    var at = colAt(s);
    var bar = Math.floor(s / ST.spb) + 1;
    var beat = (s % ST.spb) + 1;
    var cs = charsOf(ST.slots[s] || '');
    var labels = cs.map(function (c) {
      var i = CHARS.indexOf(c);
      var lab = window.SkyPadLabel ? window.SkyPadLabel(i) : { deg: i + 1, dots: 0 };
      return new Array(lab.dots + 1).join('·') + lab.deg;
    });
    box.innerHTML = '第 <b>' + (s + 1) + '</b> 格 · 第 ' + bar + ' 小节 第 ' + beat +
      ' 拍 · 音 ' + (labels.length ? labels.join(' ') : '（空）') +
      (ST.editing ? ' · <span class="ed-editing">正在改第 ' + (ST.lane + 1) + ' 个音</span>' : '');
  }

  window.SkyEditor = { open: open, close: close };
  // 调试/自动化用：看一眼当前状态（不影响正常使用）
  window.SkyEditor.state = function () {
    if (!ST) return null;
    return { jid: ST.jid, page: ST.page, pages: ST.pages, spb: ST.spb,
             sel: ST.sel, lane: ST.lane, editing: ST.editing, dirty: ST.dirty,
             phSlot: ST.phSlot, playing: !!ST.playing, rev: ST.rev,
             rows: ST.rows, soundKey: ST.soundKey,
             slots: ST.slots.slice(), meta: ST.meta ? Object.assign({}, ST.meta) : null };
  };
})();
