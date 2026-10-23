/* sky15 · 播放器
   调度方式：WebAudio 前瞻调度（lookahead），每 30ms 把未来 150ms 内该响的格子排进去。
   ★ 铁律：48 个采样用 <audio> 时 `src.start()` 之后绝不再 `stop()`——
     原站每个音是 5 秒的长采样，按时值截断会把音掐掉，听起来像"卡断"。
     所以节奏靠"什么时候开始"控制，不靠"什么时候结束"。振荡器兜底音才允许 stop。 */
(function () {
  'use strict';

  var SAMPLE_BASE = 48;      // 采样 1.mp3 对应 MIDI 48
  var SAMPLE_COUNT = 48;
  var AHEAD = 0.15;          // 前瞻窗口（秒）
  var TICK_MS = 30;

  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }

  function Player(opts) {
    opts = opts || {};
    this.onTick = opts.onTick || function () {};
    this.onState = opts.onState || function () {};

    this.ctx = null;
    this.bus = null;
    this.buffers = {};        // midi -> AudioBuffer（null 表示这条没采样）
    this.loading = {};        // midi -> Promise

    this.slots = [];          // [[midi, midi...], ...]
    this.perBar = 8;
    this.beatsPerBar = 4;
    this.tempo = 120;
    this.rate = 1;
    this.metronome = false;
    this.loop = null;         // {a: 起始小节下标, b: 结束小节下标}

    this.playing = false;
    this.i = 0;               // 下一个要排度的格子
    this.nextTime = 0;        // 该格子的绝对上下文时间
    this.idleSlot = 0;        // 暂停/停止时的位置
    this.timer = 0;
    this.raf = 0;
    this._lastSlot = -1;
  }

  Player.prototype.slotDur = function () {
    return (60 / Math.max(20, this.tempo) / 2) / this.rate;
  };

  Player.prototype.barOf = function (slot) {
    return Math.floor(slot / this.perBar);
  };

  Player.prototype.bars = function () {
    return Math.max(1, Math.ceil(this.slots.length / this.perBar));
  };

  Player.prototype.duration = function () {
    return this.slots.length * (60 / Math.max(20, this.tempo) / 2);
  };

  /* ---------- 音频上下文 ---------- */
  Player.prototype.ensure = function () {
    if (this.ctx) return this.ctx;
    var AC = window.AudioContext || window.webkitAudioContext;
    this.ctx = new AC({ latencyHint: 'interactive' });
    // 一压一收，多音同时响不至于糊成一团（照抄原站总线）
    var comp = this.ctx.createDynamicsCompressor();
    if (comp.threshold) comp.threshold.value = -14;
    if (comp.knee) comp.knee.value = 18;
    if (comp.ratio) comp.ratio.value = 4;
    if (comp.attack) comp.attack.value = 0.004;
    if (comp.release) comp.release.value = 0.2;
    var gain = this.ctx.createGain();
    gain.gain.value = 0.95;
    comp.connect(gain);
    gain.connect(this.ctx.destination);
    this.bus = comp;
    return this.ctx;
  };

  Player.prototype.resume = function () {
    this.ensure();
    if (this.ctx.state === 'suspended') this.ctx.resume();
    return this.ctx;
  };

  /* ---------- 采样 ---------- */
  Player.prototype.sampleUrl = function (midi) {
    var n = midi - SAMPLE_BASE + 1;
    if (n < 1 || n > SAMPLE_COUNT) return null;
    return 'samples/' + n + '.mp3';
  };

  Player.prototype.preload = function (midis) {
    var self = this;
    var ctx = this.ensure();
    (midis || []).forEach(function (midi) {
      if (!self.sampleUrl(midi)) { self.buffers[midi] = null; return; }
      if (midi in self.buffers || midi in self.loading) return;
      var p = fetch(self.sampleUrl(midi))
        .then(function (r) { return r.ok ? r.arrayBuffer() : null; })
        .then(function (buf) {
          if (!buf) { self.buffers[midi] = null; return null; }
          return new Promise(function (ok, bad) { ctx.decodeAudioData(buf, ok, bad); });
        })
        .then(function (ab) { self.buffers[midi] = ab || null; })
        .catch(function () { self.buffers[midi] = null; })
        .then(function () { delete self.loading[midi]; });
      self.loading[midi] = p;
    });
  };

  /* 弹一个音。when 为绝对上下文时间，缺省立刻。 */
  Player.prototype.playMidi = function (midi, when) {
    var ctx = this.resume();
    var t = Math.max(ctx.currentTime + 0.004, when || 0);
    var buf = this.buffers[midi];

    if (buf) {
      var src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(this.bus);
      src.start(t);
      // ★ 不调 src.stop()：5 秒采样自然放完，这是原站行为
      return;
    }

    if (!(midi in this.buffers)) {
      // 还没解出来：先按振荡器响一声，同时补下载
      this.preload([midi]);
    }
    this.synth(midi, t);
  };

  /* 没有采样时的兜底：四层正弦，像八音盒 */
  Player.prototype.synth = function (midi, t) {
    var ctx = this.ctx;
    var freq = 440 * Math.pow(2, (midi - 69) / 12);
    var len = 1.05;
    var parts = [[1, 1], [2, 0.42], [3, 0.2], [4, 0.1]];
    var g = ctx.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.11, t + 0.008);
    g.gain.exponentialRampToValueAtTime(0.0001, t + len);
    g.connect(this.bus);
    for (var k = 0; k < parts.length; k++) {
      var o = ctx.createOscillator();
      o.type = 'sine';
      o.frequency.value = freq * parts[k][0];
      var og = ctx.createGain();
      og.gain.value = parts[k][1];
      o.connect(og); og.connect(g);
      o.start(t); o.stop(t + len + 0.02);   // 振荡器可以停，采样不行
    }
  };

  Player.prototype.click = function (t, strong) {
    var ctx = this.ctx;
    var o = ctx.createOscillator();
    var g = ctx.createGain();
    o.type = 'triangle';
    o.frequency.value = strong ? 1750 : 1150;
    g.gain.setValueAtTime(0.0001, t);
    g.gain.linearRampToValueAtTime(strong ? 0.16 : 0.085, t + 0.003);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.055);
    o.connect(g); g.connect(this.bus);
    o.start(t); o.stop(t + 0.07);
  };

  /* ---------- 装载 ---------- */
  Player.prototype.load = function (data) {
    this.pause(true);
    this.slots = data.slots || [];
    this.perBar = Math.max(1, data.perBar || 8);
    this.beatsPerBar = Math.max(1, data.beatsPerBar || 4);
    this.tempo = data.tempo || 120;
    this.i = 0;
    this.idleSlot = 0;
    this._lastSlot = -1;
    this.onTick(this.info(0));
    return this;
  };

  Player.prototype.info = function (slot) {
    slot = clamp(slot | 0, 0, Math.max(0, this.slots.length - 1));
    return {
      slot: slot,
      bar: this.barOf(slot),
      bars: this.bars(),
      total: this.slots.length,
      notes: this.slots[slot] || [],
      playing: this.playing
    };
  };

  /* ---------- 播放头 ---------- */
  Player.prototype.playhead = function () {
    if (!this.playing || !this.ctx) return this.idleSlot;
    var sd = this.slotDur();
    var ahead = (this.nextTime - this.ctx.currentTime) / sd;
    var p = this.i - ahead;
    if (this.loop) {
      var a = this.loop.a * this.perBar;
      var b = Math.min(this.slots.length, (this.loop.b + 1) * this.perBar);
      if (p < a) p = a;
      if (p > b) p = b;
    }
    return clamp(p, 0, Math.max(0, this.slots.length - 1));
  };

  Player.prototype.loopRange = function () {
    if (!this.loop) return null;
    var a = this.loop.a * this.perBar;
    var b = Math.min(this.slots.length, (this.loop.b + 1) * this.perBar);
    return [a, b];
  };

  /* ---------- 调度 ---------- */
  Player.prototype._pump = function () {
    if (!this.playing) return;
    var ctx = this.ctx;
    var sd = this.slotDur();
    var range = this.loopRange();
    var guard = 0;
    while (this.nextTime < ctx.currentTime + AHEAD && guard++ < 64) {
      if (range && this.i >= range[1]) this.i = range[0];
      if (this.i >= this.slots.length) { this.finish(); return; }
      var t = this.nextTime;
      var notes = this.slots[this.i] || [];
      for (var k = 0; k < notes.length; k++) this.playMidi(notes[k], t);
      if (this.metronome) {
        var beatSlots = Math.max(1, Math.round(this.perBar / this.beatsPerBar));
        if (this.i % beatSlots === 0) this.click(t, this.i % this.perBar === 0);
      }
      this.nextTime = t + sd;
      this.i++;
    }
  };

  Player.prototype._frame = function () {
    var self = this;
    if (!this.playing) return;
    var head = this.playhead();
    var slot = Math.floor(head);
    if (slot !== this._lastSlot) {
      this._lastSlot = slot;
      this.onTick(this.info(slot));
    }
    this.raf = requestAnimationFrame(function () { self._frame(); });
  };

  Player.prototype.play = function () {
    if (!this.slots.length) return;
    var ctx = this.resume();
    if (this.playing) return;
    var range = this.loopRange();
    if (this.i >= this.slots.length || this.i < 0) this.i = range ? range[0] : 0;
    if (range && this.loop && (this.i < range[0] || this.i >= range[1])) this.i = range[0];
    this.nextTime = ctx.currentTime + 0.08;
    this.playing = true;
    var self = this;
    this._pump();
    this.timer = setInterval(function () { self._pump(); }, TICK_MS);
    this._frame();
    this.onState(true);
  };

  Player.prototype.pause = function (silent) {
    var head = this.playhead();
    this.playing = false;
    if (this.timer) { clearInterval(this.timer); this.timer = 0; }
    if (this.raf) { cancelAnimationFrame(this.raf); this.raf = 0; }
    this.idleSlot = Math.floor(head);
    this.i = this.idleSlot;
    if (!silent) { this.onState(false); this.onTick(this.info(this.idleSlot)); }
  };

  Player.prototype.finish = function () {
    this.pause();
    this.idleSlot = 0;
    this.i = 0;
    this.onTick(this.info(0));
  };

  Player.prototype.toggle = function () {
    if (this.playing) this.pause(); else this.play();
  };

  Player.prototype.seek = function (slot) {
    var range = this.loopRange();
    var max = Math.max(0, this.slots.length - 1);
    slot = clamp(Math.round(slot), 0, max);
    this.i = slot;
    this.idleSlot = slot;
    this._lastSlot = -1;
    if (this.playing) {
      this.nextTime = this.ctx.currentTime + 0.05;
      this._pump();
    }
    this.onTick(this.info(slot));
  };

  Player.prototype.seekBar = function (bar) {
    this.seek(Math.max(0, bar) * this.perBar);
  };

  Player.prototype.setRate = function (r) {
    r = clamp(Number(r) || 1, 0.25, 2);
    if (r === this.rate) return;
    var was = this.playing;
    var head = this.playhead();
    this.rate = r;
    if (was) {
      this.i = clamp(Math.floor(head) + 1, 0, Math.max(0, this.slots.length - 1));
      this.nextTime = this.ctx.currentTime + 0.06;
    }
    this.onTick(this.info(Math.floor(head)));
  };

  Player.prototype.setTempo = function (t) {
    t = Math.max(20, Number(t) || 120);
    if (t === this.tempo) return;
    var was = this.playing;
    var head = this.playhead();
    this.tempo = t;
    if (was) {
      this.i = clamp(Math.floor(head) + 1, 0, Math.max(0, this.slots.length - 1));
      this.nextTime = this.ctx.currentTime + 0.06;
    }
  };

  Player.prototype.setMetronome = function (on) { this.metronome = !!on; };

  Player.prototype.setLoop = function (aBar, bBar) {
    if (aBar == null || bBar == null || bBar < aBar) { this.loop = null; return; }
    this.loop = { a: aBar, b: bBar };
    if (this.playing) {
      var range = this.loopRange();
      if (this.i < range[0] || this.i >= range[1]) {
        this.i = range[0];
        this.nextTime = this.ctx.currentTime + 0.05;
      }
    }
  };

  window.SkyPlayer = Player;
})();
