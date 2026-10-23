/* sky15 · 15 键面板
   一块灰圆角方块 = 一个键：中间大数字是简谱级数，下方的点是八度，右下角是电脑键盘上的键位。
   用户明确不要菱形/圆环那类装饰，这里就只有方块、数字、点、字母。 */
(function () {
  'use strict';

  var CELL = 68, GAP = 15, R = 11, COLS = 5, ROWS = 3;
  var STEP = CELL + GAP;
  var W = COLS * CELL + (COLS - 1) * GAP;
  var H = ROWS * CELL + (ROWS - 1) * GAP;
  var NS = 'http://www.w3.org/2000/svg';

  var KB_ROWS = [
    ['y', 'u', 'i', 'o', 'p'],
    ['h', 'j', 'k', 'l', ';'],
    ['n', 'm', ',', '.', '/']
  ];

  function el(name, attrs) {
    var n = document.createElementNS(NS, name);
    for (var k in attrs) if (attrs.hasOwnProperty(k)) n.setAttribute(k, attrs[k]);
    return n;
  }

  /* 第 i 键：0..6 低八度 1-7，7..13 中八度 1-7，14 高八度 1 */
  function label(i) {
    if (i < 7) return { deg: i + 1, dots: 0 };
    if (i < 14) return { deg: i - 6, dots: 1 };
    return { deg: 1, dots: 2 };
  }

  function keyLetter(i) {
    return KB_ROWS[Math.floor(i / 5)][i % 5];
  }

  function Pad(mount, onDown) {
    this.mount = mount;
    this.onDown = onDown || function () {};
    this.keys = [];
    this.focusOnly = false;
    this.used = null;
    this.svg = null;
    this.build();
    this.bindKeys();
  }

  Pad.prototype.build = function () {
    var svg = el('svg', {
      viewBox: '0 0 ' + W + ' ' + H,
      role: 'group',
      'aria-label': '15 键面板'
    });
    var self = this;
    for (var i = 0; i < 15; i++) {
      var col = i % COLS, row = Math.floor(i / COLS);
      var x = col * STEP, y = row * STEP;
      var g = el('g', { class: 'skykey', 'data-i': i, tabindex: '-1' });
      var rect = el('rect', {
        x: x, y: y, width: CELL, height: CELL, rx: R,
        fill: 'var(--key)', stroke: 'var(--key-line)', 'stroke-width': 1.5
      });
      var lab = label(i);
      var dots = lab.dots;
      var numY = CELL / 2 + (dots ? 10 : 6);
      var num = el('text', {
        class: 'kb-num', x: x + CELL / 2, y: y + numY,
        'text-anchor': 'middle', 'font-size': 30, fill: 'var(--key-num)'
      });
      num.textContent = String(lab.deg);
      g.appendChild(rect);
      g.appendChild(num);

      for (var d = 0; d < dots; d++) {
        var dot = el('circle', {
          class: 'oct-dot', cx: x + CELL / 2, cy: y + CELL / 2 - 13 - d * 9, r: 3.6
        });
        g.appendChild(dot);
      }

      var ltr = el('text', {
        class: 'kb-ltr', x: x + CELL - 6, y: y + CELL - 6,
        'text-anchor': 'end', 'font-size': 12.5,
        fill: 'var(--key-ltr)', 'font-family': 'Consolas, monospace'
      });
      ltr.textContent = keyLetter(i);
      g.appendChild(ltr);

      this.keys.push({ g: g, rect: rect, num: num, ltr: ltr, i: i });
      svg.appendChild(g);
    }

    svg.addEventListener('pointerdown', function (ev) {
      var target = ev.target.closest ? ev.target.closest('.skykey') : null;
      if (!target) return;
      var idx = Number(target.getAttribute('data-i'));
      self.press(idx);
    });

    this.mount.innerHTML = '';
    this.mount.appendChild(svg);
    this.svg = svg;
  };

  Pad.prototype.press = function (i) {
    this.flash(i, 130);
    this.onDown(i);
  };

  Pad.prototype.flash = function (i, ms) {
    var k = this.keys[i];
    if (!k) return;
    k.g.classList.add('down');
    clearTimeout(k._t);
    var self = k;
    k._t = setTimeout(function () { self.g.classList.remove('down'); }, ms || 130);
  };

  Pad.prototype.bindKeys = function () {
    var self = this;
    var map = {};
    for (var i = 0; i < 15; i++) map[keyLetter(i)] = i;

    window.addEventListener('keydown', function (ev) {
      if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
      var tag = (ev.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'select' || tag === 'textarea' || ev.target.isContentEditable) return;
      var k = ev.key.toLowerCase();
      if (!(k in map)) return;
      ev.preventDefault();
      if (ev.repeat) return;
      self.press(map[k]);
    });
  };

  Pad.prototype.setUsed = function (list) {
    var used = {};
    (list || []).forEach(function (i) { used[i] = 1; });
    this.used = used;
    for (var i = 0; i < 15; i++) {
      var k = this.keys[i];
      var isUsed = !!used[i];
      k.g.classList.toggle('hot', isUsed);
      if (this.focusOnly && list && list.length) k.g.classList.toggle('faded', !isUsed);
      else k.g.classList.remove('faded');
    }
  };

  Pad.prototype.setFocusOnly = function (on) {
    this.focusOnly = !!on;
    var list = this.used ? Object.keys(this.used).map(Number) : null;
    this.setUsed(list);
  };

  /* 播放时点亮当前音符 */
  Pad.prototype.highlight = function (indexes) {
    var want = {};
    (indexes || []).forEach(function (i) { want[i] = 1; });
    for (var i = 0; i < 15; i++) {
      this.keys[i].g.classList.toggle('on', !!want[i]);
    }
  };

  Pad.prototype.clearHighlight = function () { this.highlight([]); };

  Pad.prototype.setBase = function () { /* 数字是级数，换调不变，占位便于将来标音名 */ };

  window.SkyPad = Pad;
  window.SkyPadKB = KB_ROWS;
})();
