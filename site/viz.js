/* Tethera: the data, drawn. Plain SVG, the site's palette, no library. Each drawing reads one endpoint and redraws itself.
   Colours come from CSS variables so the drawings follow the page in light and dark. */
(function () {
  var API = 'https://api.tetheralabs.com';
  var css = getComputedStyle(document.documentElement);
  var C = function (name, fallback) { return (css.getPropertyValue(name) || fallback).trim(); };
  var ink = C('--fg', '#2a302e'), dim = C('--fg-3', '#7c8784'), rule = C('--rule', '#cfd0c8'), raddle = C('--raddle', '#c84a2f'), moss = C('--moss', '#626f6d'), card = C('--card', '#f8f4ec');
  var NS = 'http://www.w3.org/2000/svg';
  function el(tag, attrs, parent) { var e = document.createElementNS(NS, tag); for (var k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; }
  function fmt(n) { return n == null ? '–' : Number(n).toLocaleString('en-GB'); }
  function hourLabel(iso) { var d = new Date(iso); return d.getUTCHours().toString().padStart(2, '0') + ':00'; }

  /* 1. Herd minutes per hour, last 48 h: bars; largest herd of the window marked. */
  function herdTimeline(host, data) {
    var pts = data.points, W = 720, H = 220, L = 42, R = 12, T = 18, B = 34;
    host.innerHTML = '';
    var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, width: '100%', role: 'img', 'aria-label': 'Herd minutes per hour over the last 48 hours' }, host);
    var max = Math.max(1, Math.max.apply(null, pts.map(function (p) { return p.herd_minutes; })));
    var bw = (W - L - R) / pts.length;
    [0, 0.5, 1].forEach(function (f) {
      var y = T + (H - T - B) * (1 - f);
      el('line', { x1: L, x2: W - R, y1: y, y2: y, stroke: rule, 'stroke-width': 1 }, svg);
      el('text', { x: L - 6, y: y + 4, 'text-anchor': 'end', 'font-size': 11, fill: dim, 'font-family': 'IBM Plex Mono, monospace' }, svg).textContent = Math.round(max * f);
    });
    var peak = pts.reduce(function (a, p) { return p.largest_herd > (a ? a.largest_herd : 0) ? p : a; }, null);
    pts.forEach(function (p, i) {
      var h = (H - T - B) * (p.herd_minutes / max), x = L + i * bw;
      el('rect', { x: x + 1, y: H - B - h, width: Math.max(1, bw - 2), height: h, fill: p === peak ? raddle : ink, opacity: p === peak ? 1 : 0.85 }, svg);
      if (i % 6 === 0) el('text', { x: x + bw / 2, y: H - B + 16, 'text-anchor': 'middle', 'font-size': 11, fill: dim, 'font-family': 'IBM Plex Mono, monospace' }, svg).textContent = hourLabel(p.hour);
    });
    if (peak && peak.largest_herd) {
      var i = pts.indexOf(peak), x = L + i * bw + bw / 2;
      el('text', { x: Math.min(W - R - 4, Math.max(L + 4, x)), y: T - 4, 'text-anchor': i > pts.length / 2 ? 'end' : 'start', 'font-size': 12, fill: raddle, 'font-family': 'IBM Plex Mono, monospace' }, svg).textContent = 'largest herd: ' + fmt(peak.largest_herd) + ' wallets one way';
    }
    el('text', { x: W - R, y: H - 6, 'text-anchor': 'end', 'font-size': 11, fill: dim, 'font-family': 'IBM Plex Mono, monospace' }, svg).textContent = 'UTC, herd minutes per hour';
  }

  /* 2. Automated share per hour: a line between 0 and 100%. */
  function shareLine(host, data) {
    var pts = data.points.filter(function (p) { return p.automated_share != null; }), W = 720, H = 180, L = 42, R = 12, T = 14, B = 30;
    host.innerHTML = '';
    var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, width: '100%', role: 'img', 'aria-label': 'Share of swaps by automated wallets per hour' }, host);
    [0, 0.25, 0.5, 0.75, 1].forEach(function (f) {
      var y = T + (H - T - B) * (1 - f);
      el('line', { x1: L, x2: W - R, y1: y, y2: y, stroke: rule }, svg);
      el('text', { x: L - 6, y: y + 4, 'text-anchor': 'end', 'font-size': 11, fill: dim, 'font-family': 'IBM Plex Mono, monospace' }, svg).textContent = Math.round(f * 100) + '%';
    });
    var step = (W - L - R) / Math.max(1, pts.length - 1);
    var d = pts.map(function (p, i) { return (i ? 'L' : 'M') + (L + i * step).toFixed(1) + ' ' + (T + (H - T - B) * (1 - p.automated_share)).toFixed(1); }).join(' ');
    el('path', { d: d, fill: 'none', stroke: ink, 'stroke-width': 2 }, svg);
    var sc = pts.map(function (p, i) { return (i ? 'L' : 'M') + (L + i * step).toFixed(1) + ' ' + (T + (H - T - B) * (1 - (p.self_cancelling_share || 0))).toFixed(1); }).join(' ');
    el('path', { d: sc, fill: 'none', stroke: raddle, 'stroke-width': 1.5, 'stroke-dasharray': '4 3' }, svg);
    pts.forEach(function (p, i) { if (i % 6 === 0) el('text', { x: L + i * step, y: H - B + 16, 'text-anchor': 'middle', 'font-size': 11, fill: dim, 'font-family': 'IBM Plex Mono, monospace' }, svg).textContent = hourLabel(p.hour); });
    el('text', { x: W - R, y: H - 6, 'text-anchor': 'end', 'font-size': 11, fill: dim, 'font-family': 'IBM Plex Mono, monospace' }, svg).textContent = 'solid: swaps by software · dashed: self-cancelling';
  }

  /* 3. The flock now: one mark per automated wallet active in the last minute, grouped by pool, herds raddled. */
  function flock(host, data) {
    var pools = data.pools.slice(0, 24), W = 720, pad = 14, dot = 7, gap = 3, perRow = 28;
    host.innerHTML = '';
    var rows = [], y = pad;
    pools.forEach(function (p) {
      var n = Math.max(p.buy0, p.sell0), lines = Math.ceil(n / perRow);
      rows.push({ p: p, y: y, n: n }); y += lines * (dot + gap) + 22;
    });
    var H = Math.max(80, y + pad);
    var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, width: '100%', role: 'img', 'aria-label': 'Automated wallets active in the last minute, by pool' }, host);
    if (!pools.length) { el('text', { x: W / 2, y: 40, 'text-anchor': 'middle', 'font-size': 14, fill: dim, 'font-family': 'IBM Plex Mono, monospace' }, svg).textContent = 'no automated wallets in the last minute'; return; }
    rows.forEach(function (r) {
      var p = r.p, col = p.verdict === 'herd' ? raddle : p.verdict === 'busy' ? moss : ink;
      el('text', { x: pad, y: r.y + 10, 'font-size': 12, fill: dim, 'font-family': 'IBM Plex Mono, monospace' }, svg).textContent = p.pair + ' · ' + p.verdict + ' · ' + fmt(r.n) + (p.verdict === 'herd' ? ' one way' : '');
      for (var i = 0; i < r.n; i++) {
        var cx = pad + 4 + (i % perRow) * (dot + gap), cy = r.y + 20 + Math.floor(i / perRow) * (dot + gap);
        el('rect', { x: cx, y: cy, width: dot, height: dot, rx: 2, fill: col, opacity: p.verdict === 'calm' ? 0.55 : 1 }, svg);
      }
    });
  }

  /* 4. Real against printed: one divided bar for the window. */
  function realBar(host, data) {
    var pts = data.points, sw = 0, sc = 0;
    pts.forEach(function (p) { sw += p.swaps; if (p.self_cancelling_share != null) sc += p.swaps * p.self_cancelling_share; });
    var share = sw ? sc / (sw + sc) : 0, W = 720, H = 56;
    host.innerHTML = '';
    var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, width: '100%', role: 'img', 'aria-label': 'Real against self-cancelling swaps' }, host);
    el('rect', { x: 0, y: 10, width: W, height: 22, fill: ink, rx: 3 }, svg);
    el('rect', { x: W * (1 - share), y: 10, width: W * share, height: 22, fill: raddle }, svg);
    el('text', { x: 0, y: 50, 'font-size': 12, fill: dim, 'font-family': 'IBM Plex Mono, monospace' }, svg).textContent = fmt(Math.round(sw)) + ' real swaps in ' + data.hours + ' h';
    el('text', { x: W, y: 50, 'text-anchor': 'end', 'font-size': 12, fill: raddle, 'font-family': 'IBM Plex Mono, monospace' }, svg).textContent = Math.round(share * 1000) / 10 + '% self-cancelling, removed';
  }

  function draw() {
    var hosts = document.querySelectorAll('[data-viz]');
    if (!hosts.length) return;
    var need = {};
    hosts.forEach(function (h) { need[h.dataset.viz === 'flock' ? 'flock' : 'series'] = true; });
    if (need.series) fetch(API + '/v1/series?hours=48').then(function (r) { return r.json(); }).then(function (d) {
      hosts.forEach(function (h) {
        if (h.dataset.viz === 'herds') herdTimeline(h, d);
        if (h.dataset.viz === 'share') shareLine(h, d);
        if (h.dataset.viz === 'real') realBar(h, d);
      });
    }).catch(function () {});
    if (need.flock) fetch(API + '/v1/flock').then(function (r) { return r.json(); }).then(function (d) {
      hosts.forEach(function (h) { if (h.dataset.viz === 'flock') flock(h, d); });
      var c = document.getElementById('flock-count'); if (c) c.textContent = fmt(d.automated_wallets);
    }).catch(function () {});
  }
  draw(); setInterval(draw, 60000);
})();
