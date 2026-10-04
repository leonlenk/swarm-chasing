/* paperkit.js: shared helpers for the paper-style figure pages (inlined, no dependencies).
 *
 * window.PaperKit
 *   .PRESETS            paper: 5.5 in (NeurIPS text width, 528 CSS px), SVG or 300-dpi PNG
 *   .cssVar(name)       a token from paper.css, e.g. cssVar('--c1')
 *   .renderOffscreen(fn, preset)
 *                       calls fn(host, widthPx, presetName) on a hidden host carrying the preset's
 *                       class and returns the first <svg> it drew (.release(svg) removes the host)
 *   .svgMarkup(svg, opt)  a standalone SVG string: computed styles inlined, sized in inches
 *   .exportSVG / .exportPNG(svg, name, opt)       download (PNG at opt.scale or opt.dpi)
 *   .exportFigure(fn, name, presetName, fmt, opt) re-lay out a figure for a preset, then export it
 *   .exportMenu(container, spec)                  the quiet "Export: SVG · PNG" row of a figure
 *   .copyText(text) -> Promise<boolean>, .download(blob, name)
 *   .days(dayOneISO, timeZone)   Village-day arithmetic: .of(ms) -> N, .start(N) -> ms, .label(N)
 *
 * Text that reaches an exported SVG is whatever the page put in the DOM with textContent;
 * XMLSerializer escapes it, so untrusted labels stay inert in the exported file too.
 */
(function () {
  'use strict';
  var SVGNS = 'http://www.w3.org/2000/svg';
  var PX_PER_IN = 96;
  var STYLE_PROPS = ['fill', 'fill-opacity', 'stroke', 'stroke-width', 'stroke-opacity', 'stroke-dasharray',
    'stroke-linecap', 'stroke-linejoin', 'opacity', 'font-family', 'font-size', 'font-weight', 'font-style',
    'font-variant-numeric', 'text-anchor', 'dominant-baseline', 'letter-spacing', 'paint-order', 'visibility',
    'shape-rendering'];

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function download(blob, name) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url; a.download = name; a.rel = 'noopener';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
  }

  var PRESETS = {
    paper: { name: 'paper', widthPx: 528, dpi: 300, cls: 'preset-paper', label: 'Paper' }
  };

  function renderOffscreen(fn, preset) {
    var p = typeof preset === 'string' ? PRESETS[preset] : (preset || PRESETS.paper);
    var host = document.createElement('div');
    host.className = 'paper-offscreen ' + p.cls;
    host.style.width = p.widthPx + 'px';
    document.body.appendChild(host);
    fn(host, p.widthPx, p.name);
    var svg = host.querySelector('svg');
    if (!svg) { document.body.removeChild(host); throw new Error('the figure drew no SVG'); }
    svg.__paperHost = host;
    return svg;
  }
  function release(svg) {
    var host = svg && svg.__paperHost;
    if (host && host.parentNode) host.parentNode.removeChild(host);
  }

  // Inline the computed presentation of every element so the file renders the same anywhere.
  function inlineStyles(src, dst) {
    if (src.nodeType !== 1) return;
    var cs = getComputedStyle(src), parts = [];
    for (var i = 0; i < STYLE_PROPS.length; i++) {
      var v = cs.getPropertyValue(STYLE_PROPS[i]);
      if (v && v !== 'normal' && v !== 'auto' && !(STYLE_PROPS[i] === 'visibility' && v === 'visible')) parts.push(STYLE_PROPS[i] + ':' + v);
    }
    dst.setAttribute('style', parts.join(';'));
    dst.removeAttribute('class');
    dst.removeAttribute('tabindex');
    dst.removeAttribute('role');
    dst.removeAttribute('aria-label');
    var s = src.childNodes, d = dst.childNodes;
    for (var k = 0; k < s.length; k++) inlineStyles(s[k], d[k]);
  }

  function svgMarkup(svg, opt) {
    opt = opt || {};
    var vb = svg.viewBox && svg.viewBox.baseVal && svg.viewBox.baseVal.width ? svg.viewBox.baseVal : null;
    var w = vb ? vb.width : svg.getBoundingClientRect().width;
    var h = vb ? vb.height : svg.getBoundingClientRect().height;
    var clone = svg.cloneNode(true);
    inlineStyles(svg, clone);
    // drop invisible hit targets and anything hidden: they add bytes and confuse editors
    Array.prototype.slice.call(clone.querySelectorAll('[style*="visibility:hidden"]')).forEach(function (n) { n.parentNode.removeChild(n); });
    clone.setAttribute('xmlns', SVGNS);
    clone.setAttribute('viewBox', (vb ? vb.x + ' ' + vb.y + ' ' : '0 0 ') + w + ' ' + h);
    clone.setAttribute('width', (w / PX_PER_IN).toFixed(3) + 'in');
    clone.setAttribute('height', (h / PX_PER_IN).toFixed(3) + 'in');
    clone.removeAttribute('style');
    clone.setAttribute('style', 'background:#ffffff');
    var bg = document.createElementNS(SVGNS, 'rect');
    bg.setAttribute('x', vb ? vb.x : 0); bg.setAttribute('y', vb ? vb.y : 0);
    bg.setAttribute('width', w); bg.setAttribute('height', h); bg.setAttribute('fill', '#ffffff');
    clone.insertBefore(bg, clone.firstChild);
    if (opt.title) {
      var t = document.createElementNS(SVGNS, 'title');
      t.textContent = opt.title;
      clone.insertBefore(t, clone.firstChild);
    }
    return { markup: '<?xml version="1.0" encoding="UTF-8"?>\n' + new XMLSerializer().serializeToString(clone), w: w, h: h };
  }

  function exportSVG(svg, name, opt) {
    var m = svgMarkup(svg, opt);
    download(new Blob([m.markup], { type: 'image/svg+xml;charset=utf-8' }), name + '.svg');
  }

  function exportPNG(svg, name, opt) {
    opt = opt || {};
    var m = svgMarkup(svg, opt);
    var scale = opt.scale || (opt.dpi || 300) / PX_PER_IN;
    var img = new Image();
    return new Promise(function (resolve, reject) {
      img.onload = function () {
        var c = document.createElement('canvas');
        c.width = Math.round(m.w * scale); c.height = Math.round(m.h * scale);
        var ctx = c.getContext('2d');
        ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, c.width, c.height);
        ctx.drawImage(img, 0, 0, c.width, c.height);
        c.toBlob(function (b) { if (b) { download(b, name + '.png'); resolve(); } else reject(new Error('PNG encoding failed')); }, 'image/png');
      };
      img.onerror = function () { reject(new Error('the SVG could not be rasterised')); };
      img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(m.markup);
    });
  }

  // fn(host, widthPx, presetName) must draw the figure into host as on screen, at that width.
  function exportFigure(fn, name, presetName, fmt, opt) {
    var p = PRESETS[presetName] || PRESETS.paper;
    opt = Object.assign({ dpi: p.dpi, scale: p.scale }, opt || {});
    var svg = renderOffscreen(fn, p);
    var file = name;
    try {
      if (fmt === 'png') return exportPNG(svg, file, opt).then(function () { release(svg); }, function (e) { release(svg); throw e; });
      exportSVG(svg, file, opt);
    } finally { if (fmt !== 'png') release(svg); }
    return Promise.resolve();
  }

  function copyText(text) {
    function fallback() {
      var ta = document.createElement('textarea');
      ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      var ok = false; try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      document.body.removeChild(ta);
      return ok;
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function () { return true; }, function () { return fallback(); });
    }
    return Promise.resolve(fallback());
  }

  /* The quiet export row for one figure: the current view re-laid out at 5.5 in.
   * spec = { draw: fn(host, widthPx, presetName), name: 'file-stem' | fn,
   *          caption: fn -> 'Figure N: ...' (stored as the SVG <title>), status: element } */
  function exportMenu(container, spec) {
    var label = document.createElement('span');
    label.textContent = 'Export (5.5 in)';
    container.appendChild(label);
    [['svg', 'SVG', 'Vector figure at the NeurIPS text width (5.5 in), for LaTeX'],
     ['png', 'PNG', '5.5 in wide at 300 dpi']
    ].forEach(function (o) {
      var b = document.createElement('button');
      b.type = 'button'; b.className = 'btn';
      b.textContent = o[1];
      b.title = o[2];
      b.addEventListener('click', function () {
        b.disabled = true;
        var name = typeof spec.name === 'function' ? spec.name() : spec.name;
        var cap = spec.caption ? spec.caption() : '';
        Promise.resolve()
          .then(function () { return exportFigure(spec.draw, name, 'paper', o[0], { title: cap }); })
          .then(function () { if (spec.status) spec.status.textContent = 'Saved ' + name + '.' + o[0]; })
          .catch(function (e) { console.error(e); if (spec.status) spec.status.textContent = 'Export failed: ' + e.message; })
          .then(function () { b.disabled = false; });
      });
      container.appendChild(b);
    });
  }

  /* Village days. Day N is the calendar date (in timeZone) N-1 days after dayOne, e.g. the AI
   * Village counts day 1 = 2025-04-02 in Pacific time. */
  function days(dayOneISO, timeZone) {
    if (!dayOneISO) return null;
    var tz = timeZone || 'UTC';
    var p = dayOneISO.split('-').map(Number), base = Date.UTC(p[0], p[1] - 1, p[2]);
    var fmt = new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
    function wall(ms) {
      var o = {}; fmt.formatToParts(new Date(ms)).forEach(function (x) { o[x.type] = x.value; });
      return Date.UTC(+o.year, +o.month - 1, +o.day, +o.hour % 24, +o.minute, +o.second);
    }
    function of(ms) { var w = wall(ms); return Math.floor((w - base) / 864e5) + 1; }
    function start(n) {
      var local = base + (n - 1) * 864e5;          // local midnight as if it were UTC
      var guess = local - (wall(local) - local);   // shift by the zone offset, then settle DST
      return local - (wall(guess) - guess);
    }
    return { dayOne: dayOneISO, tz: tz, of: of, start: start, label: function (n) { return 'Day ' + n; } };
  }

  window.PaperKit = {
    PRESETS: PRESETS, PAPER_WIDTH_PX: 528, PX_PER_IN: PX_PER_IN,
    cssVar: cssVar, download: download, copyText: copyText, renderOffscreen: renderOffscreen, release: release,
    svgMarkup: svgMarkup, exportSVG: exportSVG, exportPNG: exportPNG, exportFigure: exportFigure,
    exportMenu: exportMenu, days: days
  };
})();
