/* SwarmScope timeline explorer: draws the payload that timeline_html.py embeds.
 *
 * Figure 1  timeline: one row per agent; per-bin message counts stacked by channel when zoomed
 *           out, one tick per message when zoomed in; goals/periods along the top; UTC dates on
 *           the top axis and Village days on the bottom axis.
 * Figure 2  mention matrix for the window on screen.  Figure 3  activity in that window.
 * Thread reader  the conversation around a message, in its room.
 *
 * Every data-derived string reaches the DOM through textContent, canvas fillText or an SVG text
 * node; nothing is ever parsed as HTML.
 */
(function () {
  'use strict';
  var D = JSON.parse(document.getElementById('data').textContent);
  var PK = window.PaperKit;
  var $ = function (id) { return document.getElementById(id); };
  var SVGNS = 'http://www.w3.org/2000/svg';
  var lanes = D.lanes, chans = D.channels, actors = D.actors || [], META = D.meta || {};
  var N = D.t.length, NL = D.ctx ? D.ctx.a : N;
  var T = new Float64Array(N);
  for (var i0 = 0; i0 < N; i0++) T[i0] = D.t0 + D.t[i0] * 1000;
  var BIN = D.dens.bin, BASE = D.dens.base;
  var DAYS = D.days && PK.days ? PK.days(D.days.day_one, D.days.tz) : null;
  var periods = (D.periods || []).map(function (p, i) { return { i: i, id: p.id, kind: p.kind, label: p.label, s: p.s, e: p.e == null ? null : p.e }; });
  var periodKinds = [];
  periods.forEach(function (p) { if (periodKinds.indexOf(p.kind) < 0) periodKinds.push(p.kind); });
  var notes = D.notes || [];

  // ---------------------------------------------------------------- state
  var span0 = Math.max(D.end - D.start, 60000), pad0 = span0 * 0.012;
  var FULL0 = D.start - pad0, FULL1 = D.end + pad0;
  var S = {
    v0: FULL0, v1: FULL1, order: 'first', axis: DAYS ? 'both' : 'date', mode: 'auto', scale: 'row',
    hidden: new Uint8Array(chans.length), period: -1, sel: -1, hover: null
  };
  var C = {}, chCol = [];

  if (!lanes.length) {
    var plot0 = $('plot');
    plot0.textContent = '';
    var em = document.createElement('div'); em.id = 'empty';
    em.textContent = 'No agent messages match these filters.';
    plot0.appendChild(em); plot0.style.height = 'auto';
    $('controls').hidden = true; $('hint').hidden = true; $('sec-window').hidden = true;
    return;
  }

  function readColors() {
    var cs = getComputedStyle(document.documentElement);
    function g(n) { return cs.getPropertyValue(n).trim(); }
    C = { ink: g('--ink'), ink2: g('--ink-2'), ink3: g('--ink-3'), rule: g('--rule'), hair: g('--hair'),
      wash: g('--wash'), wash2: g('--wash-2'), paper: g('--paper'), other: g('--c-other'), focus: g('--focus'),
      serif: g('--serif'), mono: g('--mono'), seq: [] };
    for (var k = 0; k <= 6; k++) C.seq.push(g('--seq-' + k));
    var slots = []; for (var j = 1; j <= 7; j++) slots.push(g('--c' + j));
    chCol = chans.map(function (c) { return c.slot >= 0 && c.slot < slots.length ? slots[c.slot] : C.other; });
  }

  // ---------------------------------------------------------------- formatting
  var MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function fmtN(n) { return Math.round(n).toLocaleString('en-US'); }
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function fmtDate(ms) { var d = new Date(ms); return d.getUTCDate() + ' ' + MON[d.getUTCMonth()] + ' ' + d.getUTCFullYear(); }
  function fmtTime(ms) { var d = new Date(ms); return pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes()); }
  function fmtDateTime(ms) { return fmtDate(ms) + ', ' + fmtTime(ms) + ' UTC'; }
  function isoS(ms) { return new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z'); }
  function dayOf(ms) { return DAYS ? DAYS.of(ms) : null; }
  function dayRange(a, b) {
    if (!DAYS) return '';
    var x = DAYS.of(a), y = DAYS.of(b);
    return x === y ? 'Day ' + x : 'Days ' + x + '–' + y;
  }
  function dateRange(a, b) {
    var x = new Date(a), y = new Date(b);
    if (fmtDate(a) === fmtDate(b)) return fmtDate(a);
    if (x.getUTCFullYear() === y.getUTCFullYear()) {
      if (x.getUTCMonth() === y.getUTCMonth()) return x.getUTCDate() + '–' + fmtDate(b);
      return x.getUTCDate() + ' ' + MON[x.getUTCMonth()] + ' – ' + fmtDate(b);
    }
    return fmtDate(a) + ' – ' + fmtDate(b);
  }
  function whenRange(a, b) {  // writers think in Village days; dates follow
    var d = dayRange(a, b), t = dateRange(a, b);
    return d ? d + ' (' + t + ')' : t;
  }
  function durLabel(ms) {
    var units = [[28 * 864e5, '4 weeks'], [14 * 864e5, '2 weeks'], [7 * 864e5, 'week'], [2 * 864e5, '2 days'], [864e5, 'day'],
      [432e5, '12 hours'], [216e5, '6 hours'], [108e5, '3 hours'], [72e5, '2 hours'], [36e5, 'hour'], [18e5, '30 minutes'],
      [9e5, '15 minutes'], [3e5, '5 minutes'], [6e4, 'minute']];
    for (var k = 0; k < units.length; k++) if (Math.abs(ms - units[k][0]) < 1) return units[k][1];
    if (ms % 864e5 === 0) return (ms / 864e5) + ' days';
    if (ms % 36e5 === 0) return (ms / 36e5) + ' hours';
    return Math.round(ms / 6e4) + ' minutes';
  }
  function fmtC(n) { return n < 1000 ? String(Math.round(n)) : n < 1e4 ? (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'k' : Math.round(n / 1e3) + 'k'; }
  function fmtK(n) { return n >= 1e4 ? (n / 1e3).toFixed(n >= 1e5 ? 0 : 1).replace(/\.0$/, '') + 'k' : fmtN(n); }
  function plural(n, w) { return fmtN(n) + ' ' + w + (Math.round(n) === 1 ? '' : 's'); }
  function evid(i) { return D.idp + D.id[i]; }
  function laneOfRow(i) { return i < NL ? D.au[i] : -1; }

  // ---------------------------------------------------------------- row order
  var LAB_ORDER = ['Anthropic', 'OpenAI', 'Google', 'Other'];
  function rowsFor(order) {
    var idx = lanes.map(function (_, i) { return i; });
    if (order === 'n') idx.sort(function (a, b) { return lanes[b].n - lanes[a].n || a - b; });
    else idx.sort(function (a, b) { return lanes[a].first - lanes[b].first || a - b; });
    if (order !== 'lab' || !lanes.some(function (l) { return l.labg; })) return idx.map(function (li) { return { li: li }; });
    var out = [];
    LAB_ORDER.concat([null]).forEach(function (g) {
      var group = idx.filter(function (li) { return (lanes[li].labg || null) === g; });
      if (!group.length) return;
      out.push({ hdr: g === null ? 'Lab unknown' : (g === 'Other' ? 'Other labs' : g) });
      group.forEach(function (li) { out.push({ li: li }); });
    });
    return out;
  }

  // ---------------------------------------------------------------- ticks
  var STEPS = [6e4, 3e5, 9e5, 18e5, 36e5, 108e5, 216e5, 432e5, 864e5, 1728e5, 6048e5, 12096e5];
  var MONTHS = [1, 2, 3, 6, 12, 24, 60];
  function dateTicks(a, b, pw, minGap) {
    var target = Math.max(2, Math.floor(pw / minGap)), raw = (b - a) / target, out = [], t, k;
    for (k = 0; k < STEPS.length; k++) {
      var step = STEPS[k];
      if (step < raw) continue;
      var off = step === 6048e5 || step === 12096e5 ? 4 * 864e5 : 0;  // weeks start on Monday
      for (t = Math.ceil((a - off) / step) * step + off; t <= b; t += step) out.push(t);
      return out.map(function (t, j) {
        var d = new Date(t), lbl;
        if (step >= 864e5) {
          lbl = d.getUTCDate() + ' ' + MON[d.getUTCMonth()];
          var prev = j ? new Date(out[j - 1]) : null;
          if (!prev || prev.getUTCFullYear() !== d.getUTCFullYear()) lbl += ' ' + d.getUTCFullYear();
        } else if (d.getUTCHours() === 0 && d.getUTCMinutes() === 0) lbl = d.getUTCDate() + ' ' + MON[d.getUTCMonth()];
        else lbl = pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes());
        return { t: t, lbl: lbl };
      });
    }
    var rawM = raw / (30.44 * 864e5), m = MONTHS[MONTHS.length - 1];
    for (k = 0; k < MONTHS.length; k++) if (MONTHS[k] >= rawM) { m = MONTHS[k]; break; }
    var d0 = new Date(a), y = d0.getUTCFullYear(), mo = d0.getUTCMonth(), lastY = null;
    for (var guard = 0; guard < 3000; guard++) {
      t = Date.UTC(y, mo, 1);
      if (t > b) break;
      if (t >= a && ((y * 12 + mo) % m === 0)) {
        var lb = m >= 12 ? String(y) : MON[mo] + (lastY !== y ? ' ' + y : '');
        out.push({ t: t, lbl: lb }); lastY = y;
      }
      mo++; if (mo === 12) { mo = 0; y++; }
    }
    return out;
  }
  var DAY_STEPS = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000];
  function dayTicks(a, b, pw, minGap) {
    if (!DAYS) return [];
    var d0 = DAYS.of(a), d1 = DAYS.of(b), perDay = pw / Math.max(1, (b - a) / 864e5), step = DAY_STEPS[DAY_STEPS.length - 1];
    for (var k = 0; k < DAY_STEPS.length; k++) if (DAY_STEPS[k] * perDay >= minGap) { step = DAY_STEPS[k]; break; }
    var out = [];
    for (var n = Math.ceil(d0 / step) * step; n <= d1 + 1; n += step) {
      if (n < 1) continue;
      var t = DAYS.start(n);
      if (t >= a && t <= b) out.push({ t: t, lbl: String(n) });
    }
    return out;
  }

  // ---------------------------------------------------------------- painters (canvas and SVG share the drawing code)
  function fontOf(o) { return (o.style || '') + ' ' + (o.weight || 400) + ' ' + o.size + 'px ' + (o.family || C.serif); }
  var measureCtx = document.createElement('canvas').getContext('2d');
  function measure(s, o) { measureCtx.font = fontOf(o); return measureCtx.measureText(s).width; }
  function CanvasPainter(ctx) {
    return {
      rect: function (x, y, w, h, fill, alpha) { ctx.globalAlpha = alpha == null ? 1 : alpha; ctx.fillStyle = fill; ctx.fillRect(x, y, w, h); ctx.globalAlpha = 1; },
      strokeRect: function (x, y, w, h, stroke, lw) { ctx.strokeStyle = stroke; ctx.lineWidth = lw || 1; ctx.strokeRect(x, y, w, h); },
      line: function (x1, y1, x2, y2, stroke, lw, alpha) {
        ctx.globalAlpha = alpha == null ? 1 : alpha; ctx.strokeStyle = stroke; ctx.lineWidth = lw || 1;
        ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke(); ctx.globalAlpha = 1;
      },
      tri: function (x, y, s, fill) { ctx.fillStyle = fill; ctx.beginPath(); ctx.moveTo(x - s, y - s); ctx.lineTo(x + s, y - s); ctx.lineTo(x, y + s * 0.6); ctx.closePath(); ctx.fill(); },
      text: function (s, x, y, o) {
        ctx.font = fontOf(o); ctx.fillStyle = o.fill || C.ink; ctx.textAlign = o.align || 'left'; ctx.textBaseline = 'alphabetic';
        if (o.rotate) { ctx.save(); ctx.translate(x, y); ctx.rotate(o.rotate); ctx.fillText(s, 0, 0); ctx.restore(); } else ctx.fillText(s, x, y);
      },
      clip: function (x, y, w, h) { ctx.save(); ctx.beginPath(); ctx.rect(x, y, w, h); ctx.clip(); },
      unclip: function () { ctx.restore(); }
    };
  }
  function SvgPainter(W, H) {
    var svg = document.createElementNS(SVGNS, 'svg');
    svg.setAttribute('class', 'viz');
    svg.setAttribute('width', W); svg.setAttribute('height', H); svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    var cur = svg, stack = [], clipN = 0;
    function el(tag, attrs) {
      var e = document.createElementNS(SVGNS, tag);
      for (var k in attrs) if (attrs[k] != null) e.setAttribute(k, attrs[k]);
      cur.appendChild(e); return e;
    }
    function r(v) { return Math.round(v * 100) / 100; }
    return {
      svg: svg,
      rect: function (x, y, w, h, fill, alpha) { el('rect', { x: r(x), y: r(y), width: r(Math.max(0, w)), height: r(Math.max(0, h)), fill: fill, 'fill-opacity': alpha == null || alpha === 1 ? null : alpha }); },
      strokeRect: function (x, y, w, h, stroke, lw) { el('rect', { x: r(x), y: r(y), width: r(w), height: r(h), fill: 'none', stroke: stroke, 'stroke-width': lw || 1 }); },
      line: function (x1, y1, x2, y2, stroke, lw, alpha) { el('line', { x1: r(x1), y1: r(y1), x2: r(x2), y2: r(y2), stroke: stroke, 'stroke-width': lw || 1, 'stroke-opacity': alpha == null || alpha === 1 ? null : alpha }); },
      tri: function (x, y, s, fill) { el('path', { d: 'M' + r(x - s) + ',' + r(y - s) + 'L' + r(x + s) + ',' + r(y - s) + 'L' + r(x) + ',' + r(y + s * 0.6) + 'Z', fill: fill }); },
      text: function (s, x, y, o) {
        var t = el('text', { x: r(x), y: r(y), 'font-family': o.family || C.serif, 'font-size': o.size, 'font-weight': o.weight && o.weight !== 400 ? o.weight : null,
          'font-style': o.style || null, fill: o.fill || C.ink, 'text-anchor': o.align === 'center' ? 'middle' : (o.align === 'right' ? 'end' : null),
          transform: o.rotate ? 'rotate(' + (o.rotate * 180 / Math.PI) + ' ' + r(x) + ' ' + r(y) + ')' : null });
        t.textContent = s;
      },
      clip: function (x, y, w, h) {
        var id = 'tlclip' + (++clipN) + '-' + Math.random().toString(36).slice(2, 7);
        var cp = el('clipPath', { id: id });
        var rc = document.createElementNS(SVGNS, 'rect');
        rc.setAttribute('x', r(x)); rc.setAttribute('y', r(y)); rc.setAttribute('width', r(w)); rc.setAttribute('height', r(h));
        cp.appendChild(rc);
        var g = el('g', { 'clip-path': 'url(#' + id + ')' });
        stack.push(cur); cur = g;
      },
      unclip: function () { cur = stack.pop() || svg; }
    };
  }
  function fit(s, maxW, o) {
    if (maxW <= 4) return '';
    if (measure(s, o) <= maxW) return s;
    while (s.length > 1 && measure(s + '…', o) > maxW) s = s.slice(0, -1);
    return s.length > 1 ? s + '…' : '';
  }

  // ---------------------------------------------------------------- layout
  function sizes(W, exp) {
    var narrow = W < 560;
    return {
      fs: exp ? 12.5 : (narrow ? 12 : 13), ts: exp ? 11.5 : (narrow ? 11 : 11.5),
      laneH: exp ? 16 : (narrow ? 22 : 26), hdrH: exp ? 15 : 18, axH: exp ? 20 : 24, ribH: exp ? 13 : 17, noteH: exp ? 12 : 14,
      minBar: exp ? 3 : 5, tickGap: exp ? 56 : (narrow ? 58 : 84), narrow: narrow
    };
  }
  function layout(W, exp) {
    var z = sizes(W, exp), g = { W: W, z: z, exp: exp };
    var nameW = 0, cntW = 0;
    lanes.forEach(function (l) {
      nameW = Math.max(nameW, measure(l.name, { size: z.fs, weight: 400 }));
      cntW = Math.max(cntW, measure(fmtN(l.n), { size: z.ts }));
    });
    g.showCounts = !exp && !z.narrow;
    var want = nameW + 14 + (g.showCounts ? cntW + 12 : 0);
    g.labelW = Math.round(Math.max(z.narrow ? 92 : 120, Math.min(want, W * (z.narrow ? 0.34 : 0.27), 260)));
    g.padR = exp ? 4 : 10;
    g.x0 = g.labelW; g.x1 = W - g.padR; g.pw = Math.max(10, g.x1 - g.x0);
    var y = 0;
    g.showDate = S.axis !== 'day' || !DAYS;
    g.showDay = !!DAYS && S.axis !== 'date';
    if (g.showDate) { g.axTop = y; y += z.axH; }
    g.rib = [];
    periodKinds.slice(0, 2).forEach(function (k) { g.rib.push({ kind: k, y: y + 3 }); y += z.ribH + 4; });
    if (notes.length) { g.noteY = y + 2; y += z.noteH + 4; }
    g.lanesTop = y + 3; y = g.lanesTop;
    g.rows = rowsFor(S.order).map(function (r) {
      var h = r.hdr ? z.hdrH : z.laneH;
      var o = { li: r.li, hdr: r.hdr, y: y, h: h }; y += h; return o;
    });
    g.lanesBot = y;
    if (g.showDay) { g.axBot = y + 1; y += z.axH + 1; }
    g.H = Math.ceil(y + 2);
    return g;
  }

  // ---------------------------------------------------------------- binned counts
  var NICE = [6e4, 3e5, 9e5, 18e5, 36e5, 108e5, 216e5, 432e5, 864e5, 6048e5, 12096e5, 24192e5, 48384e5];
  function dispBin(v0, v1, pw, minBar) {
    var k = pw / (v1 - v0);
    var cands = [BIN].concat(NICE.filter(function (c) { return c > BIN && c % BIN === 0; }));
    for (var j = 0; j < cands.length; j++) if (cands[j] * k >= minBar) return cands[j];
    return cands[cands.length - 1] * Math.ceil(minBar / (cands[cands.length - 1] * k));
  }
  function alignOf(disp) { return disp % 6048e5 === 0 ? 4 * 864e5 : 0; }
  // per lane: { di -> {tot, per[]} } for the bins overlapping [v0, v1], visible channels only
  function binned(v0, v1, disp) {
    var al = alignOf(disp), res = [], max = 0, tot = 0, lmax = [];
    for (var li = 0; li < lanes.length; li++) {
      var a = D.dens.lanes[li] || [], m = {}, mx = 0;
      for (var j = 0; j < a.length; j += 3) {
        var t = BASE + a[j] * BIN, ch = a[j + 1], c = a[j + 2];
        if (t + BIN <= v0 || t >= v1 || S.hidden[ch]) continue;
        var di = Math.floor((t - al) / disp), e = m[di];
        if (!e) { e = m[di] = { tot: 0, per: {} }; }
        e.tot += c; e.per[ch] = (e.per[ch] || 0) + c; tot += c;
        if (e.tot > mx) mx = e.tot;
      }
      res.push(m); lmax.push(mx);
      if (mx > max) max = mx;
    }
    return { lanes: res, max: max, lmax: lmax, tot: tot, disp: disp, al: al };
  }
  function visibleTrueCount(v0, v1) {
    var n = 0;
    for (var li = 0; li < lanes.length; li++) {
      var a = D.dens.lanes[li] || [];
      for (var j = 0; j < a.length; j += 3) {
        var t = BASE + a[j] * BIN;
        if (t + BIN <= v0 || t >= v1 || S.hidden[a[j + 1]]) continue;
        n += a[j + 2] * Math.min(1, (Math.min(v1, t + BIN) - Math.max(v0, t)) / BIN);
      }
    }
    return n;
  }
  function effectiveMode(v0, v1, pw) {
    if (S.mode !== 'auto') return S.mode;
    return visibleTrueCount(v0, v1) / (lanes.length * pw) < 0.22 ? 'msgs' : 'bins';
  }
  function lowerBound(a, lo, hi, x) { while (lo < hi) { var m = (lo + hi) >> 1; if (a[m] < x) lo = m + 1; else hi = m; } return lo; }

  // ---------------------------------------------------------------- drawing (shared by screen and export)
  function drawAll(P, g, v0, v1, info) {
    var z = g.z, k = g.pw / (v1 - v0);
    function X(t) { return g.x0 + (t - v0) * k; }
    var lab = { size: z.fs }, tickO = { size: z.ts, fill: C.ink2 }, titleO = { size: z.ts, fill: C.ink3, style: 'italic' };
    P.rect(0, 0, g.W, g.H, C.paper);
    var mode = effectiveMode(v0, v1, g.pw);
    info.mode = mode;
    var dt = g.showDate ? dateTicks(v0, v1, g.pw, z.tickGap) : [];
    var dy = g.showDay ? dayTicks(v0, v1, g.pw, z.tickGap * 0.62) : [];
    // faint verticals at the major ticks of the uppermost time axis, through the lanes
    (dt.length ? dt : dy).forEach(function (tk) {
      var x = Math.round(X(tk.t)) + 0.5;
      if (x > g.x0 && x < g.x1) P.line(x, g.lanesTop, x, g.lanesBot, C.hair, 0.6);
    });
    // top axis (UTC)
    if (g.showDate) {
      var ya = g.axTop + z.axH - 0.5;
      P.line(g.x0, ya, g.x1, ya, C.rule, 0.8);
      P.text('UTC', g.x0 - 8, ya - 6, { size: z.ts, fill: C.ink3, style: 'italic', align: 'right' });
      dt.forEach(function (tk) {
        var x = Math.round(X(tk.t)) + 0.5;
        if (x < g.x0 - 0.5 || x > g.x1 + 0.5) return;
        P.line(x, ya - 3.5, x, ya, C.rule, 0.8);
        var w = measure(tk.lbl, tickO) / 2;
        P.text(tk.lbl, Math.min(Math.max(x, g.x0 + w), g.x1 - w), ya - 6, { size: z.ts, fill: C.ink2, align: 'center' });
      });
    }
    // periods (goals) and caller notes
    g.rib.forEach(function (rb) {
      P.text(rb.kind === 'village_goal' ? 'Goals' : rb.kind.replace(/_/g, ' '), g.x0 - 8, rb.y + z.ribH - 4, { size: z.ts, fill: C.ink3, style: 'italic', align: 'right' });
      P.clip(g.x0, rb.y - 1, g.pw, z.ribH + 2);
      var n = 0;
      periods.forEach(function (p) {
        if (p.kind !== rb.kind) return;
        var e = p.e == null ? D.end : p.e;
        n++;
        if (e < v0 || p.s > v1) return;
        var xa = X(p.s), xb = X(e);
        var sel = p.i === S.period;
        P.rect(xa + 0.5, rb.y, Math.max(1, xb - xa - 1), z.ribH, n % 2 ? C.wash2 : C.wash);
        if (sel) P.strokeRect(xa + 1, rb.y + 0.5, Math.max(1, xb - xa - 2), z.ribH - 1, C.ink, 1.4);
        var lo = Math.max(xa, g.x0) + 4, hi = Math.min(xb, g.x1) - 4;
        var txt = fit(p.label, hi - lo, { size: z.ts });
        if (txt !== p.label && txt.length < 10) txt = measure(String(n), { size: z.ts }) <= hi - lo + 4 ? String(n) : '';
        if (txt) P.text(txt, lo - (txt === String(n) ? 2 : 0), rb.y + z.ribH - 4, { size: z.ts, fill: C.ink2 });
      });
      P.unclip();
    });
    if (notes.length) {
      P.text('Events', g.x0 - 8, g.noteY + z.noteH - 3, { size: z.ts, fill: C.ink3, style: 'italic', align: 'right' });
      notes.forEach(function (nt, j) {
        var xa = X(nt.s), ty = g.noteY + z.noteH * 0.4, s3 = z.noteH * 0.34;
        if (nt.e != null && nt.e > nt.s) {  // a span: a thin rule along the bottom of the row, from start to end
          var xb = X(nt.e);
          if (xb >= g.x0 && xa <= g.x1) {
            var lo = Math.max(xa, g.x0), hi = Math.min(xb, g.x1);
            P.rect(lo, g.noteY + z.noteH - 1.6, hi - lo, 1.6, C.ink);
            if (xb <= g.x1) P.rect(xb - 0.8, g.noteY + z.noteH * 0.45, 1.6, z.noteH * 0.55, C.ink);
          }
        }
        if (xa < g.x0 || xa > g.x1) return;
        P.tri(xa, ty, s3, C.ink);
        P.text(String(j + 1), xa - s3 - 2, ty + s3 * 0.6, { size: z.ts - 1, fill: C.ink2, align: 'right' });
      });
    }
    // rows
    var bn = mode === 'bins' ? binned(v0, v1, dispBin(v0, v1, g.pw, z.minBar)) : null;
    info.bins = bn;
    var barMax = z.laneH - (g.exp ? 3 : 6);
    g.rows.forEach(function (r) {
      if (r.hdr) {
        P.text(r.hdr.toUpperCase(), 0, r.y + r.h - 4, { size: z.ts - 0.5, fill: C.ink3 });
        return;
      }
      var L = lanes[r.li], base = r.y + r.h - 2;
      if (r.li === S.sel) P.rect(0, r.y, g.W, r.h, C.wash);
      var nameO = { size: z.fs, style: L.kind === 'human' ? 'italic' : '', fill: L.kind === 'external' ? C.ink2 : C.ink, weight: r.li === S.sel ? 700 : 400 };
      var cnt = g.showCounts ? fmtN(L.n) : '', cw = cnt ? measure(cnt, tickO) + 12 : 0;
      var nm = L.name + (L.kind === 'human' ? ' (human)' : L.kind === 'external' ? ' (external)' : '');
      P.text(fit(nm, g.labelW - 12 - cw, nameO), 0, base - 3 - (r.h - z.fs) / 4, nameO);
      if (cnt) P.text(cnt, g.labelW - 10, base - 3 - (r.h - z.fs) / 4, { size: z.ts, fill: C.ink3, align: 'right' });
      P.line(g.x0, base + 0.5, g.x1, base + 0.5, C.hair, 0.6);
      P.clip(g.x0, r.y, g.pw, r.h);
      if (bn) {
        var m = bn.lanes[r.li], bw = bn.disp * k, gap = bw >= 5 ? 1 : 0, scaleMax = S.scale === 'row' ? (bn.lmax[r.li] || 1) : bn.max;
        for (var di in m) {
          var e = m[di], xb = X(bn.al + di * bn.disp);
          if (xb + bw < g.x0 || xb > g.x1) continue;
          var yb = base;
          for (var ch = 0; ch < chans.length; ch++) {
            var c = e.per[ch];
            if (!c) continue;
            var h = c / scaleMax * barMax;
            P.rect(xb + gap / 2, yb - h, Math.max(0.8, bw - gap), h, chCol[ch]);
            yb -= h;
          }
        }
      } else {
        var lo = lowerBound(T, L.a, L.b, v0 - 3 / k), hi = lowerBound(T, lo, L.b, v1 + 3 / k), lastX = -1e9, lastC = -1;
        var th = Math.min(barMax, g.exp ? 10 : 16);
        for (var j = lo; j < hi; j++) {
          var cc = D.c[j];
          if (S.hidden[cc]) continue;
          var mx = Math.round(X(T[j]) * 2) / 2;
          if (mx - lastX < 0.5 && cc === lastC) continue;
          lastX = mx; lastC = cc;
          P.rect(mx - 0.75, base - th, 1.5, th, chCol[cc]);
        }
      }
      P.unclip();
    });
    // left rule of the plot and bottom axis (Village days)
    P.line(g.x0 - 0.5, g.lanesTop, g.x0 - 0.5, g.lanesBot, C.rule, 0.8);
    if (g.showDay) {
      var yb2 = g.axBot + 0.5;
      P.line(g.x0, yb2, g.x1, yb2, C.rule, 0.8);
      P.text('Village day', g.x0 - 8, yb2 + z.ts + 3, titleO.align ? titleO : { size: z.ts, fill: C.ink3, style: 'italic', align: 'right' });
      if (!dy.length) {  // zoomed inside one Village day: name it instead of leaving the axis bare
        var lbl = dayRange(Math.max(v0, D.start), Math.min(v1, D.end));
        P.text(lbl, g.x0 + g.pw / 2, yb2 + z.ts + 3, { size: z.ts, fill: C.ink2, align: 'center' });
      }
      dy.forEach(function (tk) {
        var x = Math.round(X(tk.t)) + 0.5;
        if (x < g.x0 - 0.5 || x > g.x1 + 0.5) return;
        P.line(x, yb2, x, yb2 + 3.5, C.rule, 0.8);
        var w = measure(tk.lbl, tickO) / 2;
        P.text(tk.lbl, Math.min(Math.max(x, g.x0 + w), g.x1 - w), yb2 + z.ts + 3, { size: z.ts, fill: C.ink2, align: 'center' });
      });
    }
    return info;
  }

  // ---------------------------------------------------------------- screen
  var plot = $('plot'), cv = $('cv'), ov = $('ov');
  var ctx = cv.getContext('2d'), octx = ov.getContext('2d');
  var G = null, W = 0, dpr = 1, lastInfo = {};
  var tip = $('tip'), statusEl = $('status');
  function resize() {
    dpr = window.devicePixelRatio || 1;
    W = Math.max(300, plot.clientWidth);
    G = layout(W, false);
    plot.style.height = G.H + 'px';
    [cv, ov].forEach(function (c) { c.width = Math.round(W * dpr); c.height = Math.round(G.H * dpr); c.style.width = W + 'px'; c.style.height = G.H + 'px'; });
    draw();
  }
  function draw() {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    lastInfo = drawAll(CanvasPainter(ctx), G, S.v0, S.v1, {});
    drawHover();
    updateText();
  }
  var raf = 0;
  function redraw() { if (!raf) raf = requestAnimationFrame(function () { raf = 0; draw(); scheduleWindow(); }); }
  function xOf(t) { return G.x0 + (t - S.v0) * G.pw / (S.v1 - S.v0); }
  function tOf(x) { return S.v0 + (x - G.x0) * (S.v1 - S.v0) / G.pw; }

  function drawHover() {
    octx.setTransform(dpr, 0, 0, dpr, 0, 0);
    octx.clearRect(0, 0, W, G.H);
    var h = S.hover;
    if (!h) return;
    octx.strokeStyle = C.ink; octx.lineWidth = 1.2;
    if (h.type === 'bin') {
      var x = xOf(h.t0), w = Math.max(2, xOf(h.t1) - x);
      octx.strokeRect(x - 0.5, h.row.y + 1.5, w + 1, h.row.h - 2.5);
    } else if (h.type === 'msg') {
      var xm = Math.round(xOf(T[h.i]) * 2) / 2;
      octx.strokeRect(xm - 3.5, h.row.y + 2.5, 7, h.row.h - 4.5);
    } else if (h.type === 'period') {
      var p = periods[h.p], e = p.e == null ? D.end : p.e, rb = G.rib[h.rib];
      octx.strokeRect(xOf(p.s) + 1, rb.y + 0.5, Math.max(1, xOf(e) - xOf(p.s) - 2), G.z.ribH - 1);
    }
  }

  function updateText() {
    var a = Math.max(S.v0, D.start), b = Math.min(S.v1, D.end);
    $('view').textContent = 'View: ' + whenRange(a, b) + (lastInfo.mode === 'msgs' ? ' · single messages' : (lastInfo.bins ? ' · messages per ' + durLabel(lastInfo.bins.disp) : ''));
    $('cap-tl').textContent = captionTimeline();
  }

  // ---------------------------------------------------------------- captions
  function orderText() { return S.order === 'n' ? 'by message count' : S.order === 'lab' ? 'by lab, then first message' : 'by first message'; }
  function captionTimeline(forExport) {
    var a = Math.max(S.v0, D.start), b = Math.min(S.v1, D.end), info = lastInfo, parts = [];
    var head = 'Figure 1: When each agent was active, ' + whenRange(a, b) + '. ';
    parts.push('Rows are the ' + (lanes.length === 1 ? 'agent' : lanes.length + ' agents') + ' with the most messages' +
      (META.n_agents > lanes.length ? ' (of ' + fmtN(META.n_agents) + ')' : '') + ', ordered ' + orderText() + '.');
    if (info.mode === 'msgs') parts.push('Each tick is one message, coloured by channel.');
    else if (info.bins) parts.push('Bars count each agent’s messages per ' + durLabel(info.bins.disp) + ' (UTC), stacked by channel, ' +
      (S.scale === 'row' ? 'each row scaled to its own busiest ' + durLabel(info.bins.disp) + (G && G.showCounts ? ' (the number left of a row is its total).' : '.')
        : 'on one scale for all rows (tallest bar: ' + plural(info.bins.max, 'message') + ').'));
    var hid = chans.filter(function (_, i) { return S.hidden[i]; }).map(function (c) { return '#' + c.name; });
    if (hid.length) parts.push('Hidden channels: ' + hid.join(', ') + '.');
    if (periods.length) parts.push('The band on top marks the ' + plural(periods.filter(function (p) { return p.kind === periodKinds[0]; }).length, periodKinds[0] === 'village_goal' ? 'village goal' : 'period') + '.');
    if (notes.length) parts.push('Triangles mark ' + plural(notes.length, 'annotated event') + ' (numbered as listed below the figure).');
    if (DAYS) parts.push('Village day 1 is ' + D.days.day_one + ' (' + D.days.tz + ').');
    if (META.sampled) parts.push('Counts include all ' + fmtN(META.lane_total) + ' messages by these agents; ' + fmtN(META.marks) + ' of them (' + Math.round(100 * META.marks / Math.max(1, META.lane_total)) + '%, an even sample per agent) are kept for hovering and reading' + (info.mode === 'msgs' ? ', so the ticks show that sample.' : '.'));
    var ex = [];
    if (META.humans) ex.push(fmtN(META.humans) + ' by humans');
    if (META.external) ex.push(fmtN(META.external) + ' by external or unknown actors');
    if (META.other_msgs) ex.push(fmtN(META.other_msgs) + ' by ' + plural(Math.max(META.n_agents - lanes.length, 0), 'other agent'));
    if (ex.length) parts.push('Not drawn: ' + ex.join(', ') + (META.undated ? '; ' + fmtN(META.undated) + ' undated' : '') + '.');
    var s = head + parts.join(' ');
    if (!forExport && notes.length) s += ' Annotated events: ' + notes.map(function (n, j) { return (j + 1) + ' ' + n.label; }).join('; ') + '.';
    return s;
  }

  // ---------------------------------------------------------------- hit testing & tooltip
  function rowAt(y) { for (var j = 0; j < G.rows.length; j++) { var r = G.rows[j]; if (y >= r.y && y < r.y + r.h) return r; } return null; }
  function hit(mx, my) {
    if (my >= G.lanesTop && my < G.lanesBot) {
      var r = rowAt(my);
      if (!r || r.hdr) return null;
      if (mx < G.x0) return { type: 'lane', row: r, li: r.li };
      if (mx > G.x1) return null;
      var t = tOf(mx);
      if (lastInfo.mode === 'bins' && lastInfo.bins) {
        var bn = lastInfo.bins, di = Math.floor((t - bn.al) / bn.disp), e = bn.lanes[r.li][di];
        if (!e) return null;
        return { type: 'bin', row: r, li: r.li, di: di, e: e, t0: bn.al + di * bn.disp, t1: bn.al + (di + 1) * bn.disp };
      }
      var L = lanes[r.li], k = G.pw / (S.v1 - S.v0), rad = 6 / k, best = -1, bd = Infinity, j0 = lowerBound(T, L.a, L.b, t - rad);
      for (var j = j0; j < L.b && T[j] <= t + rad; j++) {
        if (S.hidden[D.c[j]]) continue;
        var d = Math.abs(T[j] - t); if (d < bd) { bd = d; best = j; }
      }
      return best < 0 ? null : { type: 'msg', row: r, li: r.li, i: best };
    }
    for (var q = 0; q < G.rib.length; q++) {
      var rb = G.rib[q];
      if (my >= rb.y - 1 && my <= rb.y + G.z.ribH + 1 && mx >= G.x0 && mx <= G.x1) {
        var tt = tOf(mx);
        for (var p = 0; p < periods.length; p++) {
          var P0 = periods[p], e2 = P0.e == null ? D.end : P0.e;
          if (P0.kind === rb.kind && P0.s <= tt && tt < e2) return { type: 'period', p: p, rib: q };
        }
      }
    }
    if (notes.length && my >= G.noteY - 2 && my <= G.noteY + G.z.noteH + 2) {
      for (var n2 = 0; n2 < notes.length; n2++) if (Math.abs(xOf(notes[n2].s) - mx) < 7) return { type: 'note', n: n2 };
    }
    return null;
  }

  var Tip = {
    clear: function () { tip.textContent = ''; return this; },
    add: function (cls, text) { var d = document.createElement('div'); d.className = cls; d.textContent = text; tip.appendChild(d); return d; },
    row: function (value, label, color) {
      var r = document.createElement('div'); r.className = 'tt-row';
      var key = document.createElement('span'); key.style.cssText = 'display:inline-block;width:12px;height:3px;flex:none;background:' + (color || 'transparent');
      var v = document.createElement('strong'); v.textContent = value;
      var l = document.createElement('span'); l.textContent = label;
      r.appendChild(key); r.appendChild(v); r.appendChild(l); tip.appendChild(r); return this;
    },
    show: function (cx, cy) {
      tip.hidden = false;
      var tw = tip.offsetWidth, th = tip.offsetHeight, x = cx + 14, y = cy + 14;
      if (x + tw > window.innerWidth - 8) x = Math.max(8, cx - tw - 14);
      if (y + th > window.innerHeight - 8) y = Math.max(8, cy - th - 14);
      tip.style.left = x + 'px'; tip.style.top = y + 'px';
    },
    hide: function () { tip.hidden = true; }
  };
  function showHit(h, cx, cy) {
    Tip.clear();
    if (h.type === 'bin') {
      var L = lanes[h.li];
      Tip.add('tt-title', L.name);
      Tip.add('tt-sub', whenRange(h.t0, h.t1 - 1));
      var keys = Object.keys(h.e.per).map(Number).sort(function (a, b) { return h.e.per[b] - h.e.per[a]; });
      Tip.row(fmtN(h.e.tot), h.e.tot === 1 ? 'message' : 'messages', null);
      keys.forEach(function (ch) { Tip.row(fmtN(h.e.per[ch]), '#' + chans[ch].name, chCol[ch]); });
      Tip.add('tt-note', 'Click to zoom into this ' + durLabel(h.t1 - h.t0) + '.');
    } else if (h.type === 'msg') {
      var i = h.i, Lm = lanes[h.li], ch2 = chans[D.c[i]];
      Tip.add('tt-id', evid(i));
      Tip.add('tt-title', Lm.name + '  ·  #' + ch2.name);
      Tip.add('tt-sub', (DAYS ? 'Day ' + dayOf(T[i]) + ' · ' : '') + fmtDateTime(T[i]));
      if (D.s[i]) Tip.add('tt-quote', D.s[i]);
      Tip.add('tt-note', fmtN(D.len[i]) + ' characters · masked, truncated, untrusted agent output · click to copy the id and read the thread');
    } else if (h.type === 'lane') {
      var La = lanes[h.li];
      Tip.add('tt-title', La.name);
      if (La.lab) Tip.add('tt-sub', La.lab);
      Tip.row(fmtN(La.n), 'messages in this render', null);
      if (lastInfo.bins) Tip.row(fmtN(lastInfo.bins.lmax[h.li] || 0), 'in its busiest ' + durLabel(lastInfo.bins.disp) + ' on screen', null);
      Tip.add('tt-note', 'First message ' + (DAYS ? 'Day ' + dayOf(La.first) + ', ' : '') + fmtDate(La.first) + '. Click to highlight the row.');
    } else if (h.type === 'period') {
      var p = periods[h.p], e = p.e == null ? D.end : p.e, same = periods.filter(function (q) { return q.kind === p.kind; });
      Tip.add('tt-title', (p.kind === 'village_goal' ? 'Goal ' : 'Period ') + (same.indexOf(p) + 1) + ' of ' + same.length);
      Tip.add('tt-quote', p.label || '(no label)');
      Tip.add('tt-sub', whenRange(p.s, e) + (p.e == null ? ', still running' : ''));
      Tip.add('tt-note', 'Click to zoom to it. Evidence id ' + p.id);
    } else if (h.type === 'note') {
      var nt = notes[h.n];
      Tip.add('tt-title', 'Event ' + (h.n + 1));
      Tip.add('tt-quote', nt.label || '');
      Tip.add('tt-sub', whenRange(nt.s, nt.e || nt.s));
    }
    Tip.show(cx, cy);
  }

  // ---------------------------------------------------------------- interaction
  var MIN_SPAN = 30e3, MAX_SPAN = (FULL1 - FULL0) * 1.2;
  function clampView() {
    var s = S.v1 - S.v0;
    if (s < MIN_SPAN) { var m = (S.v0 + S.v1) / 2; S.v0 = m - MIN_SPAN / 2; S.v1 = m + MIN_SPAN / 2; s = MIN_SPAN; }
    if (s > MAX_SPAN) { S.v0 = FULL0 - (MAX_SPAN - (FULL1 - FULL0)) / 2; S.v1 = S.v0 + MAX_SPAN; return; }
    var lo = FULL0 - s * 0.25, hi = FULL1 + s * 0.25;
    if (S.v0 < lo) { S.v0 = lo; S.v1 = lo + s; }
    if (S.v1 > hi) { S.v1 = hi; S.v0 = hi - s; }
  }
  function setView(a, b, keepPeriod) { S.v0 = a; S.v1 = b; if (!keepPeriod) S.period = -1; clampView(); redraw(); }
  function zoomAt(px, f) {
    var t = tOf(px); S.period = -1;
    S.v0 = t - (t - S.v0) * f; S.v1 = t + (S.v1 - t) * f; clampView(); redraw();
  }
  function local(ev) { var r = ov.getBoundingClientRect(); return [ev.clientX - r.left, ev.clientY - r.top]; }

  ov.addEventListener('wheel', function (ev) {
    ev.preventDefault();
    var pt = local(ev), dy = ev.deltaMode === 1 ? ev.deltaY * 16 : ev.deltaY;
    if (Math.abs(ev.deltaX) > Math.abs(dy)) { var sh = ev.deltaX * (S.v1 - S.v0) / G.pw; S.v0 += sh; S.v1 += sh; S.period = -1; clampView(); redraw(); return; }
    zoomAt(Math.max(G.x0, pt[0]), Math.exp(dy * 0.0015));
  }, { passive: false });

  var drag = null, pointers = {}, pinch = null;
  ov.addEventListener('pointerdown', function (ev) {
    pointers[ev.pointerId] = local(ev);
    var ids = Object.keys(pointers);
    if (ids.length === 2) {
      var a = pointers[ids[0]], b = pointers[ids[1]];
      pinch = { d: Math.abs(a[0] - b[0]) || 1, v0: S.v0, v1: S.v1, mid: (a[0] + b[0]) / 2 };
      drag = null; return;
    }
    if (ev.button !== 0) return;
    var pt = local(ev);
    drag = { x: pt[0], y: pt[1], v0: S.v0, v1: S.v1, moved: false };
    ov.setPointerCapture(ev.pointerId);
  });
  ov.addEventListener('pointermove', function (ev) {
    var pt = local(ev);
    if (pointers[ev.pointerId]) pointers[ev.pointerId] = pt;
    if (pinch) {
      var ids = Object.keys(pointers);
      if (ids.length === 2) {
        var a = pointers[ids[0]], b = pointers[ids[1]], d = Math.abs(a[0] - b[0]) || 1, f = pinch.d / d;
        var tm = pinch.v0 + (pinch.mid - G.x0) * (pinch.v1 - pinch.v0) / G.pw;
        S.v0 = tm - (tm - pinch.v0) * f; S.v1 = tm + (pinch.v1 - tm) * f; S.period = -1; clampView(); redraw();
      }
      return;
    }
    if (drag) {
      var dx = pt[0] - drag.x;
      if (!drag.moved && Math.abs(dx) > 4) { drag.moved = true; ov.classList.add('grab'); Tip.hide(); S.hover = null; }
      if (drag.moved) { var sh = dx * (drag.v1 - drag.v0) / G.pw; S.v0 = drag.v0 - sh; S.v1 = drag.v1 - sh; S.period = -1; clampView(); redraw(); }
      return;
    }
    var h = hit(pt[0], pt[1]);
    S.hover = h && h.type !== 'lane' && h.type !== 'note' ? h : null;
    ov.classList.toggle('pointer', !!h);
    if (h) showHit(h, ev.clientX, ev.clientY); else Tip.hide();
    drawHover();
  });
  function endPointer(ev) {
    delete pointers[ev.pointerId];
    if (pinch) { if (Object.keys(pointers).length < 2) pinch = null; return; }
    if (!drag) return;
    var moved = drag.moved; drag = null; ov.classList.remove('grab');
    if (moved || ev.type === 'pointercancel') return;
    var pt = local(ev), h = hit(pt[0], pt[1]);
    if (!h) return;
    if (h.type === 'bin') { var pad = (h.t1 - h.t0) * 0.15; setView(h.t0 - pad, h.t1 + pad); }
    else if (h.type === 'msg') { copyId(evid(h.i)); openThread(h.i); }
    else if (h.type === 'period') selectPeriod(h.p);
    else if (h.type === 'lane') { S.sel = S.sel === h.li ? -1 : h.li; redraw(); if (S.sel >= 0) selectAgent(S.sel, true); }
  }
  ov.addEventListener('pointerup', endPointer);
  ov.addEventListener('pointercancel', endPointer);
  ov.addEventListener('pointerleave', function () { if (!drag) { S.hover = null; Tip.hide(); drawHover(); } });
  ov.addEventListener('dblclick', function () { setView(FULL0, FULL1); });
  ov.addEventListener('keydown', function (ev) {
    var s = S.v1 - S.v0;
    if (ev.key === 'ArrowLeft' || ev.key === 'ArrowRight') { ev.preventDefault(); var sh = (ev.key === 'ArrowLeft' ? -0.15 : 0.15) * s; setView(S.v0 + sh, S.v1 + sh); }
    else if (ev.key === '+' || ev.key === '=') { ev.preventDefault(); zoomAt(G.x0 + G.pw / 2, 0.5); }
    else if (ev.key === '-' || ev.key === '_') { ev.preventDefault(); zoomAt(G.x0 + G.pw / 2, 2); }
    else if (ev.key === 'Home') { ev.preventDefault(); setView(FULL0, FULL1); }
  });
  function selectPeriod(p) {
    var P0 = periods[p], e = P0.e == null ? D.end : P0.e, pad = (e - P0.s) * 0.04;
    S.period = p; S.v0 = P0.s - pad; S.v1 = e + pad; clampView(); redraw();
  }
  $('zin').onclick = function () { zoomAt(G.x0 + G.pw / 2, 0.5); };
  $('zout').onclick = function () { zoomAt(G.x0 + G.pw / 2, 2); };
  $('reset').onclick = function () { setView(FULL0, FULL1); };
  function seg(id, key, after) {
    var box = $(id);
    box.addEventListener('click', function (ev) {
      var b = ev.target.closest('button[data-v]'); if (!b) return;
      S[key] = b.getAttribute('data-v');
      Array.prototype.forEach.call(box.querySelectorAll('button'), function (x) { x.setAttribute('aria-pressed', String(x === b)); });
      S.hover = null; Tip.hide(); if (after) after(); else redraw();
    });
  }
  seg('sort', 'order', function () { resize(); scheduleWindow(); });
  seg('axis', 'axis', function () { resize(); });
  seg('mode', 'mode');
  seg('scale', 'scale');
  if (DAYS) $('axis-ctl').hidden = false;
  if (!lanes.some(function (l) { return l.labg; })) $('sort').lastElementChild.hidden = true;

  function copyId(id) {
    PK.copyText(id).then(function (ok) { statusEl.textContent = ok ? 'Copied ' + id : 'Copy blocked; evidence id: ' + id; });
  }

  // ---------------------------------------------------------------- channel keys
  function swatch(color) {
    var s = document.createElementNS(SVGNS, 'svg'); s.setAttribute('width', 12); s.setAttribute('height', 10);
    var r = document.createElementNS(SVGNS, 'rect'); r.setAttribute('x', 0); r.setAttribute('y', 1); r.setAttribute('width', 12); r.setAttribute('height', 8); r.setAttribute('fill', color);
    s.appendChild(r); return s;
  }
  function buildKeys() {
    var keys = $('keys'); keys.textContent = '';
    var t = document.createElement('span'); t.className = 'key-title'; t.textContent = chans.length > 1 ? 'Channels (click to hide):' : 'Channel:';
    keys.appendChild(t);
    chans.forEach(function (c, i) {
      var b = document.createElement('button'); b.type = 'button'; b.className = 'key' + (S.hidden[i] ? ' off' : '');
      b.title = (S.hidden[i] ? 'Show' : 'Hide') + ' #' + c.name;
      b.setAttribute('aria-pressed', String(!S.hidden[i]));
      b.appendChild(swatch(chCol[i]));
      var nm = document.createElement('span'); nm.textContent = '#' + c.name; b.appendChild(nm);
      var n = document.createElement('span'); n.className = 'n'; n.textContent = fmtN(c.n); b.appendChild(n);
      b.onclick = function () { S.hidden[i] = S.hidden[i] ? 0 : 1; S.hover = null; Tip.hide(); buildKeys(); redraw(); };
      keys.appendChild(b);
    });
  }

  // ---------------------------------------------------------------- the window on screen: mentions and activity
  function windowBounds() { return [Math.max(S.v0, D.start), Math.min(S.v1, D.end)]; }
  function mentionMatrix(a, b) {
    var n = lanes.length, M = [], k;
    for (k = 0; k < n; k++) { M.push(new Float64Array(n)); }
    if (!META.sampled) {  // every lane message is on the page: count exactly, by timestamp
      (D.rc || []).forEach(function (r) {
        var i = r[0];
        if (i >= NL || T[i] < a || T[i] >= b || S.hidden[D.c[i]]) return;
        for (var j = 1; j < r.length; j++) M[D.au[i]][r[j]] += 1;
      });
      return M;
    }
    var q = D.ment || [];
    for (k = 0; k < q.length; k += 4) {
      var t = BASE + q[k] * BIN;
      if (t + BIN <= a || t >= b) continue;
      M[q[k + 1]][q[k + 2]] += q[k + 3];
    }
    return M;
  }
  function svgEl(parent, tag, attrs, text) {
    var e = document.createElementNS(SVGNS, tag);
    for (var k in attrs) if (attrs[k] != null) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    parent.appendChild(e); return e;
  }
  function seqColor(f) {  // 0..1 -> sequential ramp step 1..6 (0 stays paper)
    if (f <= 0) return C.paper;
    return C.seq[Math.min(6, 1 + Math.floor(f * 5.999))];
  }
  function drawMatrix(host, Wd, exp) {
    host.textContent = '';
    var wb = windowBounds(), M = mentionMatrix(wb[0], wb[1]);
    var order = rowsFor(S.order).filter(function (r) { return r.li != null; }).map(function (r) { return r.li; });
    var n = order.length, fs = exp ? 11.5 : (Wd < 560 ? 11 : 12), ts = fs - 0.5;
    var nameW = 0; order.forEach(function (li) { nameW = Math.max(nameW, measure(lanes[li].name, { size: fs })); });
    var labelW = Math.min(nameW + 10, Wd * 0.34), totW = measure('99.9k', { size: ts }) + 10;
    var cell = Math.max(10, Math.min(exp ? 22 : 26, Math.floor((Wd - labelW - totW - 4) / n)));
    var vertical = cell < 21;  // narrow cells: vertical column labels never collide
    var colH = Math.min(nameW, 150) * (vertical ? 1 : 0.72) + 10;
    var x0 = labelW, y0 = colH, H = y0 + n * cell + ts + 12, Wt = x0 + n * cell + totW;
    if (!vertical && n) {  // the last rotated column label must fit inside the figure
      var lastW = Math.min(measure(lanes[order[n - 1]].name, { size: fs }), colH * 1.25);
      Wt = Math.max(Wt, Math.ceil(x0 + (n - 0.5) * cell + lastW * 0.71 + 6));
    }
    var svg = svgEl(host, 'svg', { 'class': 'viz', width: Wt, height: H, viewBox: '0 0 ' + Wt + ' ' + H, role: 'img', 'aria-label': 'Mention matrix for the window on screen' });
    var max = 0, rowT = [], colT = new Float64Array(n), pairs = 0, recip = 0;
    order.forEach(function (ri, r) {
      var s = 0;
      order.forEach(function (ci, c) { var v = M[ri][ci]; s += v; colT[c] += v; if (v > max) max = v; });
      rowT.push(s);
    });
    for (var r = 0; r < n; r++) for (var c = r + 1; c < n; c++) {
      var ab = M[order[r]][order[c]], ba = M[order[c]][order[r]];
      if (ab || ba) { pairs++; if (ab && ba) recip++; }
    }
    order.forEach(function (li, r) {
      var y = y0 + r * cell;
      svgEl(svg, 'text', { x: x0 - 6, y: y + cell / 2 + fs * 0.35, 'text-anchor': 'end', 'class': 'lbl', style: 'font-size:' + fs + 'px' }, fitMx(lanes[li].name, labelW - 8, fs));
      var tx = x0 + r * cell + cell / 2 + (vertical ? fs * 0.35 : 0);
      svgEl(svg, 'text', { x: tx, y: y0 - 5, transform: 'rotate(' + (vertical ? -90 : -45) + ' ' + tx + ' ' + (y0 - 5) + ')', 'class': 'lbl', style: 'font-size:' + fs + 'px' }, fitMx(lanes[li].name, vertical ? colH - 10 : colH * 1.25, fs));
    });
    var cells = svgEl(svg, 'g', {});
    order.forEach(function (ri, r) {
      order.forEach(function (ci, c) {
        var v = M[ri][ci], x = x0 + c * cell, y = y0 + r * cell;
        var attrs = { x: x + 0.5, y: y + 0.5, width: cell - 1, height: cell - 1 };
        if (ri === ci) { attrs.fill = C.wash; svgEl(cells, 'rect', attrs); return; }
        attrs.fill = seqColor(max ? Math.sqrt(v / max) : 0);
        if (!v) { attrs.stroke = C.hair; attrs['stroke-width'] = 0.5; attrs.x += 0.5; attrs.y += 0.5; attrs.width -= 1; attrs.height -= 1; }
        var rect = svgEl(cells, 'rect', attrs);
        if (!exp) {
          rect.setAttribute('class', 'hit-cell'); rect.style.cursor = v ? 'pointer' : 'default';
          rect.addEventListener('pointerenter', function (ev) {
            Tip.clear(); Tip.add('tt-title', lanes[ri].name + ' → ' + lanes[ci].name);
            Tip.row(fmtN(v), v === 1 ? 'message names ' + lanes[ci].name : 'messages name ' + lanes[ci].name, null);
            Tip.row(fmtN(M[ci][ri]), 'the other way', null);
            if (rowT[r]) Tip.add('tt-note', Math.round(100 * v / rowT[r]) + '% of ' + lanes[ri].name + '’s mentions of these agents in the window.' + (v ? ' Click to read them.' : ''));
            Tip.show(ev.clientX, ev.clientY);
          });
          rect.addEventListener('pointermove', function (ev) { Tip.show(ev.clientX, ev.clientY); });
          rect.addEventListener('pointerleave', function () { Tip.hide(); });
          rect.addEventListener('click', function () { if (v) openMentions(ri, ci, wb[0], wb[1]); });
        }
      });
    });
    // margins: sent (row totals) and received (column totals)
    svgEl(svg, 'text', { x: x0 + n * cell + 6, y: y0 - 5, 'class': 'tick', style: 'font-style:italic' }, 'sent');
    rowT.forEach(function (v, r) { svgEl(svg, 'text', { x: x0 + n * cell + 6, y: y0 + r * cell + cell / 2 + ts * 0.35, 'class': 'tick' }, fmtK(v)); });
    svgEl(svg, 'text', { x: x0 - 6, y: y0 + n * cell + ts + 4, 'text-anchor': 'end', 'class': 'tick', style: 'font-style:italic' }, 'received');
    var colTs = Math.min(ts, Math.max(8.5, ts * (cell - 2) / Math.max(1, measure('9.9k', { size: ts }))));
    Array.prototype.forEach.call(colT, function (v, c) {
      svgEl(svg, 'text', { x: x0 + c * cell + cell / 2, y: y0 + n * cell + ts + 4, 'text-anchor': 'middle', 'class': 'tick', style: 'font-size:' + colTs + 'px' }, fmtC(v));
    });
    svgEl(svg, 'line', { x1: x0, x2: x0 + n * cell, y1: y0 - 0.5, y2: y0 - 0.5, 'class': 'axis-line' });
    svgEl(svg, 'line', { x1: x0 - 0.5, x2: x0 - 0.5, y1: y0, y2: y0 + n * cell, 'class': 'axis-line' });
    return { max: max, pairs: pairs, recip: recip, total: rowT.reduce(function (a, b) { return a + b; }, 0), window: wb };
  }
  function fitMx(s, w, fs) { return fit(s, w, { size: fs }); }

  function actionsIn(li, a, b) {
    var q = (D.acts || [])[li] || [], n = 0;
    for (var k = 0; k < q.length; k += 2) { var t = BASE + q[k] * BIN; if (t + BIN > a && t < b) n += q[k + 1]; }
    return n;
  }
  function messagesIn(li, a, b) {
    if (!META.sampled) {  // exact: the page holds every message of the lane
      var L = lanes[li], m = 0, j0 = lowerBound(T, L.a, L.b, a), j1 = lowerBound(T, j0, L.b, b);
      for (var j = j0; j < j1; j++) if (!S.hidden[D.c[j]]) m++;
      return m;
    }
    var q = D.dens.lanes[li] || [], n = 0;
    for (var k = 0; k < q.length; k += 3) { var t = BASE + q[k] * BIN; if (t + BIN > a && t < b && !S.hidden[q[k + 1]]) n += q[k + 2]; }
    return n;
  }
  function drawActivity(host, Wd, exp) {
    host.textContent = '';
    var wb = windowBounds();
    var rows = lanes.map(function (L, li) { return { li: li, m: messagesIn(li, wb[0], wb[1]), a: actionsIn(li, wb[0], wb[1]) }; })
      .sort(function (p, q) { return q.m - p.m || q.a - p.a || p.li - q.li; });
    var hasActs = rows.some(function (r) { return r.a > 0; });
    var fs = exp ? 11.5 : (Wd < 560 ? 11 : 12), ts = fs - 0.5, rowH = exp ? 14 : 19, barH = Math.round(rowH * 0.56);
    var nameW = 0; rows.forEach(function (r) { nameW = Math.max(nameW, measure(lanes[r.li].name, { size: fs })); });
    var labelW = Math.min(nameW + 10, Wd * 0.34), valW = measure('99.9k', { size: ts }) + 6, gapW = 18;
    var top = ts + 8, plotH = rows.length * rowH, H = top + plotH + ts + 10;
    var panels = [{ key: 'm', title: 'Messages', fill: C.ink2 }];
    if (hasActs) panels.push({ key: 'a', title: 'Actions', fill: C.paper });
    var avail = Wd - labelW - panels.length * valW - (panels.length - 1) * gapW;
    var pw = avail / panels.length;
    var svg = svgEl(host, 'svg', { 'class': 'viz', width: Wd, height: H, viewBox: '0 0 ' + Wd + ' ' + H, role: 'img', 'aria-label': 'Messages and actions per agent in the window' });
    rows.forEach(function (r, j) {
      var y = top + j * rowH;
      svgEl(svg, 'text', { x: labelW - 6, y: y + rowH / 2 + fs * 0.35, 'text-anchor': 'end', 'class': 'lbl', style: 'font-size:' + fs + 'px' + (r.li === S.sel ? ';font-weight:700' : '') }, fitMx(lanes[r.li].name, labelW - 8, fs));
    });
    panels.forEach(function (p, pi) {
      var x0 = labelW + pi * (pw + valW + gapW), max = Math.max(1, rows.reduce(function (m, r) { return Math.max(m, r[p.key]); }, 0)), k = pw / max;
      svgEl(svg, 'text', { x: x0, y: ts + 1, 'class': 'panel-title', style: 'font-size:' + fs + 'px' }, p.title);
      niceTicks(max, Math.max(1, Math.floor(pw / 64))).forEach(function (t) {
        var x = x0 + t * k;
        svgEl(svg, 'line', { x1: x, x2: x, y1: top, y2: top + plotH, 'class': 'grid' });
        svgEl(svg, 'line', { x1: x, x2: x, y1: top + plotH, y2: top + plotH + 3, 'class': 'tick-line' });
        svgEl(svg, 'text', { x: x, y: top + plotH + ts + 5, 'text-anchor': 'middle', 'class': 'tick' }, fmtK(t));
      });
      svgEl(svg, 'line', { x1: x0, x2: x0 + pw, y1: top + plotH + 0.5, y2: top + plotH + 0.5, 'class': 'axis-line' });
      svgEl(svg, 'line', { x1: x0 - 0.5, x2: x0 - 0.5, y1: top, y2: top + plotH, 'class': 'axis-line' });
      rows.forEach(function (r, j) {
        var y = top + j * rowH + (rowH - barH) / 2, v = r[p.key];
        if (p.key === 'a') svgEl(svg, 'rect', { x: x0 + 0.4, y: y + 0.4, width: Math.max(0, v * k - 0.8), height: barH - 0.8, fill: C.paper, stroke: C.ink2, 'stroke-width': 0.8 });
        else svgEl(svg, 'rect', { x: x0, y: y, width: Math.max(0, v * k), height: barH, fill: p.fill });
        svgEl(svg, 'text', { x: x0 + v * k + 4, y: y + barH / 2 + ts * 0.35, 'class': 'val' }, fmtK(v));
      });
    });
    return { hasActs: hasActs, rows: rows, window: wb };
  }
  // Ticks from 0 in a 1/2/5 step; with cover=true the last tick is at or above max (axis tops).
  function niceTicks(max, n, cover) {
    var raw = max / n, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p, step = (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * p, out = [];
    var top = cover ? Math.ceil(max / step - 1e-9) * step : max;
    for (var t = 0; t <= top + 1e-9; t += step) out.push(Math.round(t * 1e6) / 1e6);
    return out;
  }

  var winTimer = 0, lastMx = {}, lastAct = {};
  function scheduleWindow() { clearTimeout(winTimer); winTimer = setTimeout(renderWindow, 120); }
  function windowTitle() {
    var wb = windowBounds();
    var s = whenRange(wb[0], wb[1]);
    if (S.period >= 0) { var p = periods[S.period]; s = (p.kind === 'village_goal' ? 'Goal ' : 'Period ') + (periods.filter(function (q) { return q.kind === p.kind; }).indexOf(p) + 1) + ': “' + p.label + '”, ' + s; }
    return s;
  }
  function renderWindow() {
    var mxW = $('mx').clientWidth || 500, acW = $('act').clientWidth || 400;
    $('win-label').textContent = 'Window: ' + windowTitle() + '. Follows Figure 1’s zoom; click a goal to select it.';
    lastMx = drawMatrix($('mx'), mxW, false);
    lastAct = drawActivity($('act'), acW, false);
    $('cap-mx').textContent = captionMatrix(lastMx);
    $('cap-act').textContent = captionActivity(lastAct);
    renderRecap();
  }
  function captionMatrix(m) {
    var s = 'Figure 2: Who names whom, ' + whenRange(m.window[0], m.window[1]) + '. ';
    s += 'Cell (row A, column B) counts A’s messages that name B; darker is more (square-root scale, darkest = ' + plural(m.max, 'message') + '). ';
    s += 'Rows and columns follow Figure 1’s order; margins give the totals sent and received among these agents. ';
    if (m.pairs) s += m.recip + ' of the ' + m.pairs + ' pairs that mention each other at all (' + Math.round(100 * m.recip / m.pairs) + '%) do so in both directions. ';
    else s += 'No agent in the rows names another in this window. ';
    s += 'Mentions are names found in the message text (the store’s recipient ids), counted over every message' + (META.sampled ? ' (not just the sample) in ' + durLabel(BIN) + ' bins, so the window edges are rounded to whole bins.' : '.');
    return s;
  }
  function captionActivity(a) {
    var s = 'Figure 3: Who was active, ' + whenRange(a.window[0], a.window[1]) + '. ';
    s += 'Messages per agent' + (a.hasActs ? ' (left) and recorded actions such as session goals and summaries (right, own scale)' : '') + ', sorted by messages.';
    if (chans.some(function (_, i) { return S.hidden[i]; })) s += ' Messages exclude the hidden channels.';
    return s;
  }

  // ---------------------------------------------------------------- thread reader
  var reader = $('reader'), rbody = $('reader-body'), byChan = null;
  function chanRows() {
    if (byChan) return byChan;
    byChan = chans.map(function () { return []; });
    for (var i = 0; i < N; i++) byChan[D.c[i]].push(i);
    byChan.forEach(function (a) { a.sort(function (x, y) { return T[x] - T[y] || (D.id[x] < D.id[y] ? -1 : 1); }); });
    return byChan;
  }
  function actorOf(i) { return actors[D.au[i]] || { name: '?', kind: 'agent' }; }
  function msgEl(i, focus, asLink) {
    var a = actorOf(i), box = document.createElement('div');
    box.className = 'rmsg' + (focus ? ' focus' : '') + (asLink ? ' link' : '');
    var head = document.createElement('div');
    var who = document.createElement('span'); who.className = 'who' + (a.kind === 'human' ? ' human' : ''); who.textContent = a.name + (a.kind === 'human' ? ' (human)' : '');
    var when = document.createElement('span'); when.className = 'when'; when.textContent = (DAYS ? 'Day ' + dayOf(T[i]) + ' · ' : '') + fmtDateTime(T[i]) + (asLink ? ' · #' + chans[D.c[i]].name : '');
    head.appendChild(who); head.appendChild(when); box.appendChild(head);
    var txt = document.createElement('div'); txt.className = 'txt'; txt.textContent = D.s[i] || '(no text on this page)'; box.appendChild(txt);
    var meta = document.createElement('div'); meta.className = 'meta';
    var code = document.createElement('code'); code.textContent = evid(i); meta.appendChild(code);
    var cp = document.createElement('button'); cp.type = 'button'; cp.textContent = 'copy id';
    cp.addEventListener('click', function (ev) { ev.stopPropagation(); copyId(evid(i)); });
    meta.appendChild(cp);
    if (D.s[i] && D.len[i] > D.s[i].length) { var tr = document.createElement('span'); tr.textContent = fmtN(D.len[i]) + ' characters, truncated'; meta.appendChild(tr); }
    box.appendChild(meta);
    if (asLink) box.addEventListener('click', function () { openThread(i); });
    return box;
  }
  function readerNote() {
    if (D.ctx && D.ctx.complete) return '';
    var s = 'Only messages by the ' + lanes.length + ' agents in Figure 1 are on this page';
    if (META.sampled) s += ', and only an even sample of them (' + Math.round(100 * META.marks / Math.max(1, META.lane_total)) + '%)';
    return s + ', so neighbouring messages may be missing. Render a narrower window (--since/--until) to read whole threads.';
  }
  var cur = null;
  function openThread(i, before, after) {
    before = before == null ? 12 : before; after = after == null ? 12 : after;
    var arr = chanRows()[D.c[i]], pos = arr.indexOf(i);
    var a = Math.max(0, pos - before), b = Math.min(arr.length, pos + after + 1);
    cur = { i: i, before: before, after: after };
    $('reader-title').textContent = '#' + chans[D.c[i]].name;
    $('reader-sub').textContent = 'Around ' + actorOf(i).name + ', ' + (DAYS ? 'Day ' + dayOf(T[i]) + ', ' : '') + fmtDateTime(T[i]) + ' · ' + (b - a) + ' messages in this room';
    rbody.textContent = '';
    var note = readerNote();
    if (note) { var nt = document.createElement('p'); nt.className = 'reader-note'; nt.textContent = note; rbody.appendChild(nt); }
    if (a > 0) rbody.appendChild(moreBtn('Show 12 earlier', function () { openThread(i, before + 12, after); }));
    var focusEl = null;
    for (var j = a; j < b; j++) { var el = msgEl(arr[j], arr[j] === i, false); if (arr[j] === i) focusEl = el; rbody.appendChild(el); }
    if (b < arr.length) rbody.appendChild(moreBtn('Show 12 later', function () { openThread(i, before, after + 12); }));
    showReader();
    if (focusEl) focusEl.scrollIntoView({ block: 'center' });
  }
  function openMentions(ri, ci, a, b) {
    var rows = [];
    (D.rc || []).forEach(function (r) {
      var i = r[0];
      if (D.au[i] !== ri || T[i] < a || T[i] >= b) return;
      for (var k = 1; k < r.length; k++) if (r[k] === ci) { rows.push(i); break; }
    });
    rows.sort(function (x, y) { return T[x] - T[y]; });
    cur = null;
    $('reader-title').textContent = lanes[ri].name + ' → ' + lanes[ci].name;
    $('reader-sub').textContent = rows.length + ' messages on this page that name ' + lanes[ci].name + ', ' + whenRange(a, b);
    rbody.textContent = '';
    var nt = document.createElement('p'); nt.className = 'reader-note';
    nt.textContent = 'Click a message to read the conversation around it.' + (META.sampled ? ' This page keeps an even sample of the messages, so Figure 2’s count can be higher than this list.' : '');
    rbody.appendChild(nt);
    rows.slice(0, 400).forEach(function (i) { rbody.appendChild(msgEl(i, false, true)); });
    showReader();
  }
  function moreBtn(label, fn) { var d = document.createElement('div'); d.className = 'reader-more'; var b = document.createElement('button'); b.type = 'button'; b.className = 'btn'; b.textContent = label; b.onclick = fn; d.appendChild(b); return d; }
  function showReader() { reader.hidden = false; reader.focus({ preventScroll: true }); }
  function closeReader() { reader.hidden = true; ov.focus({ preventScroll: true }); }
  $('reader-close').onclick = closeReader;
  document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape' && !reader.hidden) closeReader(); });

  // ---------------------------------------------------------------- shared pieces for the linked panels
  var tableNo = 0;
  // A booktabs table with its "Table N:" title above. cols: [{label, num, cls, get(row) -> string | Node}]
  function bookTable(container, caption, cols, rows, emptyText) {
    container.textContent = '';
    if (!container.__tableNo) container.__tableNo = ++tableNo;
    var t = document.createElement('table'); t.className = 'booktabs stack';
    var cap = document.createElement('caption'); cap.textContent = 'Table ' + container.__tableNo + ': ' + caption; t.appendChild(cap);
    var thead = document.createElement('thead'), tr = document.createElement('tr');
    cols.forEach(function (c) { var th = document.createElement('th'); th.scope = 'col'; th.textContent = c.label; if (c.num) th.className = 'num'; tr.appendChild(th); });
    thead.appendChild(tr); t.appendChild(thead);
    var tb = document.createElement('tbody');
    if (!rows.length) {
      var r0 = document.createElement('tr'), td0 = document.createElement('td');
      td0.colSpan = cols.length; td0.className = 'muted'; td0.textContent = emptyText || 'Nothing to show.';
      r0.appendChild(td0); tb.appendChild(r0);
    }
    rows.forEach(function (row) {
      var r = document.createElement('tr');
      cols.forEach(function (c) {
        var td = document.createElement('td'); if (c.num) td.className = 'num'; else if (c.cls) td.className = c.cls;
        if (c.label) td.setAttribute('data-label', c.label);
        if (c.nowrap) td.style.whiteSpace = 'nowrap';
        var v = c.get(row);
        if (v instanceof Node) td.appendChild(v); else td.textContent = v == null ? '–' : String(v);
        r.appendChild(td);
      });
      tb.appendChild(r);
    });
    t.appendChild(tb); container.appendChild(t);
    return t;
  }
  function linkBtn(label, fn, title) {
    var b = document.createElement('button'); b.type = 'button'; b.className = 'lnk'; b.textContent = label;
    if (title) b.title = title;
    b.addEventListener('click', fn); return b;
  }
  function frag() { var f = document.createElement('span'); for (var k = 0; k < arguments.length; k++) if (arguments[k]) f.appendChild(arguments[k]); return f; }

  // Split time-ordered points into runs wherever consecutive points are more than maxGap apart, so
  // lines and bands do not bridge stretches with no data.
  function runs(pts, maxGap) {
    var out = [], cur = [];
    pts.forEach(function (p, i) { if (i && p.t - pts[i - 1].t > maxGap) { out.push(cur); cur = []; } cur.push(p); });
    if (cur.length) out.push(cur);
    return out;
  }
  function bandPath(pts, X, Y) {
    return 'M' + pts.map(function (p) { return X(p.t).toFixed(1) + ',' + Y(p.hi).toFixed(1); }).join('L') +
      'L' + pts.slice().reverse().map(function (p) { return X(p.t).toFixed(1) + ',' + Y(p.lo).toFixed(1); }).join('L') + 'Z';
  }
  function linePath(pts, X, Y) { return 'M' + pts.map(function (p) { return X(p.t).toFixed(1) + ',' + Y(p.y).toFixed(1); }).join('L'); }
  var GAP_BREAK = 4.5 * 864e5;

  // Time series with a band: series = [{name, color, dash, pts: [{t, y, lo, hi}]}]; direct labels at the right.
  function lineBand(host, Wd, spec, exp) {
    host.textContent = '';
    var fs = exp ? 11.5 : (Wd < 560 ? 11 : 12), ts = fs - 0.5;
    var labW = 0; spec.series.forEach(function (se) { labW = Math.max(labW, measure(se.name, { size: fs })); });
    var ml = measure('100%', { size: ts }) + 14, mr = Math.min(labW + 22, Wd * 0.3), mt = 10, axH = ts + 10 + (DAYS ? ts + 8 : 0);
    var ph = spec.height || (exp ? 150 : (Wd < 560 ? 170 : 220)), H = mt + ph + axH + (spec.ribbon ? 14 : 0);
    var x0 = ml, x1 = Wd - mr, y0 = mt + (spec.ribbon ? 14 : 0), y1 = y0 + ph;
    var a = spec.t0, b = spec.t1, k = (x1 - x0) / Math.max(1, b - a);
    function X(t) { return x0 + (t - a) * k; }
    var ymax = spec.ymax != null ? spec.ymax : 0;
    if (spec.ymax == null) spec.series.forEach(function (se) { se.pts.forEach(function (p) { ymax = Math.max(ymax, p.hi != null ? p.hi : p.y); }); });
    var yt = niceTicks(ymax || 1, 4, true); ymax = yt[yt.length - 1] || 1;
    function Y(v) { return y1 - (v / ymax) * ph; }
    var svg = svgEl(host, 'svg', { 'class': 'viz', width: Wd, height: H, viewBox: '0 0 ' + Wd + ' ' + H, role: 'img', 'aria-label': spec.aria || 'time series' });
    yt.forEach(function (v) {
      svgEl(svg, 'line', { x1: x0, x2: x1, y1: Y(v), y2: Y(v), 'class': 'grid' });
      svgEl(svg, 'text', { x: x0 - 5, y: Y(v) + ts * 0.35, 'text-anchor': 'end', 'class': 'tick' }, spec.yfmt ? spec.yfmt(v) : fmtK(v));
    });
    if (spec.ylabel) svgEl(svg, 'text', { x: x0 + 4, y: y0 - 4, 'class': 'tick', style: 'font-style:italic' }, spec.ylabel);
    // goal changes: hairlines across the plot, numbered in a strip on top
    if (spec.ribbon) {
      var n = 0;
      periods.forEach(function (p) {
        if (p.kind !== periodKinds[0]) return; n++;
        if (p.s < a || p.s > b) return;
        var x = X(p.s);
        svgEl(svg, 'line', { x1: x, x2: x, y1: y0 - 10, y2: y1, stroke: C.wash2, 'stroke-width': exp ? 0.35 : 0.6 });
        if (spec.ribbon === 'numbers' && (x1 - x0) / Math.max(1, periods.length) > 11) svgEl(svg, 'text', { x: x + 2, y: y0 - 3, 'class': 'tick', style: 'font-size:' + (ts - 1.5) + 'px' }, String(n));
      });
    }
    var dt = dateTicks(a, b, x1 - x0, 84);
    svgEl(svg, 'line', { x1: x0, x2: x1, y1: y1 + 0.5, y2: y1 + 0.5, 'class': 'axis-line' });
    svgEl(svg, 'line', { x1: x0 - 0.5, x2: x0 - 0.5, y1: y0, y2: y1, 'class': 'axis-line' });
    dt.forEach(function (tk) {
      var x = X(tk.t);
      svgEl(svg, 'line', { x1: x, x2: x, y1: y1, y2: y1 + 3, 'class': 'tick-line' });
      svgEl(svg, 'text', { x: x, y: y1 + ts + 4, 'text-anchor': 'middle', 'class': 'tick' }, tk.lbl);
    });
    if (DAYS) {
      dayTicks(a, b, x1 - x0, 60).forEach(function (tk) { svgEl(svg, 'text', { x: X(tk.t), y: y1 + 2 * ts + 10, 'text-anchor': 'middle', 'class': 'tick', style: 'fill:' + C.ink3 }, tk.lbl); });
      svgEl(svg, 'text', { x: x0 - 5, y: y1 + 2 * ts + 10, 'text-anchor': 'end', 'class': 'tick', style: 'font-style:italic;fill:' + C.ink3 }, 'Day');
    }
    var ends = [];
    spec.series.forEach(function (se) {
      var pts = se.pts.filter(function (p) { return p.y != null && p.t >= a && p.t <= b; });
      if (!pts.length) return;
      runs(pts.filter(function (p) { return p.lo != null && p.hi != null; }), GAP_BREAK).forEach(function (band) {
        if (band.length > 1) svgEl(svg, 'path', { d: bandPath(band, X, Y), fill: se.color, 'fill-opacity': 0.12, stroke: 'none' });
      });
      runs(pts, GAP_BREAK).forEach(function (run) {
        svgEl(svg, 'path', { d: linePath(run, X, Y), fill: 'none', stroke: se.color,
          'stroke-width': exp ? 1.1 : 1.4, 'stroke-dasharray': se.dash || null, 'stroke-linejoin': 'round' });
      });
      var last = pts[pts.length - 1];
      ends.push({ se: se, x: X(last.t), y: Y(last.y), v: last.y });
    });
    // direct labels: keep each near its line's end, nudged apart, with a leader when moved
    ends.sort(function (p, q) { return p.y - q.y; });
    var gap = fs + 2;
    ends.forEach(function (e, j) { e.ly = j ? Math.max(e.y, ends[j - 1].ly + gap) : e.y; });
    var over = ends.length ? ends[ends.length - 1].ly - (y1 + 4) : 0;
    if (over > 0) ends.forEach(function (e) { e.ly -= over; });
    ends.forEach(function (e) {
      var lx = x1 + 10;
      if (Math.abs(e.ly - e.y) > 1.5) svgEl(svg, 'path', { d: 'M' + (e.x + 2) + ',' + e.y + 'L' + (lx - 3) + ',' + e.ly, fill: 'none', stroke: C.ink3, 'stroke-width': 0.6 });
      svgEl(svg, 'text', { x: lx, y: e.ly + fs * 0.35, 'class': 'lbl', style: 'font-size:' + fs + 'px' }, fitMx(e.se.name, mr - 14, fs));
    });
    if (!exp && spec.hover !== false) {
      var cross = svgEl(svg, 'line', { y1: y0, y2: y1, stroke: C.ink3, 'stroke-width': 0.8, visibility: 'hidden' });
      var ov = svgEl(svg, 'rect', { x: x0, y: y0, width: x1 - x0, height: ph, fill: 'transparent' });
      ov.addEventListener('pointermove', function (ev) {
        var r = svg.getBoundingClientRect(), t = a + (ev.clientX - r.left - x0) / k;
        cross.setAttribute('x1', ev.clientX - r.left); cross.setAttribute('x2', ev.clientX - r.left); cross.setAttribute('visibility', 'visible');
        Tip.clear(); Tip.add('tt-title', (DAYS ? 'Day ' + dayOf(t) + ' · ' : '') + fmtDate(t));
        spec.series.forEach(function (se) {
          var best = null, bd = Infinity;
          se.pts.forEach(function (p) { var dd = Math.abs(p.t - t); if (dd < bd) { bd = dd; best = p; } });
          if (best && best.y != null) Tip.row((spec.yfmt ? spec.yfmt(best.y) : fmtN(best.y)) + (best.lo != null ? ' [' + (spec.yfmt ? spec.yfmt(best.lo) : fmtN(best.lo)) + ', ' + (spec.yfmt ? spec.yfmt(best.hi) : fmtN(best.hi)) + ']' : ''), se.name, se.color);
        });
        if (spec.tipNote) Tip.add('tt-note', spec.tipNote);
        Tip.show(ev.clientX, ev.clientY);
      });
      ov.addEventListener('pointerleave', function () { cross.setAttribute('visibility', 'hidden'); Tip.hide(); });
    }
    return svg;
  }

  // Rows x columns of shaded cells: rows [{name, cells:[v...]}], cols [{label, title}]
  function heatStrip(host, Wd, spec, exp) {
    host.textContent = '';
    var fs = exp ? 11.5 : (Wd < 560 ? 11 : 12), ts = fs - 0.5;
    var nameW = 0; spec.rows.forEach(function (r) { nameW = Math.max(nameW, measure(r.name, { size: fs })); });
    var labelW = Math.min(nameW + 10, Wd * 0.3), nc = spec.cols.length;
    var cw = Math.max(4, Math.min(26, (Wd - labelW - 8) / Math.max(1, nc))), rh = exp ? 13 : 18, top = ts + 10;
    var H = top + spec.rows.length * rh + ts + 10, max = 0;
    spec.rows.forEach(function (r) { r.cells.forEach(function (v) { if (v > max) max = v; }); });
    var svg = svgEl(host, 'svg', { 'class': 'viz', width: Wd, height: H, viewBox: '0 0 ' + Wd + ' ' + H, role: 'img', 'aria-label': spec.aria || 'heat strip' });
    var every = Math.max(1, Math.ceil(24 / cw));
    spec.cols.forEach(function (c, j) {
      if (j % every === 0) svgEl(svg, 'text', { x: labelW + j * cw + cw / 2, y: top - 4, 'text-anchor': 'middle', 'class': 'tick', style: 'font-size:' + (ts - 1) + 'px' }, c.label);
    });
    spec.rows.forEach(function (r, i) {
      var y = top + i * rh;
      svgEl(svg, 'text', { x: labelW - 6, y: y + rh / 2 + fs * 0.35, 'text-anchor': 'end', 'class': 'lbl', style: 'font-size:' + fs + 'px' + (r.bold ? ';font-weight:700' : '') }, fitMx(r.name, labelW - 8, fs));
      r.cells.forEach(function (v, j) {
        var x = labelW + j * cw;
        var rect = svgEl(svg, 'rect', { x: x + 0.5, y: y + 0.5, width: cw - 1, height: rh - 1, fill: v ? seqColor(max ? Math.sqrt(v / max) : 0) : C.paper, stroke: v ? null : C.hair, 'stroke-width': v ? null : 0.4 });
        if (!exp && spec.tip) {
          rect.addEventListener('pointerenter', function (ev) { spec.tip(r, j, v); Tip.show(ev.clientX, ev.clientY); });
          rect.addEventListener('pointermove', function (ev) { Tip.show(ev.clientX, ev.clientY); });
          rect.addEventListener('pointerleave', function () { Tip.hide(); });
          if (spec.click) { rect.style.cursor = 'pointer'; rect.addEventListener('click', function () { spec.click(r, j, v); }); }
        }
      });
    });
    if (spec.marks) spec.marks.forEach(function (m) {  // e.g. partner shifts: a tick under the column
      var x = labelW + m.j * cw + cw / 2, y = top + spec.rows.length * rh + 3;
      svgEl(svg, 'path', { d: 'M' + (x - 3.5) + ',' + (y + 6) + 'L' + (x + 3.5) + ',' + (y + 6) + 'L' + x + ',' + y + 'Z', fill: C.ink });
    });
    return { svg: svg, max: max };
  }

  // ---------------------------------------------------------------- linked panels: recap, moments, agent arc, metrics
  var XD = D.x || {};
  var rowById = null;
  function rowOf(id) {
    if (!rowById) { rowById = {}; for (var i = 0; i < N; i++) rowById[D.idp + D.id[i]] = i; }
    return rowById[id];
  }
  function firstRow(ids) { for (var k = 0; k < (ids || []).length; k++) { var r = rowOf(ids[k]); if (r != null) return r; } return null; }
  function readLink(ids, label) {
    var r = firstRow(ids);
    if (r == null) {
      var sp = document.createElement('span'); sp.className = 'muted'; sp.textContent = '\u2013';
      sp.title = 'Not on this page (sampled away or outside the filters): ' + (ids || []).slice(0, 3).join(', ');
      return sp;
    }
    return linkBtn(label || 'Read', function () { openThread(r); }, 'Open the conversation around it');
  }
  function showLink(a, b) { return linkBtn('Show', function () { var pad = Math.max(36e5, (b - a) * 0.3); setView(a - pad, b + pad); $('fig-tl').scrollIntoView({ behavior: 'smooth', block: 'start' }); }, 'Zoom Figure 1 to this moment'); }
  function viewIsFull() { return S.v0 <= FULL0 + (FULL1 - FULL0) * 0.02 && S.v1 >= FULL1 - (FULL1 - FULL0) * 0.02; }
  function currentRecap() {
    if (S.period >= 0 && XD.recaps && XD.recaps[periods[S.period].id]) return { r: XD.recaps[periods[S.period].id], what: 'this goal', vs: 'the previous goal' };
    if (viewIsFull() && XD.recap_all) return { r: XD.recap_all, what: 'this render', vs: 'the same length before it' };
    return null;
  }
  function renderRecap() {
    var more = $('recap-more'), hint = $('recap-hint');
    if (!XD.recaps && !XD.recap_all) { more.hidden = true; hint.hidden = true; return; }
    var cr = currentRecap();
    if (!cr) {
      more.hidden = true; hint.hidden = false;
      hint.textContent = 'Rising terms and the busiest threads are computed for each goal and for the whole render: click a goal in Figure 1, or Full range.';
      return;
    }
    more.hidden = false; hint.hidden = true;
    var r = cr.r;
    bookTable($('tbl-terms'), 'Terms that rose in ' + cr.what + ' against ' + cr.vs + '. A term is a word or two-word phrase counted once per message; ranked by log-odds with an informative prior (Monroe et al., 2008).', [
      { label: 'Term', cls: 'term', get: function (t) { return t.term; } },
      { label: 'Msgs', num: true, get: function (t) { return fmtN(t.n); } },
      { label: 'Before', num: true, get: function (t) { return t.n_before == null ? '–' : fmtN(t.n_before); } },
      { label: 'Agents', num: true, get: function (t) { return fmtN(t.agents); } },
      { label: '', get: function (t) { return t.first_id ? readLink([t.first_id], 'First use') : ''; } }
    ], r.baseline ? r.terms : [], r.baseline ? 'No term rose clearly.' : 'There are no messages before this window to compare with; pick a goal (each is compared with the previous one).');
    bookTable($('tbl-bursts'), 'Busiest threads in ' + cr.what + ': runs of messages in one channel with gaps of at most 20 minutes, by length.', [
      { label: 'When', get: function (b) { return (DAYS ? 'Day ' + dayOf(b.s) + ', ' : '') + fmtTime(b.s) + '–' + fmtTime(b.e) + ' UTC'; } },
      { label: 'Room', get: function (b) { return '#' + b.channel; } },
      { label: 'Msgs', num: true, get: function (b) { return fmtN(b.n); } },
      { label: 'Who', get: function (b) { var a = b.agents || []; return a.slice(0, 3).join(', ') + (a.length > 3 ? ' +' + (a.length - 3) : ''); } },
      { label: '', get: function (b) { var rr = firstRow(b.ids); return rr == null ? readLink(b.ids) : linkBtn('Read', function () { openThread(rr, 2, Math.min(80, (b.n || 20) + 2)); }, 'Open this thread from its start'); } }
    ], r.bursts, 'No threads.');
  }

  var KIND = { burst: 'Burst', silence: 'Silence', partner_shift: 'Partner shift', first_use: 'First use' };
  function renderMoments() {
    var ms = XD.moments || [];
    if (!ms.length) return;
    $('sec-moments').hidden = false;
    $('moments-intro').textContent = 'Spikes and changes worth a look, in time order (the strongest of each kind are kept). Bursts and silences compare an agent’s or a channel’s daily messages with its previous 14 active days; partner shifts compare an agent’s mix of mentions between goals (Jensen–Shannon distance); first uses are terms that other agents picked up quickly. Each row says why it was flagged.';
    bookTable($('tbl-moments'), 'Notable moments in this render (' + ms.length + ').', [
      { label: '#', num: true, get: function (m) { return String(ms.indexOf(m) + 1); } },
      { label: 'When', nowrap: true, get: function (m) { return (DAYS ? 'Day ' + dayOf(m.t) + ', ' : '') + fmtDate(m.t); } },
      { label: 'Kind', nowrap: true, get: function (m) { return KIND[m.kind] || m.kind; } },
      { label: 'Who or where', get: function (m) { return m.agent || (m.channel ? '#' + m.channel : '') || ''; } },
      { label: 'Why', cls: 'why', get: function (m) { return m.why; } },
      { label: '', nowrap: true, get: function (m) { return frag(readLink(m.ids, 'Read'), showLink(m.t, m.e || m.t)); } }
    ], ms);
  }

  // ---- one agent over time
  var arcSel = $('agent-sel');
  function setupAgentSelect() {
    var arcs = XD.arcs || {};
    if (!Object.keys(arcs).length) return false;
    lanes.forEach(function (L, li) { if (!arcs[li]) return; var o = document.createElement('option'); o.value = String(li); o.textContent = L.name; arcSel.appendChild(o); });
    arcSel.addEventListener('change', function () { selectAgent(+arcSel.value, false); });
    return true;
  }
  function selectAgent(li, scroll, quiet) {
    if (!XD.arcs || !XD.arcs[li]) return;
    arcSel.value = String(li); S.arc = li;
    if (!quiet && S.sel !== li) { S.sel = li; redraw(); }  // a user's pick is also highlighted in Figure 1
    renderArc();
    if (scroll) $('sec-agent').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  function arcRates(li) {
    var R = XD.agent_rates; if (!R || !R.lanes[li]) return null;
    return R.lanes[li].map(function (p) { return { t: R.starts[p[0]], y: p[2], lo: p[3], hi: p[4], v: p[1], num: p[5], den: p[6] }; });
  }
  function drawArc(host, Wd, exp) {
    host.textContent = '';
    var li = S.arc, A = XD.arcs[li], L = lanes[li];
    var fs = exp ? 11.5 : (Wd < 560 ? 11 : 12), ts = fs - 0.5;
    var ml = measure('100%', { size: ts }) + 16, mr = 12, gap = 26, top = 14;
    var h1 = exp ? 70 : 96, rates = arcRates(li), h2 = rates ? (exp ? 60 : 84) : 0;
    var axH = ts + 10 + (DAYS ? ts + 8 : 0), H = top + h1 + (rates ? gap + h2 : 0) + axH + 4;
    var wk0 = (A.bin_days || 7) * 864e5, lastBin = (A.bins || []).filter(function (q) { return q[1] || q[2]; }).pop();
    var a = Math.max(D.start, L.first - wk0), b = Math.min(D.end, lastBin ? lastBin[0] + 2 * wk0 : D.end);
    if (b - a < 14 * 864e5) b = Math.min(D.end, a + 14 * 864e5);
    var x0 = ml, x1 = Wd - mr, k = (x1 - x0) / Math.max(1, b - a);
    function X(t) { return x0 + (t - a) * k; }
    var svg = svgEl(host, 'svg', { 'class': 'viz', width: Wd, height: H, viewBox: '0 0 ' + Wd + ' ' + H, role: 'img', 'aria-label': 'Activity of ' + L.name + ' over time' });
    svg.__span = [a, b];
    // goal boundaries as hairlines through both panels
    periods.forEach(function (p) { if (p.kind === periodKinds[0] && p.s > a && p.s < b) svgEl(svg, 'line', { x1: X(p.s), x2: X(p.s), y1: top - 4, y2: top + h1 + (rates ? gap + h2 : 0), stroke: C.wash2, 'stroke-width': exp ? 0.35 : 0.6 }); });
    // (a) messages and actions per bin
    var bins = A.bins || [], wk = (A.bin_days || 7) * 864e5, maxv = 1;
    bins.forEach(function (bn) { maxv = Math.max(maxv, bn[1], bn[2]); });
    var yt = niceTicks(maxv, 3, true); maxv = yt[yt.length - 1] || 1;
    var ya0 = top, ya1 = top + h1;
    function Ya(v) { return ya1 - v / maxv * h1; }
    svgEl(svg, 'text', { x: x0, y: ya0 - 4, 'class': 'panel-title', style: 'font-size:' + fs + 'px' }, '(a) Messages per ' + (A.bin_days === 7 ? 'week' : A.bin_days + ' days') + ' (bars) and actions (line)');
    yt.forEach(function (v) { svgEl(svg, 'line', { x1: x0, x2: x1, y1: Ya(v), y2: Ya(v), 'class': 'grid' }); svgEl(svg, 'text', { x: x0 - 5, y: Ya(v) + ts * 0.35, 'text-anchor': 'end', 'class': 'tick' }, fmtK(v)); });
    var bw = Math.max(1, wk * k - (wk * k > 4 ? 1 : 0));
    bins = bins.filter(function (bn) { return bn[0] + wk > a && bn[0] < b; });
    bins.forEach(function (bn) {
      if (!bn[1]) return;
      var xa = Math.max(x0, X(bn[0])), xb = Math.min(x1, X(bn[0]) + bw);
      if (xb - xa > 0.4) svgEl(svg, 'rect', { x: xa, y: Ya(bn[1]), width: xb - xa, height: ya1 - Ya(bn[1]), fill: C.ink2 });
    });
    var actPts = bins.map(function (bn) { return Math.min(x1, Math.max(x0, X(bn[0]) + bw / 2)).toFixed(1) + ',' + Ya(bn[2]).toFixed(1); });
    if (bins.some(function (bn) { return bn[2] > 0; })) svgEl(svg, 'path', { d: 'M' + actPts.join('L'), fill: 'none', stroke: C.ink, 'stroke-width': exp ? 0.9 : 1.2, 'stroke-dasharray': '3 2' });
    svgEl(svg, 'line', { x1: x0, x2: x1, y1: ya1 + 0.5, y2: ya1 + 0.5, 'class': 'axis-line' });
    svgEl(svg, 'line', { x1: x0 - 0.5, x2: x0 - 0.5, y1: ya0, y2: ya1, 'class': 'axis-line' });
    var yb1 = ya1;
    if (rates) {
      var yb0 = ya1 + gap; yb1 = yb0 + h2;
      function Yb(v) { return yb1 - v * h2; }
      svgEl(svg, 'text', { x: x0, y: yb0 - 4, 'class': 'panel-title', style: 'font-size:' + fs + 'px' }, '(b) Share of its messages that name another agent');
      [0, 0.5, 1].forEach(function (v) { svgEl(svg, 'line', { x1: x0, x2: x1, y1: Yb(v), y2: Yb(v), 'class': 'grid' }); svgEl(svg, 'text', { x: x0 - 5, y: Yb(v) + ts * 0.35, 'text-anchor': 'end', 'class': 'tick' }, Math.round(v * 100) + '%'); });
      runs(rates.filter(function (p) { return p.lo != null && p.hi != null && p.t >= a; }), GAP_BREAK).forEach(function (band) {
        if (band.length > 1) svgEl(svg, 'path', { d: bandPath(band, X, Yb), fill: C.ink, 'fill-opacity': 0.12 });
      });
      runs(rates.filter(function (p) { return p.y != null && p.t >= a; }), GAP_BREAK).forEach(function (run) {
        svgEl(svg, 'path', { d: linePath(run, X, Yb), fill: 'none', stroke: C.ink, 'stroke-width': exp ? 1 : 1.4, 'stroke-linejoin': 'round' });
      });
      svgEl(svg, 'line', { x1: x0, x2: x1, y1: yb1 + 0.5, y2: yb1 + 0.5, 'class': 'axis-line' });
      svgEl(svg, 'line', { x1: x0 - 0.5, x2: x0 - 0.5, y1: yb0, y2: yb1, 'class': 'axis-line' });
    }
    dateTicks(a, b, x1 - x0, 84).forEach(function (tk) {
      var x = X(tk.t);
      svgEl(svg, 'line', { x1: x, x2: x, y1: yb1, y2: yb1 + 3, 'class': 'tick-line' });
      svgEl(svg, 'text', { x: x, y: yb1 + ts + 4, 'text-anchor': 'middle', 'class': 'tick' }, tk.lbl);
    });
    if (DAYS) {
      dayTicks(a, b, x1 - x0, 60).forEach(function (tk) { svgEl(svg, 'text', { x: X(tk.t), y: yb1 + 2 * ts + 10, 'text-anchor': 'middle', 'class': 'tick', style: 'fill:' + C.ink3 }, tk.lbl); });
      svgEl(svg, 'text', { x: x0 - 5, y: yb1 + 2 * ts + 10, 'text-anchor': 'end', 'class': 'tick', style: 'font-style:italic;fill:' + C.ink3 }, 'Day');
    }
    if (!exp) {
      var cross = svgEl(svg, 'line', { y1: top, y2: yb1, stroke: C.ink3, 'stroke-width': 0.8, visibility: 'hidden' });
      var ov2 = svgEl(svg, 'rect', { x: x0, y: top, width: x1 - x0, height: yb1 - top, fill: 'transparent' });
      ov2.addEventListener('pointermove', function (ev) {
        var r = svg.getBoundingClientRect(), t = a + (ev.clientX - r.left - x0) / k;
        cross.setAttribute('x1', ev.clientX - r.left); cross.setAttribute('x2', ev.clientX - r.left); cross.setAttribute('visibility', 'visible');
        var bn = null; bins.forEach(function (q) { if (q[0] <= t && t < q[0] + wk) bn = q; });
        Tip.clear(); Tip.add('tt-title', L.name); Tip.add('tt-sub', whenRange(bn ? bn[0] : t, bn ? bn[0] + wk - 1 : t));
        if (bn) { Tip.row(fmtN(bn[1]), 'messages', C.ink2); Tip.row(fmtN(bn[2]), 'actions', C.ink); }
        if (rates) {
          var best = null, bd = Infinity; rates.forEach(function (p) { var dd = Math.abs(p.t - t); if (dd < bd) { bd = dd; best = p; } });
          if (best && best.y != null && bd < 3 * 864e5) Tip.row(Math.round(best.y * 100) + '% [' + Math.round(best.lo * 100) + ', ' + Math.round(best.hi * 100) + ']', 'name another agent, rolling ' + (XD.agent_rates.window || 7) + '-day', null);
        }
        Tip.show(ev.clientX, ev.clientY);
      });
      ov2.addEventListener('pointerleave', function () { cross.setAttribute('visibility', 'hidden'); Tip.hide(); });
    }
  }
  function partnerSpec(li) {
    var A = XD.arcs[li], tot = {};
    var cols = (A.partners || []).filter(function (p) { return p.out > 0; });
    cols.forEach(function (p) { (p.to || []).forEach(function (x) { tot[x[0]] = (tot[x[0]] || 0) + x[1]; }); });
    var names = Object.keys(tot).sort(function (p, q) { return tot[q] - tot[p]; }).slice(0, 8);
    var goalNo = {}; var n = 0; periods.forEach(function (p) { if (p.kind === periodKinds[0]) goalNo[p.id] = ++n; });
    return {
      cols: cols.map(function (p) { return { label: goalNo[p.id] ? String(goalNo[p.id]) : '', p: p }; }),
      rows: names.map(function (nm) { return { name: nm, cells: cols.map(function (p) { var hit = (p.to || []).filter(function (x) { return x[0] === nm; })[0]; return hit ? hit[1] : 0; }) }; }),
      marks: cols.map(function (p, j) { return p.js != null && p.js >= 0.5 ? { j: j } : null; }).filter(Boolean),
      aria: 'Whom ' + lanes[li].name + ' names, goal by goal',
      tip: function (r, j, v) {
        var p = cols[j];
        Tip.clear(); Tip.add('tt-title', (goalNo[p.id] ? 'Goal ' + goalNo[p.id] + ': ' : '') + (p.label || ''));
        Tip.add('tt-sub', whenRange(p.s, p.e || D.end));
        Tip.row(fmtN(v), lanes[li].name + ' named ' + r.name, null);
        Tip.add('tt-note', 'It named others ' + fmtN(p.out) + ' times and was named ' + fmtN(p['in']) + ' times in this goal.' + (p.js != null ? ' Shift vs the previous goal: ' + p.js.toFixed(2) + '.' : '') + ' Click to zoom to the goal.');
      },
      click: function (r, j) { var k2 = periods.findIndex(function (q) { return q.id === cols[j].p.id; }); if (k2 >= 0) { selectPeriod(k2); $('fig-tl').scrollIntoView({ behavior: 'smooth', block: 'start' }); } }
    };
  }
  function renderArc() {
    var li = S.arc; if (li == null || !XD.arcs || !XD.arcs[li]) return;
    var A = XD.arcs[li], L = lanes[li], W1 = $('arc').clientWidth || 800;
    drawArc($('arc'), W1, false);
    $('cap-arc').textContent = captionArc(li);
    var ps = partnerSpec(li);
    if (ps.rows.length) { $('fig-partners').hidden = false; heatStrip($('partners'), $('partners').clientWidth || 800, ps, false); $('cap-partners').textContent = captionPartners(li, ps); }
    else $('fig-partners').hidden = true;
    var tc = A.term_counts || {};
    bookTable($('tbl-arc-terms'), 'Novel terms ' + L.name + ' coined (' + fmtN(tc.coined || 0) + ') or adopted (' + fmtN(tc.adopted || 0) + '), most adopters first. A novel term first appears after the first tenth of the data and is used by at least three agents. There is no dictionary, so ordinary words that first appear late count too: read these as candidates.', [
      { label: 'Term', cls: 'term', get: function (t) { return t.term; } },
      { label: 'Role', get: function (t) { return t.role; } },
      { label: 'First use', get: function (t) { return t.t ? (DAYS ? 'Day ' + dayOf(t.t) + ', ' : '') + fmtDate(t.t) : ''; } },
      { label: 'Its msgs', num: true, get: function (t) { return fmtN(t.n || 0); } },
      { label: 'Adopters', num: true, get: function (t) { return fmtN(t.adopters || 0); } },
      { label: '', get: function (t) { return t.first_id ? readLink([t.first_id], 'First use') : ''; } }
    ], A.terms || [], 'No novel terms.');
  }
  function captionArc(li) {
    var A = XD.arcs[li], L = lanes[li], tot = 0, act = 0;
    (A.bins || []).forEach(function (b) { tot += b[1]; act += b[2]; });
    var s = 'Figure 4: ' + L.name + ' over time' + (L.lab ? ' (' + L.lab + ')' : '') + ', from its first message (' + whenRange(L.first, L.first) + ') on. ';
    s += '(a) Its messages per ' + (A.bin_days === 7 ? 'week' : A.bin_days + ' days') + ' (bars; ' + fmtN(tot) + ' in all) and recorded actions such as session goals and summaries (dashed line; ' + fmtN(act) + ').';
    if (arcRates(li)) s += ' (b) The share of its messages that name another agent: trailing ' + (XD.agent_rates.window || 7) + '-day mean over its active days with a 95% Wilson band.';
    s += ' Hairlines mark goal changes. Hover for the numbers.';
    return s;
  }
  function captionPartners(li, ps) {
    return 'Figure 5: Whom ' + lanes[li].name + ' names, goal by goal. Rows are its ' + ps.rows.length + ' most-named partners; columns are the ' + ps.cols.length +
      ' goals (numbered as in Figure 1) in which it named anyone; darker cells hold more messages naming that partner (square-root shading). Triangles mark goals where its mix of partners shifted most against the previous goal (Jensen–Shannon distance at least 0.5). Click a cell to zoom Figure 1 to that goal.';
  }

  // ---- metrics over time
  var LABCOL = { 'Anthropic': '--lab-anthropic', 'OpenAI': '--lab-openai', 'Google': '--lab-google', 'Other labs': '--lab-other', 'Other': '--lab-other' };
  var DASH = [null, '5 3', '2 2', '7 2 2 2', '1 3', '9 3'];
  function seriesSpec(m) {
    var slots = []; for (var j = 1; j <= 7; j++) slots.push(PK.cssVar('--c' + j));
    return {
      t0: D.start, t1: D.end, ribbon: true, aria: m.label, ylabel: m.kind === 'rate' ? null : (m.unit || null),
      yfmt: m.kind === 'rate' || m.kind === 'share' || (m.unit || '').indexOf('share') >= 0 ? function (v) { return Math.round(v * 100) + '%'; } : null,
      ymax: (m.kind === 'rate' || (m.unit || '').indexOf('share') >= 0) ? 1 : null,
      series: m.groups.map(function (g, gi) {
        var col = m.by === 'lab' && LABCOL[g.name] ? PK.cssVar(LABCOL[g.name]) : (m.by === 'channel' ? (chCol[chans.findIndex(function (c) { return '#' + c.name === g.name || c.name === g.name; })] || slots[gi % 7]) : slots[gi % 7]);
        return { name: g.name, color: col, dash: DASH[gi % DASH.length], pts: g.pts.map(function (p) { return { t: m.starts[p[0]], y: p[2], lo: p[3], hi: p[4] }; }) };
      })
    };
  }
  function renderSeries() {
    var list = XD.series || []; if (!list.length) return;
    var m = list[+$('metric-sel').value || 0];
    lineBand($('series'), $('series').clientWidth || 800, seriesSpec(m), false);
    $('cap-series').textContent = captionSeries(m);
  }
  function captionSeries(m) {
    var isRate = m.kind === 'rate' || (m.unit || '').indexOf('share') >= 0;
    return 'Figure 6: ' + m.label + ', ' + whenRange(D.start, D.end) + '. Lines are trailing ' + (m.window || 7) + '-day means over active days; bands are 95% ' + (isRate ? 'Wilson intervals on the pooled window counts' : 'intervals on the window mean') + '. Groups are labelled at the right end of their lines and also differ in dash pattern. Hairlines mark goal changes. ' + (m.notes || []).filter(function (n) { return /more .* not shown|excluded/i.test(n); }).join(' ');
  }

  function setupPanels() {
    renderMoments();
    if (setupAgentSelect()) {
      $('sec-agent').hidden = false;
      selectAgent(S.sel >= 0 ? S.sel : 0, false, true);
    }
    var list = XD.series || [];
    if (list.length) {
      $('sec-metrics').hidden = false;
      list.forEach(function (m, i) { var o = document.createElement('option'); o.value = String(i); o.textContent = m.label; $('metric-sel').appendChild(o); });
      $('metric-sel').addEventListener('change', renderSeries);
      renderSeries();
    }
    PK.exportMenu($('tools-arc'), { draw: function (h, w) { drawArc(h, w, true); }, name: function () { return 'swarmscope-agent-' + (lanes[S.arc] || {}).name; }, caption: function () { return captionArc(S.arc); }, status: statusEl });
    PK.exportMenu($('tools-partners'), { draw: function (h, w) { heatStrip(h, w, partnerSpec(S.arc), true); }, name: function () { return 'swarmscope-partners-' + (lanes[S.arc] || {}).name; }, caption: function () { return captionPartners(S.arc, partnerSpec(S.arc)); }, status: statusEl });
    PK.exportMenu($('tools-series'), { draw: function (h, w) { lineBand(h, w, seriesSpec((XD.series || [])[+$('metric-sel').value || 0]), true); }, name: function () { return 'swarmscope-metric'; }, caption: function () { return captionSeries((XD.series || [])[+$('metric-sel').value || 0]); }, status: statusEl });
    if (window.ResizeObserver) {
      var lw = $('sec-agent').clientWidth;
      new ResizeObserver(function () { var w = $('sec-agent').clientWidth; if (w !== lw) { lw = w; renderArc(); renderSeries(); } }).observe($('sec-agent'));
    }
  }

  // ---------------------------------------------------------------- export (current view at 5.5 in)
  function exportTimeline(host, Wd) {
    var g = layout(Wd, true), P = SvgPainter(Wd, g.H);
    drawAll(P, g, S.v0, S.v1, {});
    host.appendChild(P.svg);
  }
  function fileStem(kind) {
    var wb = windowBounds();
    return 'swarmscope-' + kind + '-' + (DAYS ? 'days-' + DAYS.of(wb[0]) + '-' + DAYS.of(wb[1]) : isoS(wb[0]).slice(0, 10) + '-' + isoS(wb[1]).slice(0, 10));
  }
  PK.exportMenu($('tools-tl'), { draw: exportTimeline, name: function () { return fileStem('timeline'); }, caption: function () { return captionTimeline(true); }, status: statusEl });
  PK.exportMenu($('tools-mx'), { draw: function (h, w) { drawMatrix(h, w, true); }, name: function () { return fileStem('mentions'); }, caption: function () { return captionMatrix(lastMx); }, status: statusEl });
  PK.exportMenu($('tools-act'), { draw: function (h, w) { drawActivity(h, w, true); }, name: function () { return fileStem('activity'); }, caption: function () { return captionActivity(lastAct); }, status: statusEl });

  // ---------------------------------------------------------------- start
  readColors(); buildKeys(); resize(); renderWindow(); setupPanels();
  if (window.ResizeObserver) {
    var lastW = plot.clientWidth;
    new ResizeObserver(function () { if (plot.clientWidth !== lastW) { lastW = plot.clientWidth; resize(); scheduleWindow(); } }).observe(plot);
  } else window.addEventListener('resize', function () { resize(); scheduleWindow(); });
})();
