/* sky15 · 界面逻辑
   结构：偏好 → 状态 → 上传/排队 → 轮询 → 结果渲染 → 播放/键位 → 调参重算 → 导出 → 历史。
   没有框架，全原生，方便 pythonw 双击就能跑，不依赖任何网络资源。 */
(function () {
  'use strict';

  /* ================= 常量 ================= */
  var KEYS = ['C', 'Db', 'D', 'Eb', 'E', 'F', 'Gb', 'G', 'Ab', 'A', 'Bb', 'B'];
  var KEY_LABEL = {
    C: 'C 调', Db: 'D♭ 调', D: 'D 调', Eb: 'E♭ 调', E: 'E 调', F: 'F 调',
    Gb: 'G♭ 调', G: 'G 调', Ab: 'A♭ 调', A: 'A 调', Bb: 'B♭ 调', B: 'B 调'
  };
  var KEY_MIDI = { C: 60, Db: 61, D: 62, Eb: 63, E: 64, F: 65, Gb: 66, G: 67, Ab: 68, A: 69, Bb: 70, B: 71 };
  var CHARS = 'DRMFSLX12345678';
  var SEMIS = [0, 2, 4, 5, 7, 9, 11];
  var KB_ROWS = [['y', 'u', 'i', 'o', 'p'], ['h', 'j', 'k', 'l', ';'], ['n', 'm', ',', '.', '/']];
  var POLL_MS = 400;
  var LS_PREFS = 'sky15.prefs';
  var LS_LAST = 'sky15.last';
  var FS_STEPS = [0.85, 0.95, 1, 1.12, 1.28, 1.45];

  /* ================= 工具 ================= */
  function $(s, r) { return (r || document).querySelector(s); }
  function $$(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }
  function mk(tag, cls, txt) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (txt != null) n.textContent = txt;
    return n;
  }
  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }
  function fmtTime(sec) {
    sec = Math.max(0, sec || 0);
    var m = Math.floor(sec / 60);
    var s = Math.floor(sec % 60);
    return m + ':' + (s < 10 ? '0' : '') + s;
  }
  function debounce(fn, ms) {
    var t = 0;
    return function () {
      var a = arguments, self = this;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(self, a); }, ms);
    };
  }
  function readPrefs() {
    var p = {};
    try { p = JSON.parse(localStorage.getItem(LS_PREFS) || '{}') || {}; } catch (e) { p = {}; }
    return p;
  }

  /* 统一请求：非 2xx 抛出带 advice 的错误 */
  function api(url, opt) {
    return fetch(url, opt || {}).then(function (r) {
      var ct = r.headers.get('Content-Type') || '';
      if (ct.indexOf('application/json') < 0) {
        if (!r.ok) throw { message: '服务返回了 ' + r.status + '，不是正常的接口响应。', advice: [] };
        return r.text();
      }
      return r.json().then(function (j) {
        if (!r.ok || j.ok === false) {
          throw {
            message: j.error || ('请求失败（' + r.status + '）'),
            advice: j.advice || [],
            code: j.code || ''
          };
        }
        return j;
      });
    }, function () {
      throw { message: '连不上本地服务，界面可能是自己一个人在这儿。', advice: ['确认那个黑色的 python 窗口还开着；关掉了就再双击「启动界面.bat」。'] };
    });
  }

  var toastTimer = 0;
  function toast(msg, kind) {
    var t = $('#toast');
    t.textContent = msg;
    t.className = 'toast show' + (kind ? ' ' + kind : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.className = 'toast'; }, kind === 'err' ? 5200 : 2600);
  }

  /* ================= 偏好 ================= */
  var prefs = {
    theme: 'dark', fsIdx: 2, panelOpen: true,
    expTheme: 'dark', expDpi: '150', padFocus: false, rate: 1, metro: false
  };
  (function () {
    var p = readPrefs();
    var hadPanel = ('panelOpen' in p);
    for (var k in prefs) if (k in p) prefs[k] = p[k];
    if (prefs.fsIdx == null || prefs.fsIdx < 0 || prefs.fsIdx >= FS_STEPS.length) prefs.fsIdx = 2;
    // 手机/窄屏第一次进来，设置面板默认收起，先看见结果和键位
    if (!hadPanel && window.innerWidth <= 720) prefs.panelOpen = false;
  })();
  function savePrefs() {
    try { localStorage.setItem(LS_PREFS, JSON.stringify(prefs)); } catch (e) { }
  }
  function applyTheme() {
    document.documentElement.setAttribute('data-theme', prefs.theme);
    $('#btn-theme').textContent = prefs.theme === 'dark' ? '浅色' : '深色';
  }
  function applyFs() {
    document.documentElement.style.setProperty('--fs', String(FS_STEPS[prefs.fsIdx]));
  }

  /* ================= 状态 ================= */
  var S = {
    job: null,        // 当前展示的 job 快照
    score: null,      // 解析后的谱面
    jobs: {},         // id -> job 快照（含伪造的上传中条目）
    order: [],        // 卡片顺序
    poll: 0,
    page: 1,
    pages: 1,
    busy: false,
    draggingProg: false
  };

  /* ================= 播放器 / 键位 ================= */
  var player = new SkyPlayer({
    onTick: function (info) { onTick(info); },
    onState: function (playing) {
      $('#transport').classList.toggle('playing', !!playing);
      $('#btn-play').setAttribute('aria-label', playing ? '暂停' : '播放');
      if (!playing) pad.clearHighlight();
    }
  });
  var pad = new SkyPad($('#pad-wrap'), function (i) { tapKey(i); });

  function baseMidi() {
    var k = (S.score && S.score.key) || 'C';
    var b = KEY_MIDI[k] == null ? 60 : KEY_MIDI[k];
    if (S.score && S.score.has8vb) b -= 12;
    return b;
  }
  function midiOfIndex(i, base) {
    if (i < 7) return base + SEMIS[i];
    if (i < 14) return base + 12 + SEMIS[i - 7];
    return base + 24;
  }
  function idxOfMidi(midi, base) {
    var rel = midi - base;
    while (rel < 0) rel += 12;
    while (rel >= 36) rel -= 12;
    var oct = Math.floor(rel / 12);
    var pc = rel - oct * 12;
    var best = 0, bd = 99;
    for (var i = 0; i < 7; i++) {
      var d = Math.abs(SEMIS[i] - pc);
      if (d < bd) { bd = d; best = i; }
    }
    if (oct >= 2) return best === 0 ? 14 : 7 + best;
    return oct * 7 + best;
  }
  function tapKey(i) {
    player.playMidi(midiOfIndex(i, baseMidi()));
  }
  function onTick(info) {
    if (!info) return;
    var total = Math.max(0, (info.total || 0) - 1);
    var prog = $('#prog');
    prog.max = String(total);
    if (!S.draggingProg) prog.value = String(info.slot);
    var sd = player.slotDur();
    var dur = player.slots.length * (60 / Math.max(20, player.tempo) / 2);
    $('#prog-label').textContent = fmtTime(info.slot * sd) + ' / ' + fmtTime(dur);
    prog.style.setProperty('--p', (total > 0 ? (info.slot / total) * 100 : 0) + '%');
    $('#bar-now').textContent = '第 ' + (info.bar + 1) + ' / ' + info.bars + ' 小节';
    highlightBar(info.bar);
    var idx = [];
    (info.notes || []).forEach(function (m) { idx.push(idxOfMidi(m, baseMidi())); });
    pad.highlight(idx);
  }

  function highlightBar(bar) {
    var cells = $$('#bar-ruler .bar-cell');
    for (var i = 0; i < cells.length; i++) {
      cells[i].classList.toggle('now', i === bar);
    }
    var cur = cells[bar];
    if (cur && S.score) {
      var ruler = $('#bar-ruler');
      var left = cur.offsetLeft - ruler.clientWidth / 2 + cur.offsetWidth / 2;
      if (Math.abs(ruler.scrollLeft - left) > ruler.clientWidth * 0.6) {
        ruler.scrollTo({ left: Math.max(0, left), behavior: 'smooth' });
      }
    }
  }

  /* ================= 谱面解析 ================= */
  function field(text, name) {
    var re = new RegExp(name + ':\\s*[\\[［]([^\\]］]*)[\\]］]');
    var m = text.match(re);
    return m ? m[1].trim() : '';
  }

  function parseSmc(text) {
    var keyRaw = field(text, '调性') || 'C';
    var km = keyRaw.match(/^[A-G][#b♯♭]?/);
    var key = km ? km[0].replace('♯', '#').replace('♭', 'b') : 'C';
    var ts = field(text, '节拍') || '4/4';
    var tm = ts.match(/(\d+)\s*\/\s*(\d+)/);
    var num = tm ? parseInt(tm[1], 10) : 4;
    var den = tm ? parseInt(tm[2], 10) : 4;
    var perBar = Math.max(1, Math.round(num * 8 / den));
    return {
      title: field(text, '曲名') || '未命名',
      tempo: parseInt(field(text, '速度'), 10) || 120,
      keyRaw: keyRaw,
      key: key,
      has8vb: /8vb/i.test(keyRaw),
      timeSig: ts,
      beatsPerBar: num,
      perBar: perBar,
      difficulty: field(text, '难度'),
      groups: (field(text, '曲谱') || '').split(/\s+/).filter(Boolean)
    };
  }

  function buildScore(p) {
    var base = KEY_MIDI[p.key] == null ? 60 : KEY_MIDI[p.key];
    if (p.has8vb) base -= 12;
    var slots = p.groups.map(function (g) {
      var out = [];
      for (var k = 0; k < 4; k++) {
        var ch = g.charAt(k);
        if (!ch) continue;
        var i = CHARS.indexOf(ch);
        if (i < 0) continue;
        out.push({ ch: ch, idx: i, midi: midiOfIndex(i, base) });
      }
      return out;
    });
    var used = {};
    slots.forEach(function (s) {
      s.forEach(function (n) { used[idxOfMidi(n.midi, base)] = 1; });
    });
    return {
      meta: p, base: base, slots: slots, used: Object.keys(used).map(Number).sort(function (a, b) { return a - b; }),
      bars: Math.max(1, Math.ceil(slots.length / p.perBar))
    };
  }

  /* ================= 上传 / 队列 ================= */
  var seq = 0;
  function newSeq() { seq += 1; return 'up' + seq; }

  function addFiles(files) {
    var list = Array.prototype.slice.call(files || []);
    if (!list.length) return;
    list.forEach(function (f, i) {
      var id = newSeq();
      S.jobs[id] = {
        id: id, name: f.name || '剪贴板图片', status: 'uploading', upload: 0,
        progress: 0, stage: '上传中', stage_index: 0, stages: [], size: f.size, seq: i
      };
      S.order.push(id);
      paintJobs();
      uploadOne(id, f);
    });
    showResultArea(true);
  }

  function uploadOne(localId, file) {
    var q = new URLSearchParams();
    q.set('name', file.name || 'paste.png');
    var t = $('#opt-tempo').value.trim();
    if (t) q.set('tempo', t);
    var k = $('#opt-key').value;
    if (k) q.set('key', k);
    var b = $('#opt-backend').value;
    if (b) q.set('backend', b);
    q.set('chords', $('#opt-chords').value);
    q.set('harmony', $('#opt-chords').value === 'off' ? '0' : '1');
    q.set('max_notes', $('#opt-maxnotes').value || '3');
    var rb = $('#opt-rubato');
    if (rb && rb.value) q.set('rubato', rb.value);
    saveUploadChoice();

    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/convert?' + q.toString(), true);
    xhr.responseType = 'text';
    xhr.upload.onprogress = function (ev) {
      if (!ev.lengthComputable) return;
      var j = S.jobs[localId];
      if (!j) return;
      j.upload = ev.loaded / ev.total;
      paintJobs();
    };
    xhr.onload = function () {
      var j = S.jobs[localId];
      var data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
      if (xhr.status >= 200 && xhr.status < 300 && data && data.job) {
        delete S.jobs[localId];
        S.order[S.order.indexOf(localId)] = data.job;
        S.jobs[data.job] = {
          id: data.job, name: data.name || file.name, kind: data.kind,
          status: 'queued', stage: '排队中', progress: 0, stage_index: 0,
          stages: [], queue_pos: data.queue_pos || 1
        };
        paintJobs();
        startPoll();
      } else {
        if (j) {
          j.status = 'error';
          j.stage = '上传失败';
          j.error = (data && data.error) || ('上传失败（' + xhr.status + '）');
          j.advice = (data && data.advice) || [];
        }
        paintJobs();
      }
    };
    xhr.onerror = function () {
      var j = S.jobs[localId];
      if (j) { j.status = 'error'; j.stage = '上传失败'; j.error = '上传的时候连接断了。'; j.advice = ['确认服务窗口还在，再试一次。']; }
      paintJobs();
    };
    xhr.send(file);
  }

  function saveUploadChoice() {
    prefs.upTempo = $('#opt-tempo').value || '';
    prefs.upKey = $('#opt-key').value || '';
    prefs.upBackend = $('#opt-backend').value || '';
    prefs.upChords = $('#opt-chords').value || 'auto';
    prefs.upMax = $('#opt-maxnotes').value || '3';
    prefs.upRubato = ($('#opt-rubato') || {}).value || 'auto';
    savePrefs();
  }
  function restoreUploadChoice() {
    if (prefs.upTempo) $('#opt-tempo').value = prefs.upTempo;
    if (prefs.upKey) $('#opt-key').value = prefs.upKey;
    if (prefs.upBackend) $('#opt-backend').value = prefs.upBackend;
    if (prefs.upChords) $('#opt-chords').value = prefs.upChords;
    if (prefs.upMax) $('#opt-maxnotes').value = prefs.upMax;
    if (prefs.upRubato && $('#opt-rubato')) $('#opt-rubato').value = prefs.upRubato;
    updatePanelSummary();
  }

  /* ================= 轮询 ================= */
  function startPoll() {
    if (S.poll) return;
    S.poll = setInterval(pollOnce, POLL_MS);
    pollOnce();
  }
  function stopPoll() {
    if (S.poll) { clearInterval(S.poll); S.poll = 0; }
  }
  function activeIds() {
    return S.order.filter(function (id) {
      var j = S.jobs[id];
      return j && ['queued', 'running', 'uploading'].indexOf(j.status) >= 0;
    });
  }
  function pollOnce() {
    var ids = activeIds().filter(function (id) { return S.jobs[id].status !== 'uploading'; });
    if (!ids.length) { stopPoll(); return; }
    ids.forEach(function (id) {
      api('/api/job/' + id).then(function (job) {
        var was = S.jobs[id] || {};
        var merged = job;
        merged.seq = was.seq;
        S.jobs[id] = merged;
        paintJobs();
        if (job.status === 'done') maybeOpen(job);
        if (job.status === 'error' || job.status === 'cancelled') { /* 卡片自带提示 */ }
        if (!activeIds().length) stopPoll();
      }).catch(function (e) {
        var j = S.jobs[id];
        // 服务重启过之后，历史里的老任务 id 已经不存在了：这不是错误，标成"已失效"就停。
        var gone = /404/.test(e.message || '') || e.code === 'not_found';
        if (j) {
          j.status = 'error';
          j.error = gone ? '这个任务已经被服务清掉了（重启过服务）。' : e.message;
          j.advice = gone ? ['把文件重新拖进来转一次就行。'] : (e.advice || []);
          if (gone) j.stale = true;
        }
        paintJobs();
        if (!activeIds().length) stopPoll();
      });
    });
  }

  function maybeOpen(job) {
    if (!S.job) { openJob(job.id, true); return; }
    if (S.job.id === job.id) { renderResult(job); return; }
    toast('转好了：' + (job.result && job.result.title ? job.result.title : job.name));
  }

  /* ================= 任务卡 ================= */
  function paintJobs() {
    var host = $('#jobs');
    var ids = S.order.filter(function (id) { return S.jobs[id]; });
    // 清掉消失的
    $$('.job', host).forEach(function (n) {
      if (ids.indexOf(n.getAttribute('data-id')) < 0) n.remove();
    });
    ids.forEach(function (id, i) {
      var j = S.jobs[id];
      var card = host.querySelector('.job[data-id="' + id + '"]');
      if (!card) {
        card = mk('div', 'job');
        card.setAttribute('data-id', id);
        card.innerHTML =
          '<div class="job-top">' +
          '<span class="job-name"></span>' +
          '<span class="job-pct"></span>' +
          '<button class="job-x" title="取消这个任务">✕</button>' +
          '</div>' +
          '<div class="steps"></div>' +
          '<div class="bar"><i></i></div>' +
          '<div class="job-note"></div>' +
          '<div class="advice-slot"></div>';
        card.querySelector('.job-x').addEventListener('click', function () { cancelJob(id); });
        host.appendChild(card);
      }
      updateJobCard(card, j);
    });
  }

  function updateJobCard(card, j) {
    card.className = 'job' + (j.status === 'error' ? ' err' : j.status === 'done' ? ' done' : '');
    var name = card.querySelector('.job-name');
    name.textContent = (j.result && j.result.title) || j.name || j.id;
    var pct = card.querySelector('.job-pct');
    var pr = j.status === 'uploading' ? (j.upload || 0) : (j.progress || 0);
    var done = j.status === 'done';
    var stopping = j.status === 'running' && /停下/.test(j.stage || '');
    pct.textContent = j.status === 'uploading'
      ? '上传 ' + Math.round(pr * 100) + '%'
      : done ? '完成'
        : j.status === 'cancelled' ? '已取消'
          : j.status === 'error' ? '出错'
            : stopping ? '正在停下'
              : Math.round(pr * 100) + '%';
    card.classList.toggle('stopping', !!stopping);

    // 步骤条
    var steps = card.querySelector('.steps');
    if (j.stages && j.stages.length) {
      var key = (j.stages.map(function (s) { return s.key + s.label; }).join('|')) + '#' + j.stage_index + '#' + j.status;
      if (steps.getAttribute('data-sig') !== key) {
        steps.setAttribute('data-sig', key);
        steps.innerHTML = '';
        j.stages.forEach(function (s, i) {
          if (i) steps.appendChild(mk('span', 'step-sep'));
          var st = mk('span', 'step');
          var dot = mk('span', 'step-dot');
          var isDone = done || i < (j.stage_index || 0);
          var isNow = !done && i === (j.stage_index || 0) && (j.status === 'running');
          dot.textContent = isDone ? '✓' : String(i + 1);
          st.appendChild(dot);
          st.appendChild(document.createTextNode(s.label));
          st.classList.add(isDone ? 'done' : isNow ? 'now' : 'idle');
          steps.appendChild(st);
        });
      }
    } else if (steps.innerHTML === '') {
      steps.innerHTML = '<span class="step now"><span class="step-dot">·</span>' +
        (j.status === 'uploading' ? '正在上传' : '等待服务开始') + '</span>';
    }

    var fill = card.querySelector('.bar > i');
    fill.style.width = Math.round((done ? 1 : pr) * 100) + '%';

    var note = card.querySelector('.job-note');
    var bits = [];
    if (j.status === 'running') bits.push(j.stage_hint || j.stage || '');
    else if (j.status === 'queued') bits.push('排队第 ' + (j.queue_pos || 1) + ' 位');
    else if (j.status === 'uploading') bits.push(fmtSize(j.size));
    else if (j.status === 'cancelled') bits.push('已取消，不会拖慢别的任务');
    if (j.elapsed) bits.push('已用 ' + Math.round(j.elapsed) + ' 秒');
    if (j.quality && j.quality.ok) bits.push('录音 ' + j.quality.score + '/100');
    if (j.status === 'done' && j.result) {
      bits.push((j.result.notes || 0) + ' 个音 / ' + (j.result.bars || 0) + ' 小节');
      if (j.result.backend) bits.push(backendName(j.result.backend));
    }
    note.textContent = bits.filter(Boolean).join(' · ');

    var slot = card.querySelector('.advice-slot');
    if (j.status === 'error' || (j.error && j.status !== 'done')) {
      slot.innerHTML = '';
      slot.appendChild(adviceBox(j.error || '出错了', j.advice, true));
    } else if (j.status === 'done' && S.job && S.job.id !== j.id) {
      if (!slot.querySelector('.open-btn')) {
        slot.innerHTML = '';
        var b = mk('button', 'btn small primary open-btn', '打开这个结果');
        b.addEventListener('click', function () { openJob(j.id); });
        slot.appendChild(b);
      }
    } else {
      slot.innerHTML = '';
    }
    var x = card.querySelector('.job-x');
    x.textContent = j.status === 'done' ? '✕' : '✕';
    x.title = j.status === 'done' ? '从列表里移除' : '取消这个任务';
  }

  function fmtSize(n) {
    if (!n) return '';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }

  function backendName(b) {
    if (!b) return '';
    if (b.indexOf('librosa') === 0) return '快速旋律';
    if (b.indexOf('transkun') === 0) return 'Transkun 2.0';
    if (b.indexOf('piano') === 0) return '钢琴模型';
    if (b.indexOf('basic') === 0) return '基础模型';
    return b;
  }

  function cancelJob(id) {
    var j = S.jobs[id];
    if (!j) return;
    if (j.status === 'uploading') {  // 还在传，直接丢掉卡片
      delete S.jobs[id];
      S.order = S.order.filter(function (x) { return x !== id; });
      paintJobs();
      return;
    }
    if (j.status === 'done') {
      delete S.jobs[id];
      S.order = S.order.filter(function (x) { return x !== id; });
      paintJobs();
      return;
    }
    api('/api/cancel/' + id, { method: 'POST' }).then(function (r) {
      if (r.job) S.jobs[id] = r.job;
      paintJobs();
      toast('已请求取消：跑到一半的那一步算完就停');
    }).catch(function (e) { toast(e.message, 'err'); });
  }

  /* ================= 建议框 ================= */
  function adviceBox(msg, list, isErr) {
    var box = mk('div', 'advice' + (isErr ? ' err' : ''));
    box.appendChild(mk('b', null, msg));
    var items = (list || []).map(function (a) { return typeof a === 'string' ? a : (a && a.text) || ''; })
      .filter(Boolean);
    if (items.length) {
      var ul = mk('ul');
      items.forEach(function (t) { ul.appendChild(mk('li', null, t)); });
      box.appendChild(ul);
    }
    return box;
  }

  /* ================= 打开 / 渲染结果 ================= */
  function openJob(id, quiet) {
    return api('/api/job/' + id).then(function (job) {
      if (job.status !== 'done' || !job.result) {
        if (!quiet) toast('这个任务还没出结果', 'err');
        return;
      }
      S.job = job;
      S.jobs[id] = job;
      if (S.order.indexOf(id) < 0) { S.order.push(id); }
      paintJobs();
      renderResult(job);
      try { localStorage.setItem(LS_LAST, id); } catch (e) { }
      showResultArea(true);
    }).catch(function (e) {
      if (!quiet) toast(e.message, 'err');
      try { localStorage.removeItem(LS_LAST); } catch (x) { }
    });
  }

  function showResultArea(on) {
    $('#empty').classList.toggle('hidden', !!on);
    $('#result').hidden = !on;
    if (on) $('#result').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function renderResult(job) {
    var r = job.result || {};
    S.job = job;
    S.page = 1;
    // 手机屏太矮：设置面板一展开就把结果顶出屏幕，出结果时自动收起
    if (narrowScreen() && prefs.panelOpen) {
      prefs.panelOpen = false;
      applyPanel();
      savePrefs();
    }
    $('#result').hidden = false;
    $('#empty').classList.add('hidden');
    $('#res-title').textContent = r.title || job.name || '未命名';
    renderChips(r, job);
    var adv = (r.advice && r.advice.length ? r.advice : job.advice) || [];
    var box = $('#res-advice');
    box.innerHTML = '';
    if (adv.length) {
      var hasErr = adv.some(function (a) { return a && a.level === 'err'; });
      box.appendChild(adviceBox(hasErr ? '这份结果有点问题' : '两件事值得看一眼', adv, hasErr));
      box.classList.remove('hidden');
    } else {
      box.classList.add('hidden');
    }

    var parsed = parseSmc(r.smc || '');
    S.score = buildScore(parsed);
    player.load({
      slots: S.score.slots.map(function (s) { return s.map(function (n) { return n.midi; }); }),
      perBar: parsed.perBar,
      beatsPerBar: parsed.beatsPerBar,
      tempo: parsed.tempo
    });
    pad.setUsed(S.score.used);
    pad.setFocusOnly(prefs.padFocus);
    player.setMetronome(prefs.metro);
    player.setRate(prefs.rate);
    buildBarRuler(S.score.bars);
    $('#loop-a').max = String(S.score.bars);
    $('#loop-b').max = String(S.score.bars);
    $('#loop-a').value = '1';
    $('#loop-b').value = String(S.score.bars);
    syncLoop();

    fillTune(job, r);
    setScoreImage();
    setDownloads();
    loadPages();
  }

  function renderChips(r, job) {
    var host = $('#res-chips');
    host.innerHTML = '';
    function tag(text, cls) { host.appendChild(mk('span', 'tag' + (cls ? ' ' + cls : ''), text)); }
    if (r.key_raw) tag(r.key_raw + ' 调', 'k');
    if (r.tempo) tag('速度 ' + r.tempo + ' · ' + (r.time_sig || '4/4'));
    if (r.bars) tag((r.notes || 0) + ' 个音 / ' + r.bars + ' 小节');
    if (r.seconds) tag('原曲约 ' + fmtTime(r.seconds));
    if (r.difficulty) tag('难度 ' + r.difficulty);
    if (r.confidence) {
      var hit = r.hit == null ? null : Math.round(r.hit);
      var cls = r.confidence === '高' ? 'ok' : r.confidence === '低' ? 'warn' : '';
      tag('测速 ' + r.confidence + (hit == null ? '' : ' ' + hit + '%'), cls);
    }
    if (r.doubled) tag('节奏按双倍网格对上的', 'warn');
    if (r.rubato && r.rubato.applied) {
      tag('跟随速度起伏' + (r.rubato.gq ? ' ' + r.rubato.gq.toFixed(2) : ''), 'ok');
    }
    var aq = r.audioq;
    if (aq && aq.ok) {
      tag('录音 ' + aq.score + '/100',
        aq.level === 'bad' ? 'warn' : aq.level === 'good' ? 'ok' : '');
    }
    if (r.rearranged) tag('已调参重排');
    if (r.restored) tag('从历史恢复（不能重排）');
    if (r.backend) tag(backendName(r.backend));
    if (job.kind === 'smc') tag('现成谱面');
  }

  /* ---------- 谱面图 / 分页 ---------- */
  function expQuery(extra) {
    var q = new URLSearchParams();
    q.set('theme', $('#exp-theme').value || prefs.expTheme);
    q.set('dpi', $('#exp-dpi').value || prefs.expDpi);
    if (narrowScreen()) q.set('bars', '2');   // 手机竖屏一行 2 小节，字大一倍
    for (var k in (extra || {})) q.set(k, extra[k]);
    return q.toString();
  }

  function narrowScreen() { return window.innerWidth <= 720; }

  // 一页放得下且屏幕够宽 → 长图（一直往下滚）；多页 → 按页翻；
  // 手机窄屏且只有一页 → 只画第一页并按内容裁边（字最大，不留整页空白）
  function scoreMode() {
    if (S.pages > 1) return 'page';
    return narrowScreen() ? 'pagefit' : 'long';
  }

  function scoreSrc(mode, dpi) {
    return '/api/export/' + S.job.id + '?mode=' + mode +
      (mode === 'page' ? '&page=' + clamp(S.page, 1, S.pages) : '') +
      '&' + expQuery(dpi ? { dpi: dpi } : { dpi: 150 });
  }

  function setScoreImage() {
    if (!S.job) return;
    var img = $('#score-img');
    var mode = scoreMode();
    var src = scoreSrc(mode);
    var multi = S.pages > 1;
    $('#page-nav').hidden = !multi;
    if (img.dataset.mode === mode && img.getAttribute('src') === src) return;
    img.dataset.mode = mode;
    img.title = mode === 'long' ? '点一下看大图' : '点一下看这一页的大图';
    $('#score-loading').hidden = false;
    $('#score-loading').textContent = '正在画谱面…';
    img.onload = function () { $('#score-loading').hidden = true; };
    img.onerror = function () {
      $('#score-loading').hidden = false;
      $('#score-loading').textContent = '谱面图没画出来，导出按钮还在下面，可以先存一张试试。';
    };
    img.src = src;
  }

  function setDownloads() {
    if (!S.job) return;
    var id = S.job.id;
    $('#dl-long').href = '/api/export/' + id + '?mode=long&' + expQuery();
    $('#dl-pages').href = '/api/export/' + id + '?mode=pages&' + expQuery();
    $('#dl-pdf').href = '/api/export/' + id + '?mode=pdf&' + expQuery();
    $('#dl-smc').href = '/api/export/' + id + '?mode=smc';
    $('#dl-midi').href = '/api/export/' + id + '?mode=midi';
    $('#dl-page').href = '/api/export/' + id + '?mode=page&page=' + S.page + '&' + expQuery();
  }

  function loadPages() {
    if (!S.job) return;
    api('/api/info/' + S.job.id + '?' + expQuery()).then(function (info) {
      S.pages = Math.max(1, info.pages || 1);
      S.page = clamp(S.page, 1, S.pages);
      $('#pg-count').textContent = String(S.pages);
      paintPage();
      setScoreImage();
    }).catch(function () {
      S.pages = 1;
      S.page = 1;
      $('#pg-count').textContent = '1';
      paintPage();
      setScoreImage();
    });
  }

  function paintPage() {
    if (!S.job) return;
    S.page = clamp(S.page, 1, S.pages);
    $('#pg-num').textContent = String(S.page);
    $('#pg-prev').disabled = S.page <= 1;
    $('#pg-next').disabled = S.page >= S.pages;
    $('#dl-page').href = '/api/export/' + S.job.id + '?mode=page&page=' + S.page + '&' + expQuery();
    if (scoreMode() === 'page') setScoreImage();
  }

  /* ---------- 调参 ---------- */
  function paintAlts(r) {
    var box = $('#tune-alts'), list = $('#tune-alts-list');
    if (!box || !list) return;
    list.innerHTML = '';
    var alts = (r && r.tempo_alts) || [];
    if (!alts.length || (r && r.restored)) { box.classList.add('hidden'); return; }
    alts.forEach(function (a) {
      var b = document.createElement('button');
      b.className = 'alt-chip';
      b.type = 'button';
      b.innerHTML = a.tempo + ' BPM<small>' + (a.factor ? a.factor + '×' : '') + '</small>';
      b.title = '按 ' + a.tempo + ' BPM 重排一次（每小节约 ' + (a.npb || '?') + ' 个音）';
      b.addEventListener('click', function () {
        if (!S.job || S.busy) return;
        b.classList.add('busy');
        $('#tune-tempo').value = String(a.tempo);
        rerun(false);
      });
      list.appendChild(b);
    });
    box.classList.remove('hidden');
  }

  function fillTune(job, r) {
    var p = (r && r.params) || {};
    if (r && r.tempo) $('#tune-tempo').value = String(r.tempo);
    if (r && r.key) $('#tune-key').value = r.key;
    else if (p.key) $('#tune-key').value = p.key;
    else $('#tune-key').value = '';
    $('#tune-chords').value = p.chords || 'auto';
    $('#tune-maxnotes').value = String(p.max_notes || 3);
    paintAlts(r);
    var st = $('#tune-state');
    st.className = 'tune-state';
    st.textContent = r && r.restored ? '这份是历史记录，重排要重新转一次' : '';
  }

  function rerun(restoreAuto) {
    if (!S.job || S.busy) return;
    S.busy = true;
    var btn = $('#btn-rerun');
    btn.classList.add('busy');
    var body = {};
    if (restoreAuto) {
      body.tempo = '';
    } else {
      var t = $('#tune-tempo').value.trim();
      if (t) body.tempo = Number(t);
      var k = $('#tune-key').value;
      if (k) body.key = k;
      var chords = $('#tune-chords').value;
      body.chords = chords;
      body.harmony = chords !== 'off';
      body.max_notes = Number($('#tune-maxnotes').value || 3);
    }
    api('/api/rearrange/' + S.job.id, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function () {
      return api('/api/job/' + S.job.id);
    }).then(function (job) {
      renderResult(job);
      S.jobs[job.id] = job;
      paintJobs();
      var st = $('#tune-state');
      st.className = 'tune-state ok';
      st.textContent = '已按新参数重排，谱面是最新的';
      toast('重排好了', 'ok');
    }).catch(function (e) {
      var st = $('#tune-state');
      st.className = 'tune-state';
      st.textContent = e.message;
      if (e.advice && e.advice.length) {
        var box = $('#res-advice');
        box.innerHTML = '';
        box.appendChild(adviceBox(e.message, e.advice, true));
        box.classList.remove('hidden');
      }
      toast(e.message, 'err');
    }).then(function () {
      S.busy = false;
      btn.classList.remove('busy');
    });
  }

  /* ---------- 小节尺 ---------- */
  function buildBarRuler(bars) {
    var host = $('#bar-ruler');
    host.innerHTML = '';
    for (var i = 0; i < bars; i++) {
      var b = mk('button', 'bar-cell', String(i + 1));
      b.setAttribute('data-bar', i);
      b.title = '跳到第 ' + (i + 1) + ' 小节';
      b.addEventListener('click', function () {
        var bar = Number(this.getAttribute('data-bar'));
        player.seekBar(bar);
      });
      host.appendChild(b);
    }
    syncLoop();
  }

  function syncLoop() {
    var on = $('#loop-on').checked;
    var a = clamp(parseInt($('#loop-a').value, 10) || 1, 1, S.score ? S.score.bars : 1);
    var b = clamp(parseInt($('#loop-b').value, 10) || 1, a, S.score ? S.score.bars : 1);
    if (on) player.setLoop(a - 1, b - 1); else player.setLoop(null, null);
    $$('#bar-ruler .bar-cell').forEach(function (c) {
      var i = Number(c.getAttribute('data-bar'));
      c.classList.toggle('inloop', on && i >= a - 1 && i <= b - 1);
    });
  }

  /* ================= 历史 ================= */
  function loadHistory() {
    return api('/api/history').then(function (h) {
      var items = h.items || [];
      var badge = $('#hist-count');
      badge.textContent = String(items.length);
      badge.classList.toggle('hidden', !items.length);
      renderHistory(items);
      return items;
    }).catch(function () { return []; });
  }

  function renderHistory(items) {
    var host = $('#hist-list');
    host.innerHTML = '';
    if (!items.length) {
      host.appendChild(mk('div', 'hist-empty', '还没有记录。转出来的谱子会自动存在这里，刷新页面也不丢。'));
      return;
    }
    items.forEach(function (it) {
      var row = mk('div', 'hist-item');
      row.setAttribute('data-id', it.id);
      var main = mk('div', 'hist-main');
      var title = mk('div', 'hist-title', it.title || '未命名');
      var sub = mk('div', 'hist-sub');
      var bits = [];
      if (it.date) bits.push(it.date);
      if (it.at) bits.push(it.at);
      if (it.tempo) bits.push('速度 ' + it.tempo);
      if (it.key_raw || it.key) bits.push((it.key_raw || it.key) + ' 调');
      if (it.bars) bits.push(it.bars + ' 小节');
      if (it.notes) bits.push(it.notes + ' 音');
      sub.textContent = bits.join(' · ');
      main.appendChild(title);
      main.appendChild(sub);
      var acts = mk('div', 'hist-acts');

      var openBtn = mk('button', 'icon-btn', '');
      openBtn.title = '打开';
      openBtn.innerHTML = '<svg viewBox="0 0 16 16"><path d="M6 3h7v7M13 3L6.5 9.5M3 6.5V13h6.5"/></svg>';
      openBtn.addEventListener('click', function () {
        $('#history').close();
        openJob(it.id);
      });
      acts.appendChild(openBtn);

      var renBtn = mk('button', 'icon-btn', '');
      renBtn.title = '改名';
      renBtn.innerHTML = '<svg viewBox="0 0 16 16"><path d="M3 11.5L11 3.5l1.5 1.5-8 8H3zM10 4.5l1.5 1.5"/></svg>';
      renBtn.addEventListener('click', function () {
        startRename(row, it);
      });
      acts.appendChild(renBtn);

      var dlBtn = mk('button', 'icon-btn', '');
      dlBtn.title = '下载谱面文件';
      dlBtn.innerHTML = '<svg viewBox="0 0 16 16"><path d="M8 2v8m0 0L4.8 6.8M8 10l3.2-3.2M2.5 13h11"/></svg>';
      dlBtn.addEventListener('click', function () {
        window.location.href = '/api/export/' + it.id + '?mode=smc';
      });
      acts.appendChild(dlBtn);

      var delBtn = mk('button', 'icon-btn danger', '');
      delBtn.title = '删除';
      delBtn.innerHTML = '<svg viewBox="0 0 16 16"><path d="M3.5 4.5h9M6.5 4.5V3h3v1.5M4.8 4.5l.6 8.2h5.2l.6-8.2"/></svg>';
      var armed = false, armTimer = 0;
      delBtn.addEventListener('click', function () {
        if (!armed) {
          armed = true;
          delBtn.style.color = 'var(--err)';
          delBtn.title = '再点一下真删';
          armTimer = setTimeout(function () { armed = false; delBtn.style.color = ''; delBtn.title = '删除'; }, 3000);
          return;
        }
        clearTimeout(armTimer);
        api('/api/history/' + it.id + '/delete', { method: 'POST' }).then(function () {
          if (S.job && S.job.id === it.id) {
            S.job = null;
            $('#result').hidden = true;
            if (!Object.keys(S.jobs).length) $('#empty').classList.remove('hidden');
          }
          toast('删掉了', 'ok');
          loadHistory();
        }).catch(function (e) { toast(e.message, 'err'); });
      });
      acts.appendChild(delBtn);

      row.appendChild(main);
      row.appendChild(acts);
      host.appendChild(row);
    });
  }

  function startRename(row, it) {
    var main = row.querySelector('.hist-main');
    var title = row.querySelector('.hist-title');
    if (!title) return;
    var input = mk('input', 'hist-rename');
    input.type = 'text';
    input.value = it.title || '';
    input.maxLength = 80;
    main.replaceChild(input, title);
    input.focus();
    input.select();
    var committed = false;
    function commit() {
      if (committed) return;
      committed = true;
      var v = input.value.trim();
      if (!v || v === it.title) { main.replaceChild(title, input); return; }
      api('/api/history/' + it.id + '/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: v })
      }).then(function () {
        toast('改好了', 'ok');
        if (S.job && S.job.id === it.id) {
          S.job.result.title = v;
          $('#res-title').textContent = v;
        }
        loadHistory();
        if (S.job && S.job.id === it.id) setScoreImage();
      }).catch(function (e) { toast(e.message, 'err'); main.replaceChild(title, input); });
    }
    input.addEventListener('blur', commit);
    input.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); commit(); }
      if (ev.key === 'Escape') { committed = true; main.replaceChild(title, input); }
    });
  }

  /* ================= 设置面板摘要 ================= */
  function updatePanelSummary() {
    var bits = [];
    bits.push($('#opt-tempo').value ? ('速度 ' + $('#opt-tempo').value) : '自动测速');
    bits.push($('#opt-key').value ? (KEY_LABEL[$('#opt-key').value] || $('#opt-key').value) : '自动定调');
    var chords = $('#opt-chords').value;
    bits.push(chords === 'off' ? '只留主旋律' : chords === 'on' ? '每格加和弦' : '和弦自动');
    var b = $('#opt-backend').value;
    if (b) bits.push(backendName(b));
    $('#panel-summary').textContent = bits.join(' · ');
  }

  /* ================= 事件绑定 ================= */
  function bind() {
    // 顶栏
    $('#btn-theme').addEventListener('click', function () {
      prefs.theme = prefs.theme === 'dark' ? 'light' : 'dark';
      applyTheme(); savePrefs();
      if (S.job) { setScoreImage(); paintPage(); }
    });
    $('#btn-fs-up').addEventListener('click', function () {
      prefs.fsIdx = clamp(prefs.fsIdx + 1, 0, FS_STEPS.length - 1);
      applyFs(); savePrefs();
    });
    $('#btn-fs-down').addEventListener('click', function () {
      prefs.fsIdx = clamp(prefs.fsIdx - 1, 0, FS_STEPS.length - 1);
      applyFs(); savePrefs();
    });
    $('#btn-history').addEventListener('click', function () {
      loadHistory();
      var d = $('#history');
      if (d.showModal) d.showModal(); else d.setAttribute('open', '');
    });
    $('#hist-close').addEventListener('click', function () { $('#history').close(); });

    // 空态
    $('#btn-pick').addEventListener('click', function () { $('#file').click(); });
    $('#btn-demo').addEventListener('click', loadExample);
    $('#file').addEventListener('change', function (e) {
      addFiles(e.target.files);
      e.target.value = '';
    });

    // 设置
    $('#panel-toggle').addEventListener('click', function () {
      prefs.panelOpen = !prefs.panelOpen;
      applyPanel(); savePrefs();
    });
    ['#opt-tempo', '#opt-key', '#opt-backend', '#opt-chords', '#opt-maxnotes', '#opt-rubato'].forEach(function (s) {
      $(s).addEventListener('change', updatePanelSummary);
      $(s).addEventListener('input', updatePanelSummary);
    });

    // 播放器
    $('#btn-play').addEventListener('click', function () { player.toggle(); });
    $('#prog').addEventListener('input', function () {
      S.draggingProg = true;
      player.seek(Number(this.value));
      onTick(player.info(Number(this.value)));
    });
    $('#prog').addEventListener('change', function () {
      S.draggingProg = false;
      player.seek(Number(this.value));
    });
    $('#rate').addEventListener('input', function () {
      var r = Number(this.value) / 100;
      prefs.rate = r;
      $('#rate-val').textContent = r.toFixed(2) + '×';
      player.setRate(r);
      if (S.score) onTick(player.info(Math.floor(player.playhead())));
      savePrefs();
    });
    $('#metro').addEventListener('change', function () {
      prefs.metro = this.checked;
      player.setMetronome(this.checked);
      savePrefs();
    });
    $('#loop-on').addEventListener('change', syncLoop);
    $('#loop-a').addEventListener('change', syncLoop);
    $('#loop-b').addEventListener('change', syncLoop);

    // 键位
    $('#pad-focus').addEventListener('change', function () {
      prefs.padFocus = this.checked;
      pad.setFocusOnly(this.checked);
      savePrefs();
    });

    // 调参
    $('#btn-rerun').addEventListener('click', function () { rerun(false); });
    $('#btn-rerun-top').addEventListener('click', function () { rerun(false); $('#result').scrollIntoView({ block: 'start' }); });
    $('#btn-reset-tune').addEventListener('click', function () { rerun(true); });
    $('#btn-res-close').addEventListener('click', function () {
      $('#result').hidden = true;
      $('#empty').classList.remove('hidden');
      player.pause();
    });

    // 导出
    $('#exp-theme').addEventListener('change', function () {
      prefs.expTheme = this.value; savePrefs();
      if (S.job) { setScoreImage(); setDownloads(); paintPage(); }
    });
    $('#exp-dpi').addEventListener('change', function () {
      prefs.expDpi = this.value; savePrefs();
      if (S.job) { setDownloads(); loadPages(); }
    });
    ['#dl-long', '#dl-pages', '#dl-pdf', '#dl-midi'].forEach(function (s) {
      $(s).addEventListener('click', function () {
        var big = s === '#dl-pages' || s === '#dl-pdf';
        toast(big ? '正在生成文件，大谱子要等几秒' : '开始下载', 'ok');
      });
    });
    $('#btn-score-text').addEventListener('click', function () {
      var pre = $('#smc-text');
      pre.hidden = !pre.hidden;
      if (!pre.hidden && S.job && S.job.result) pre.textContent = S.job.result.smc || '';
      this.textContent = pre.hidden ? '源码文本' : '收起源码';
    });
    $('#btn-copy-smc').addEventListener('click', function () {
      var txt = (S.job && S.job.result && S.job.result.smc) || '';
      if (!txt) return;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(txt).then(function () { toast('谱面文本已复制', 'ok'); },
          function () { toast('复制没成功，用「源码文本」手动选吧', 'err'); });
      } else {
        toast('这个浏览器不给复制权限，点「源码文本」手动选吧', 'err');
      }
    });
    $('#pg-prev').addEventListener('click', function () { S.page = clamp(S.page - 1, 1, S.pages); paintPage(); });
    $('#pg-next').addEventListener('click', function () { S.page = clamp(S.page + 1, 1, S.pages); paintPage(); });

    // 点谱面看大图（手机上看长图最实用）
    $('#score-img').addEventListener('click', function () {
      if (!S.job || !this.getAttribute('src')) return;
      var mode = this.dataset.mode || 'long';
      var url = scoreSrc(mode, 300);
      window.open(url, '_blank', 'noopener');
    });

    // 窗口变了（横竖屏切换、拉宽浏览器）重新决定用长图还是分页
    var rz = 0;
    window.addEventListener('resize', function () {
      clearTimeout(rz);
      rz = setTimeout(function () {
        if (!S.job) return;
        var img = $('#score-img');
        if (img.dataset.mode !== scoreMode()) setScoreImage();
      }, 250);
    });

    // 键盘：空格播放/暂停，左右移一小节
    window.addEventListener('keydown', function (ev) {
      var tag = (ev.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'select' || tag === 'textarea' || ev.target.isContentEditable) return;
      if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
      if (ev.key === ' ' && S.score) { ev.preventDefault(); player.toggle(); }
      if (ev.key === 'ArrowRight' && S.score) { ev.preventDefault(); player.seekBar(Math.min(S.score.bars - 1, player.info(Math.floor(player.playhead())).bar + 1)); }
      if (ev.key === 'ArrowLeft' && S.score) { ev.preventDefault(); player.seekBar(Math.max(0, player.info(Math.floor(player.playhead())).bar - 1)); }
    });

    bindDrop();
    bindPaste();
  }

  function applyPanel() {
    $('#panel').classList.toggle('open', !!prefs.panelOpen);
    $('#panel-toggle').setAttribute('aria-expanded', prefs.panelOpen ? 'true' : 'false');
  }

  /* ---------- 拖拽 ---------- */
  function bindDrop() {
    var mask = $('#dropmask');
    var depth = 0;
    function hasFiles(ev) {
      var dt = ev.dataTransfer;
      if (!dt) return false;
      if (dt.types) {
        for (var i = 0; i < dt.types.length; i++) if (dt.types[i] === 'Files') return true;
      }
      return false;
    }
    window.addEventListener('dragenter', function (ev) {
      if (!hasFiles(ev)) return;
      ev.preventDefault();
      depth += 1;
      mask.classList.add('show');
    });
    window.addEventListener('dragover', function (ev) {
      if (!hasFiles(ev)) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = 'copy';
    });
    window.addEventListener('dragleave', function (ev) {
      if (!hasFiles(ev) && !ev.relatedTarget) return;
      depth -= 1;
      if (depth <= 0) { depth = 0; mask.classList.remove('show'); }
    });
    window.addEventListener('drop', function (ev) {
      ev.preventDefault();
      depth = 0;
      mask.classList.remove('show');
      var files = ev.dataTransfer && ev.dataTransfer.files;
      if (files && files.length) addFiles(files);
    });
    document.addEventListener('dragover', function (ev) { ev.preventDefault(); });
    document.addEventListener('drop', function (ev) { ev.preventDefault(); });
  }

  /* ---------- 粘贴 ---------- */
  function bindPaste() {
    window.addEventListener('paste', function (ev) {
      var cd = ev.clipboardData;
      if (!cd) return;
      var files = [];
      var items = cd.items || [];
      for (var i = 0; i < items.length; i++) {
        if (items[i].kind !== 'file') continue;
        var f = items[i].getAsFile();
        if (!f) continue;
        var ext = (f.type.split('/')[1] || 'png').replace('jpeg', 'jpg');
        var name = '剪贴板-' + stamp() + '.' + ext;
        try {
          files.push(new File([f], name, { type: f.type || 'image/png' }));
        } catch (e) {
          files.push(f);
        }
      }
      if (files.length) {
        ev.preventDefault();
        addFiles(files);
        toast('从剪贴板拿到 ' + files.length + ' 张，开始转');
        return;
      }
      var text = (cd.getData('text') || '').trim();
      if (/^https?:\/\/\S+\.(png|jpe?g|webp|bmp|gif|mp3|wav|m4a|mp4|pdf|smc)(\?\S*)?$/i.test(text)) {
        ev.preventDefault();
        toast('正在取粘贴的链接…');
        fetch(text).then(function (r) { return r.blob(); }).then(function (blob) {
          var ext = (text.split('.').pop().split(/[?#]/)[0] || 'png').toLowerCase();
          var name = '粘贴链接-' + stamp() + '.' + ext;
          addFiles([new File([blob], name, { type: blob.type })]);
        }).catch(function () {
          toast('那个链接取不下来，先存到本地再拖进来', 'err');
        });
      }
    });
  }
  function stamp() {
    var d = new Date();
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return String(d.getFullYear()).slice(2) + p(d.getMonth() + 1) + p(d.getDate()) + '-' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
  }

  /* ---------- 示例 ---------- */
  function loadExample() {
    api('/api/example', { method: 'POST' }).then(function (r) {
      var id = r.job || r.id;
      S.jobs[id] = { id: id, name: '示例：小星星', status: 'queued', stage: '排队中', progress: 0, stage_index: 0, stages: [] };
      S.order.push(id);
      paintJobs();
      showResultArea(true);
      startPoll();
    }).catch(function (e) { toast(e.message, 'err'); });
  }

  /* ================= 初始化 ================= */
  function fillSelects() {
    var o = '<option value="">自动挑调（推荐）</option>';
    KEYS.forEach(function (k) { o += '<option value="' + k + '">' + KEY_LABEL[k] + '</option>'; });
    $('#opt-key').innerHTML = o;
    var t = '<option value="">跟当前一样</option>';
    KEYS.forEach(function (k) { t += '<option value="' + k + '">' + KEY_LABEL[k] + '</option>'; });
    $('#tune-key').innerHTML = t;
  }

  function init() {
    applyTheme();
    applyFs();
    applyPanel();
    fillSelects();
    restoreUploadChoice();
    $('#exp-theme').value = prefs.expTheme;
    $('#exp-dpi').value = prefs.expDpi;
    $('#rate').value = String(Math.round(clamp(prefs.rate, 0.5, 1.5) * 100));
    $('#rate-val').textContent = clamp(prefs.rate, 0.5, 1.5).toFixed(2) + '×';
    $('#metro').checked = !!prefs.metro;
    $('#pad-focus').checked = !!prefs.padFocus;
    $('#prog').max = '0';
    onTick({ slot: 0, bar: 0, bars: 1, total: 0, notes: [] });
    bind();
    updatePanelSummary();
    pad.setUsed([]);

    api('/api/ping').then(function (p) {
      $('#foot-ver').textContent = '服务版本 ' + (p.version || '') + ' · 历史 ' + (p.history || 0) + ' 条';
    }).catch(function () {
      $('#foot-ver').textContent = '没连上服务';
    });
    loadHistory();

    // 刷新后回到上次的结果页；先查历史确认这个 id 还在（服务重启过就没了），
    // 这样不会再往控制台喷一个 404。
    var last = null;
    try { last = localStorage.getItem(LS_LAST); } catch (e) { }
    if (last) {
      api('/api/history').then(function (h) {
        var alive = (h.items || []).some(function (it) { return it.id === last; });
        if (!alive) {
          try { localStorage.removeItem(LS_LAST); } catch (e) { }
          return null;
        }
        return openJob(last, true);
      }).catch(function () { });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // 给控制台留个把手，方便自己/验收脚本查状态
  window.SkyApp = { S: S, player: player, pad: pad, openJob: openJob, rerun: rerun, prefs: prefs };
})();
