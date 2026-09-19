/* Line Finder page — renders /line-finder/data. All the math is server-side;
   this file only draws: tiles, the win-% chart (inline SVG), per-line tables,
   the margin-trend chart, the backtest, the line history and the cups table.
   Vanilla JS, no dependencies. Everything user-visible is built with
   createElement/textContent (player names come from the DB). */
(function () {
  'use strict';

  var root = document.getElementById('line-finder');
  if (!root) return;

  var SVG_NS = 'http://www.w3.org/2000/svg';
  var FORMATS = ['wii', 'mk8dx', 'mixed'];
  var COLOR = { wii: 'var(--lf-wii)', mk8dx: 'var(--lf-switch)', mixed: 'var(--lf-mixed)' };
  var ALL_EQUAL = 366; // slider stop past 365 days = "all cups equal" (no weighting)
  var DEBOUNCE_MS = 150;

  var ui = {
    twoPlayer: document.getElementById('lf-two-player'),
    showActual: document.getElementById('lf-show-actual'),
    showFitted: document.getElementById('lf-show-fitted'),
    halfLife: document.getElementById('lf-half-life'),
    halfLifeValue: document.getElementById('lf-half-life-value'),
    status: document.getElementById('lf-status'),
    empty: document.getElementById('lf-empty'),
    body: document.getElementById('lf-body'),
    tiles: document.getElementById('lf-tiles'),
    legend: document.getElementById('lf-legend'),
    figure: document.getElementById('lf-figure'),
    chart: document.getElementById('lf-chart'),
    tip: document.getElementById('lf-tip'),
    chartNote: document.getElementById('lf-chart-note'),
    tabs: document.getElementById('lf-tabs'),
    tables: document.getElementById('lf-tables'),
    tableNote: document.getElementById('lf-table-note'),
    trendLegend: document.getElementById('lf-trend-legend'),
    trend: document.getElementById('lf-trend'),
    trendNote: document.getElementById('lf-trend-note'),
    backtest: document.getElementById('lf-backtest'),
    history: document.getElementById('lf-history'),
    cups: document.getElementById('lf-cups')
  };

  var state = {
    halfLife: root.dataset.halfLife === 'none' ? null : parseInt(root.dataset.halfLife, 10),
    twoPlayer: root.dataset.twoPlayer === '1',
    showActual: root.dataset.actual !== '0',
    showFitted: root.dataset.fitted !== '0',
    tab: 'wii',
    data: null,
    inflight: 0
  };

  // ---- tiny DOM helpers -------------------------------------------------

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === 'class') node.className = attrs[k];
        else if (k === 'text') node.textContent = attrs[k];
        else if (k === 'style') node.style.cssText = attrs[k];
        else node.setAttribute(k, attrs[k]);
      });
    }
    (children || []).forEach(function (c) {
      if (c == null) return;
      node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    });
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  function svgEl(tag, attrs, parent) {
    var e = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (parent) parent.appendChild(e);
    return e;
  }

  function svgText(text, attrs, parent) {
    var t = svgEl('text', attrs, parent);
    t.textContent = text;
    return t;
  }

  // ---- formatting -------------------------------------------------------

  function fmtLine(v) {
    if (v == null) return '—';
    if (v % 1 !== 0) {
      return (v < 0 ? '-' : '+') + Math.floor(Math.abs(v)) + '½';
    }
    if (v === 0) return 'even';
    return v > 0 ? '+' + v : String(v);
  }

  function fmtSigned(v, places) {
    if (v == null) return '—';
    var s = v.toFixed(places == null ? 1 : places);
    return v >= 0 ? '+' + s : s;
  }

  function pct(v) {
    return v == null ? '—' : Math.round(v) + '%';
  }

  function fmtDate(iso) {
    var d = new Date(iso.slice(0, 10) + 'T00:00:00');
    if (isNaN(d)) return iso.slice(0, 10);
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  }

  function fmtDateShort(iso) {
    var d = new Date(iso.slice(0, 10) + 'T00:00:00');
    if (isNaN(d)) return iso.slice(0, 10);
    var opts = { month: 'short', day: 'numeric' };
    // Any cup outside the current year carries its year, so a 1900 or 9999
    // row can never pass for one of this year's.
    if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
    return d.toLocaleDateString(undefined, opts);
  }

  function plural(n, word) {
    return n + ' ' + word + (n === 1 ? '' : 's');
  }

  function lineIndex(d, L) {
    return d.lines.indexOf(L);
  }

  // "k of n" with ties shown as halves, e.g. "12+1½ of 20".
  function countText(fm, i) {
    var w = fm.actual_wins[i], t = fm.actual_ties[i], n = fm.actual_n;
    return (t ? w + '+' + t + '½' : String(w)) + ' of ' + n;
  }

  function actualLabel(d, f) {
    var fm = d.formats[f];
    if (f === 'mixed' && fm.actual_source === 'pairs') return 'Mixed · from Wii×Switch pairs';
    return fm.label + ' · actual cups';
  }

  function outcomeName(d, o) {
    if (o === 'a') return d.players.a;
    if (o === 'b') return d.players.b;
    if (o === 'tie') return 'tie';
    return '—';
  }

  // ---- controls ---------------------------------------------------------

  function sliderFromHalfLife(hl) {
    return hl == null ? ALL_EQUAL : hl;
  }

  function halfLifeFromSlider(v) {
    v = parseInt(v, 10);
    return v >= ALL_EQUAL ? null : v;
  }

  function halfLifeText(hl) {
    return hl == null ? 'all cups equal' : hl + ' days';
  }

  function syncControls() {
    ui.twoPlayer.checked = state.twoPlayer;
    ui.showActual.checked = state.showActual;
    ui.showFitted.checked = state.showFitted;
    ui.halfLife.value = sliderFromHalfLife(state.halfLife);
    ui.halfLifeValue.textContent = halfLifeText(state.halfLife);
    root.classList.toggle('lf-hide-actual', !state.showActual);
    root.classList.toggle('lf-hide-fitted', !state.showFitted);
  }

  function queryString() {
    var q = ['half_life=' + (state.halfLife == null ? 'none' : state.halfLife),
             'two_player=' + (state.twoPlayer ? 1 : 0)];
    if (!state.showActual) q.push('actual=0');
    if (!state.showFitted) q.push('fitted=0');
    return q.join('&');
  }

  function updateUrl() {
    try {
      history.replaceState(null, '', window.location.pathname + '?' + queryString());
    } catch (e) { /* ignore */ }
  }

  function setStatus(text) {
    ui.status.textContent = text || '';
    ui.status.hidden = !text;
  }

  var debounceTimer = null;
  function scheduleFetch() {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(load, DEBOUNCE_MS);
  }

  function load() {
    debounceTimer = null;
    updateUrl();
    var url = root.dataset.endpoint + '?half_life=' + (state.halfLife == null ? 'none' : state.halfLife) +
      '&two_player=' + (state.twoPlayer ? 1 : 0);
    var seq = ++state.inflight;
    setStatus('Updating…');
    fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (data) {
        if (seq !== state.inflight) return; // a newer request superseded this one
        state.data = data;
        setStatus('');
        render();
      })
      .catch(function (err) {
        if (seq !== state.inflight) return;
        setStatus("Couldn't load the numbers (" + (err && err.message ? err.message : 'error') + '). Try reloading.');
      });
  }

  document.getElementById('lf-controls').addEventListener('submit', function (e) { e.preventDefault(); });
  ui.twoPlayer.addEventListener('change', function () {
    state.twoPlayer = ui.twoPlayer.checked;
    load();
  });
  ui.showActual.addEventListener('change', function () {
    state.showActual = ui.showActual.checked;
    syncControls();
    updateUrl();
    if (state.data) { drawChart(state.data); renderLegend(state.data); }
  });
  ui.showFitted.addEventListener('change', function () {
    state.showFitted = ui.showFitted.checked;
    syncControls();
    updateUrl();
    if (state.data) { drawChart(state.data); renderLegend(state.data); }
  });
  ui.halfLife.addEventListener('input', function () {
    state.halfLife = halfLifeFromSlider(ui.halfLife.value);
    ui.halfLifeValue.textContent = halfLifeText(state.halfLife);
    scheduleFetch();
  });
  ui.halfLife.addEventListener('change', function () {
    state.halfLife = halfLifeFromSlider(ui.halfLife.value);
    ui.halfLifeValue.textContent = halfLifeText(state.halfLife);
    scheduleFetch();
  });

  // ---- render -----------------------------------------------------------

  function render() {
    var d = state.data;
    if (!d.available) {
      ui.body.hidden = true;
      ui.empty.hidden = false;
      clear(ui.empty).appendChild(el('p', { class: 'empty', text: d.message }));
      return;
    }
    if (d.n_cups === 0) {
      ui.body.hidden = true;
      ui.empty.hidden = false;
      var why = state.twoPlayer && d.n_all_cups > 0
        ? 'No completed cups with just ' + d.players.a + ' and ' + d.players.b + ' yet — untick the 2-player box to see all ' + plural(d.n_all_cups, 'cup') + '.'
        : 'No completed cups with both ' + d.players.a + ' and ' + d.players.b + ' yet.';
      clear(ui.empty).appendChild(el('p', { class: 'empty', text: why }));
      return;
    }
    ui.empty.hidden = true;
    ui.body.hidden = false;
    renderTiles(d);
    renderLegend(d);
    drawChart(d);
    renderTables(d);
    drawTrend(d);
    renderBacktest(d);
    renderHistory(d);
    renderCups(d);
  }

  // ---- tiles ------------------------------------------------------------

  function renderTiles(d) {
    clear(ui.tiles);
    FORMATS.forEach(function (f) {
      var fm = d.formats[f];
      var tile = el('div', { class: 'lf-tile', style: '--c:' + COLOR[f] });
      tile.appendChild(el('div', { class: 'lf-tile-k' }, [el('i', { 'aria-hidden': 'true' }), fm.label + ' cup']));
      var value;
      if (fm.rec == null) value = el('div', { class: 'lf-tile-v lf-tile-v-na', text: 'n/a' });
      else if (fm.rec === 0) value = el('div', { class: 'lf-tile-v', text: 'Even' });
      else value = el('div', { class: 'lf-tile-v' }, [d.players.b + ' ', el('span', { text: fmtLine(fm.rec) })]);
      tile.appendChild(value);
      var sub = [];
      if (fm.rec != null) sub.push(pct(fm.fitted_at_rec) + ' fitted', '±' + fm.se.toFixed(1));
      if (fm.rec_within_noise) sub.push('inside the noise');
      sub.push(plural(fm.n, 'cup'));
      tile.appendChild(el('div', { class: 'lf-tile-sub', text: sub.join(' · ') }));
      var notes = el('div', { class: 'lf-tile-n' });
      fm.notes.forEach(function (n) { notes.appendChild(el('div', { text: n })); });
      tile.appendChild(notes);
      ui.tiles.appendChild(tile);
    });
  }

  // ---- legend -----------------------------------------------------------

  function legendItem(label, color, dashed) {
    var swatch = el('b', { class: dashed ? 'lf-dash' : '', style: '--c:' + color });
    return el('span', {}, [swatch, label]);
  }

  function renderLegend(d) {
    clear(ui.legend);
    FORMATS.forEach(function (f) {
      var fm = d.formats[f];
      if (state.showActual && fm.actual) ui.legend.appendChild(legendItem(actualLabel(d, f), COLOR[f], false));
    });
    FORMATS.forEach(function (f) {
      var fm = d.formats[f];
      if (state.showFitted && fm.fitted) ui.legend.appendChild(legendItem(fm.label + ' · fitted', COLOR[f], true));
    });
    ui.legend.appendChild(legendItem('50 / 50', 'var(--color-faint)', true));

    var note = 'Solid steps count real cups (a tie at the line counts as half a win). Dashed curves are a normal fit centred on the recency-weighted per-race edge. ' +
      'Dots along the bottom are the individual cups — ' + d.players.a + "'s margin over " + d.players.b + ': filled = just the two of them, hollow = more players in the cup.';
    if (d.formats.mixed.actual_source === 'pairs') {
      note += ' No mixed cup has been played yet, so the solid Mixed line pairs every Wii cup with every Switch cup (' + d.formats.mixed.actual_n + ' combos' +
        (d.formats.mixed.pairs_capped ? ', most recent 100 cups per console' : '') + '), half of each standing in for its two races.';
    }
    if (d.skipped_unparseable_dates > 0) {
      var k = d.skipped_unparseable_dates;
      note += ' ' + k + (k === 1 ? ' cup has an unreadable stored date and is' : ' cups have unreadable stored dates and are') + ' left out.';
    }
    ui.chartNote.textContent = note;
  }

  // ---- the win-% chart --------------------------------------------------

  var chart = { X0: -10, X1: 35 };

  function drawChart(d) {
    var svg = clear(ui.chart);
    var title = svgEl('title', { id: 'lf-chart-title' }, svg);
    title.textContent = 'Percentage of cups ' + d.players.a + ' would have won at each line given to ' + d.players.b;

    var rugFormats = FORMATS.filter(function (f) { return d.formats[f].n > 0; });
    var W = 420, ML = 34, MR = 12, MT = 14, PH = 190;
    var AX = 30;                 // x-axis labels + caption
    var RUG = 13;                // per rug row
    var H = MT + PH + AX + 8 + rugFormats.length * RUG + 4;
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    var PW = W - ML - MR;
    var X0 = chart.X0, X1 = chart.X1;
    var x = function (v) { return ML + (v - X0) / (X1 - X0) * PW; };
    var y = function (v) { return MT + (100 - v) / 100 * PH; };
    chart.x = x; chart.y = y; chart.ML = ML; chart.PW = PW; chart.MT = MT; chart.PH = PH; chart.W = W;

    // grid + axes
    for (var v = 0; v <= 100; v += 25) {
      svgEl('line', { x1: ML, x2: ML + PW, y1: y(v), y2: y(v), stroke: v === 50 ? 'var(--color-faint)' : 'var(--color-border)',
        'stroke-width': v === 50 ? 1.2 : 1, 'stroke-dasharray': v === 50 ? '5 4' : 'none' }, svg);
      svgText(v + '%', { x: ML - 6, y: y(v) + 4, 'text-anchor': 'end', class: 'lf-axis' }, svg);
    }
    svgText('50 / 50', { x: ML + PW - 2, y: y(50) - 4, 'text-anchor': 'end', class: 'lf-cap' }, svg);
    for (var t = X0; t <= X1; t += 5) {
      svgEl('line', { x1: x(t), x2: x(t), y1: y(0), y2: y(0) + 4, stroke: 'var(--color-border-strong)' }, svg);
      svgText(t === 0 ? 'even' : (t > 0 ? '+' + t : String(t)), { x: x(t), y: y(0) + 15, 'text-anchor': 'middle', class: 'lf-axis' }, svg);
    }
    svgEl('line', { x1: ML, x2: ML + PW, y1: y(0), y2: y(0), stroke: 'var(--color-border-strong)' }, svg);
    svgText('points given to ' + d.players.b + ' at the start of the cup', { x: ML + PW / 2, y: y(0) + 28, 'text-anchor': 'middle', class: 'lf-cap' }, svg);

    // rug of real cups
    rugFormats.forEach(function (f, row) {
      var fm = d.formats[f];
      var ry = y(0) + AX + 8 + row * RUG + 4;
      svgText(fm.label, { x: ML - 6, y: ry + 3.5, 'text-anchor': 'end', class: 'lf-axis' }, svg);
      var counts = {};
      fm.margins.forEach(function (m) { counts[m] = (counts[m] || 0) + 1; });
      var seen = {};
      fm.margins.forEach(function (m, i) {
        var k = seen[m] || 0; seen[m] = k + 1;
        var jitter = (k - (counts[m] - 1) / 2) * 4;
        var two = fm.n_players[i] === 2;
        var cx = Math.max(ML, Math.min(ML + PW, x(m) + jitter));
        svgEl('circle', { cx: cx, cy: ry, r: 3.5, fill: two ? COLOR[f] : 'var(--color-surface)', stroke: COLOR[f], 'stroke-width': two ? 0 : 1.5 }, svg);
      });
    });

    function stepPath(arr) {
      var p = '';
      d.lines.forEach(function (L, i) {
        if (L < X0 || L > X1) return;
        var px = x(L), py = y(arr[i]);
        p += p ? 'H' + px + 'V' + py : 'M' + px + ',' + py;
      });
      return p;
    }
    function linePath(arr) {
      var p = '';
      d.lines.forEach(function (L, i) {
        if (L < X0 || L > X1) return;
        p += (p ? 'L' : 'M') + x(L) + ',' + y(arr[i]);
      });
      return p;
    }

    // fitted (dashed) first so the actual steps sit on top
    if (state.showFitted) {
      FORMATS.forEach(function (f) {
        var fm = d.formats[f];
        if (!fm.fitted) return;
        svgEl('path', { d: linePath(fm.fitted), fill: 'none', stroke: COLOR[f], 'stroke-width': 1.8, 'stroke-dasharray': '5 4', 'stroke-linejoin': 'round', opacity: 0.9 }, svg);
      });
    }
    if (state.showActual) {
      ['mixed', 'mk8dx', 'wii'].forEach(function (f) {
        var fm = d.formats[f];
        if (!fm.actual) return;
        svgEl('path', { d: stepPath(fm.actual), fill: 'none', stroke: COLOR[f], 'stroke-width': 2.2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }, svg);
      });
    }

    // recommended-line markers with direct labels
    var labelPos = { wii: [8, -8], mk8dx: [-8, -8], mixed: [8, 14] };
    FORMATS.forEach(function (f) {
      var fm = d.formats[f];
      if (fm.rec == null || !fm.fitted) return;
      var L = Math.max(X0, Math.min(X1, fm.rec));
      var i = lineIndex(d, L);
      var yv = i < 0 ? 50 : fm.fitted[i];
      svgEl('line', { x1: x(L), x2: x(L), y1: y(yv), y2: y(0), stroke: COLOR[f], 'stroke-width': 1, 'stroke-dasharray': '2 3', opacity: 0.7 }, svg);
      svgEl('circle', { cx: x(L), cy: y(yv), r: 4.5, fill: COLOR[f], stroke: 'var(--color-surface)', 'stroke-width': 2 }, svg);
      var dx = labelPos[f][0], dy = labelPos[f][1];
      var anchor = dx < 0 ? 'end' : 'start';
      if (x(L) + dx + 40 > ML + PW) { anchor = 'end'; dx = -8; }
      if (x(L) + dx - 40 < ML && anchor === 'end') { anchor = 'start'; dx = 8; }
      svgText(fm.label + ' ' + fmtLine(fm.rec), { x: x(L) + dx, y: y(yv) + dy, 'text-anchor': anchor, class: 'lf-lbl' }, svg);
    });

    // hover / touch
    var cross = svgEl('line', { x1: 0, x2: 0, y1: MT, y2: y(0), stroke: 'var(--color-faint)', 'stroke-width': 1, opacity: 0 }, svg);
    var hit = svgEl('rect', { x: ML, y: MT, width: PW, height: PH, fill: 'transparent' }, svg);
    chart.cross = cross;
    hit.addEventListener('mousemove', function (e) { showTip(d, e); });
    hit.addEventListener('mouseleave', hideTip);
    hit.addEventListener('touchstart', function (e) { showTip(d, e.touches[0]); }, { passive: true });
    hit.addEventListener('touchmove', function (e) { showTip(d, e.touches[0]); }, { passive: true });
  }

  function snapLine(L) {
    return Math.max(chart.X0, Math.min(chart.X1, Math.round(L * 2) / 2));
  }

  function tipRow(label, value, color) {
    return el('div', { class: 'lf-tip-r', style: '--c:' + color }, [
      el('span', {}, [el('i', { 'aria-hidden': 'true' }), label]),
      el('em', { text: value })
    ]);
  }

  function showTip(d, pt) {
    var r = ui.chart.getBoundingClientRect();
    if (!r.width) return;
    var sx = (pt.clientX - r.left) * chart.W / r.width;
    var L = snapLine(chart.X0 + (sx - chart.ML) / chart.PW * (chart.X1 - chart.X0));
    chart.cross.setAttribute('x1', chart.x(L));
    chart.cross.setAttribute('x2', chart.x(L));
    chart.cross.setAttribute('opacity', 0.7);
    var i = lineIndex(d, L);
    var tip = clear(ui.tip);
    tip.appendChild(el('div', { class: 'lf-tip-h', text: d.players.b + ' ' + fmtLine(L) }));
    FORMATS.forEach(function (f) {
      var fm = d.formats[f];
      if (state.showActual && fm.actual) {
        tip.appendChild(tipRow((f === 'mixed' && fm.actual_source === 'pairs' ? 'Mixed pairs' : fm.label + ' actual'),
          pct(fm.actual[i]) + ' · ' + countText(fm, i), COLOR[f]));
      }
      if (state.showFitted && fm.fitted) {
        tip.appendChild(tipRow(fm.label + ' fitted', pct(fm.fitted[i]), COLOR[f]));
      }
    });
    tip.hidden = false;
    var fr = ui.figure.getBoundingClientRect();
    var left = pt.clientX - fr.left + 14, top = pt.clientY - fr.top - 10;
    if (left + tip.offsetWidth > fr.width) left = pt.clientX - fr.left - tip.offsetWidth - 14;
    if (left < 0) left = 4;
    tip.style.left = left + 'px';
    tip.style.top = Math.max(0, top) + 'px';
  }

  function hideTip() {
    ui.tip.hidden = true;
    if (chart.cross) chart.cross.setAttribute('opacity', 0);
  }

  document.addEventListener('touchstart', function (e) {
    if (!ui.figure.contains(e.target)) hideTip();
  }, { passive: true });

  // ---- per-line tables --------------------------------------------------

  function renderTables(d) {
    clear(ui.tabs);
    clear(ui.tables);
    if (!d.formats[state.tab] || (!d.formats[state.tab].actual && !d.formats[state.tab].fitted)) {
      var first = FORMATS.filter(function (f) { return d.formats[f].actual || d.formats[f].fitted; })[0];
      if (first) state.tab = first;
    }
    FORMATS.forEach(function (f) {
      var fm = d.formats[f];
      var label = fm.label + (f === 'mixed' && fm.actual_source === 'pairs' ? ' (pairs)' : '') + ' · ' + plural(fm.n, 'cup');
      var btn = el('button', { type: 'button', class: 'lf-tab', role: 'tab', 'aria-selected': state.tab === f ? 'true' : 'false', 'data-format': f, text: label });
      btn.style.setProperty('--c', COLOR[f]);
      btn.addEventListener('click', function () {
        state.tab = f;
        renderTables(d);
      });
      ui.tabs.appendChild(btn);
    });

    var fm = d.formats[state.tab];
    var wrap = el('div', { class: 'lf-table-wrap' });
    var table = el('table', { class: 'table lf-table' });
    var thead = el('thead', {}, [el('tr', {}, [
      el('th', { text: d.players.b + ' gets' }),
      el('th', { class: 'num lf-col-actual', text: 'Actual' }),
      el('th', { class: 'num lf-col-actual', text: 'Cups' }),
      el('th', { class: 'num lf-col-fitted', text: 'Fitted' })
    ])]);
    table.appendChild(thead);
    var tbody = el('tbody');
    for (var L = -10; L <= 35; L++) {
      var i = lineIndex(d, L);
      var tr = el('tr', { class: L === fm.rec ? 'lf-rec' : '' }, [
        el('td', { text: fmtLine(L) + (L === fm.rec ? ' ★' : '') }),
        el('td', { class: 'num lf-col-actual', text: fm.actual ? pct(fm.actual[i]) : '—' }),
        el('td', { class: 'num lf-col-actual text-faint', text: fm.actual ? countText(fm, i) : '—' }),
        el('td', { class: 'num lf-col-fitted', text: fm.fitted ? pct(fm.fitted[i]) : '—' })
      ]);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    ui.tables.appendChild(wrap);

    var note = 'Actual = share of real cups ' + d.players.a + ' won with that line applied (a tie counts half). Fitted = the normal model. ★ = recommended line.';
    if (state.tab === 'mixed' && fm.actual_source === 'pairs') {
      note += ' Mixed actual is built from ' + fm.actual_n + ' Wii×Switch cup pairs' + (fm.pairs_capped ? ' (most recent 100 cups per console)' : '') + ' — no mixed cup has been played yet.';
    }
    if (!fm.fitted) note += ' Not enough cups for a fitted curve yet.';
    ui.tableNote.textContent = note;
  }

  // ---- margin trend -----------------------------------------------------

  function drawTrend(d) {
    var svg = clear(ui.trend);
    var pts = d.trend.points;
    clear(ui.trendLegend);
    if (!pts.length) { ui.trendNote.textContent = 'No cups yet.'; return; }
    FORMATS.forEach(function (f) {
      if (d.formats[f].n > 0) ui.trendLegend.appendChild(legendItem(d.formats[f].label, COLOR[f], false));
    });
    ['wii', 'mk8dx'].forEach(function (f) {
      if (d.trend.running[f].length > 1) ui.trendLegend.appendChild(legendItem(d.formats[f].label + ' · running mean', COLOR[f], true));
    });

    var W = 420, H = 240, ML = 34, MR = 12, MT = 12, MB = 28;
    var PW = W - ML - MR, PH = H - MT - MB;
    var toMs = function (iso) { return new Date(iso + 'T00:00:00').getTime(); };
    var xs = pts.map(function (p) { return toMs(p.date); });
    var xmin = Math.min.apply(null, xs), xmax = Math.max.apply(null, xs);
    var day = 86400000;
    if (xmax - xmin < day) { xmin -= day; xmax += day; }
    var ms = pts.map(function (p) { return p.margin; });
    var ymin = Math.floor(Math.min(-5, Math.min.apply(null, ms)) / 5) * 5;
    var ymax = Math.ceil(Math.max(5, Math.max.apply(null, ms)) / 5) * 5;
    var x = function (t) { return ML + (t - xmin) / (xmax - xmin) * PW; };
    var y = function (v) { return MT + (ymax - v) / (ymax - ymin) * PH; };

    var step = (ymax - ymin) > 40 ? 10 : 5;
    for (var v = ymin; v <= ymax; v += step) {
      svgEl('line', { x1: ML, x2: ML + PW, y1: y(v), y2: y(v), stroke: v === 0 ? 'var(--color-faint)' : 'var(--color-border)', 'stroke-width': v === 0 ? 1.2 : 1 }, svg);
      svgText(fmtLine(v) === 'even' ? '0' : (v > 0 ? '+' + v : String(v)), { x: ML - 6, y: y(v) + 4, 'text-anchor': 'end', class: 'lf-axis' }, svg);
    }
    var axisFmt = pts[0].date.slice(0, 4) !== pts[pts.length - 1].date.slice(0, 4) ? fmtDate : fmtDateShort;
    svgText(axisFmt(pts[0].date), { x: ML, y: H - 8, 'text-anchor': 'start', class: 'lf-axis' }, svg);
    svgText(axisFmt(pts[pts.length - 1].date), { x: ML + PW, y: H - 8, 'text-anchor': 'end', class: 'lf-axis' }, svg);
    svgText(d.players.a + "'s margin", { x: ML + PW / 2, y: H - 8, 'text-anchor': 'middle', class: 'lf-cap' }, svg);

    ['wii', 'mk8dx'].forEach(function (f) {
      var series = d.trend.running[f].filter(function (p) { return p.value != null; });
      if (series.length < 2) return;
      var p = '';
      series.forEach(function (s) { p += (p ? 'L' : 'M') + x(toMs(s.date)) + ',' + y(s.value); });
      svgEl('path', { d: p, fill: 'none', stroke: COLOR[f], 'stroke-width': 1.6, 'stroke-dasharray': '5 4', opacity: 0.85 }, svg);
    });
    pts.forEach(function (p) {
      var two = p.n_players === 2;
      var c = svgEl('circle', { cx: x(toMs(p.date)), cy: y(p.margin), r: 4, fill: two ? COLOR[p.format] : 'var(--color-surface)', stroke: COLOR[p.format], 'stroke-width': two ? 0 : 1.6 }, svg);
      var t = svgEl('title', {}, c);
      t.textContent = fmtDate(p.date) + ' · ' + d.formats[p.format].label + ' · ' + fmtSigned(p.margin, 0) + (two ? ' · 2 players' : ' · ' + p.n_players + ' players');
    });
    ui.trendNote.textContent = 'Each dot is one cup: ' + d.players.a + "'s raw score minus " + d.players.b + "'s, before any line (filled = just the two of them). " +
      'The dashed lines are the recency-weighted running mean for Wii and Switch, using the same half-life as above.';
  }

  // ---- backtest ---------------------------------------------------------

  function renderBacktest(d) {
    var box = clear(ui.backtest);
    var s = d.backtest.summary;
    var any = FORMATS.some(function (f) { return s[f].n > 0; });
    box.appendChild(el('p', { class: 'lf-note lf-note-top', text:
      'Walk the cups in order; for each one, recommend a line from only the cups played BEFORE it (same settings), apply it, and see who would have won — versus the line that was actually used (net of any line the other player had). A cup needs at least 12 earlier races — three cups\' worth — on each console involved, otherwise it is skipped as "not enough history".' }));
    if (!any) {
      box.appendChild(el('p', { class: 'empty', text: 'Not enough history to backtest yet.' }));
      return;
    }
    var wrap = el('div', { class: 'lf-table-wrap' });
    var table = el('table', { class: 'table lf-table lf-table-summary' });
    table.appendChild(el('thead', {}, [el('tr', {}, [
      el('th', { text: 'Format' }),
      el('th', { class: 'num', text: 'Cups' }),
      el('th', { class: 'num', text: 'Backtested line' }),
      el('th', { class: 'num', text: 'Line used' })
    ])]));
    var tbody = el('tbody');
    // One line per outcome so long player names wrap inside the cell instead
    // of pushing the table into a sideways scroll on a phone.
    var tally = function (c) {
      var lines = [d.players.a + ' ' + c.a, d.players.b + ' ' + c.b];
      if (c.tie) lines.push('tie ' + c.tie);
      return el('div', { class: 'lf-tally' }, lines.map(function (t) { return el('div', { text: t }); }));
    };
    FORMATS.forEach(function (f) {
      var row = s[f];
      tbody.appendChild(el('tr', {}, [
        el('td', { text: d.formats[f].label }),
        el('td', { class: 'num' }, [String(row.n), row.skipped ? el('div', { class: 'text-faint', text: '+' + row.skipped + ' skipped' }) : null]),
        el('td', { class: 'num lf-tally-cell' }, [row.n ? tally(row.rec) : '—']),
        el('td', { class: 'num lf-tally-cell' }, [row.n ? tally(row.used) : '—'])
      ]));
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    box.appendChild(wrap);

    var details = el('details', { class: 'lf-details' });
    details.appendChild(el('summary', { text: 'Every cup, one row each' }));
    var wrap2 = el('div', { class: 'lf-table-wrap' });
    var t2 = el('table', { class: 'table lf-table lf-table-sm' });
    t2.appendChild(el('thead', {}, [el('tr', {}, [
      el('th', { text: 'Date' }), el('th', { text: 'Format' }), el('th', { class: 'num', text: 'Margin' }),
      el('th', { class: 'num', text: 'Backtested' }), el('th', { text: 'Result' }),
      el('th', { class: 'num', text: 'Used' }), el('th', { text: 'Result' })
    ])]));
    var tb2 = el('tbody');
    d.backtest.rows.slice().reverse().forEach(function (r) {
      tb2.appendChild(el('tr', {}, [
        el('td', { text: fmtDateShort(r.date) }),
        el('td', { text: d.formats[r.format].label }),
        el('td', { class: 'num', text: fmtSigned(r.margin, 0) }),
        el('td', { class: 'num', text: r.rec_line == null ? 'not enough history' : fmtLine(r.rec_line) }),
        el('td', { text: outcomeName(d, r.rec_outcome) }),
        el('td', { class: 'num', text: fmtLine(r.line_used) }),
        el('td', { text: outcomeName(d, r.used_outcome) })
      ]));
    });
    t2.appendChild(tb2);
    wrap2.appendChild(t2);
    details.appendChild(wrap2);
    box.appendChild(details);
  }

  // ---- line history -----------------------------------------------------

  function renderHistory(d) {
    var box = clear(ui.history);
    var h = d.line_history;
    var stored = h.stored_line == null ? '—' : fmtLine(h.stored_line);
    box.appendChild(el('p', { class: 'lf-note lf-note-top', text:
      'The line ' + d.players.b + ' actually played with in each cup, and every recorded line change (before → after). Stored line in the app today: ' + stored + '.' }));
    if (!h.rows.length) {
      box.appendChild(el('p', { class: 'empty', text: 'No cups yet.' }));
      return;
    }
    var wrap = el('div', { class: 'lf-table-wrap' });
    var table = el('table', { class: 'table lf-table lf-table-sm' });
    table.appendChild(el('thead', {}, [el('tr', {}, [
      el('th', { text: 'Date' }), el('th', { text: 'Format' }),
      el('th', { class: 'num', text: 'Line used' }), el('th', { text: 'Change' })
    ])]));
    var tbody = el('tbody');
    h.rows.slice().reverse().forEach(function (r) {
      var change = r.line_before == null ? '' :
        (r.line_before === r.line_after ? fmtLine(r.line_before) + ' (no change)' : fmtLine(r.line_before) + ' → ' + fmtLine(r.line_after));
      tbody.appendChild(el('tr', {}, [
        el('td', { text: fmtDateShort(r.date) }),
        el('td', { text: d.formats[r.format].label }),
        el('td', { class: 'num', text: fmtLine(r.line_used) }),
        el('td', { class: change ? '' : 'text-faint', text: change || '—' })
      ]));
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    box.appendChild(wrap);
  }

  // ---- cups -------------------------------------------------------------

  function renderCups(d) {
    var box = clear(ui.cups);
    var wrap = el('div', { class: 'lf-table-wrap' });
    var table = el('table', { class: 'table lf-table lf-table-sm' });
    table.appendChild(el('thead', {}, [el('tr', {}, [
      el('th', { text: 'Date' }), el('th', { text: 'Format' }), el('th', { class: 'num', text: 'Players' }),
      el('th', { class: 'num', text: d.players.a }), el('th', { class: 'num', text: d.players.b }),
      el('th', { class: 'num', text: 'Margin' }), el('th', { class: 'num', text: 'Line' })
    ])]));
    var tbody = el('tbody');
    var editUrl = root.dataset.cupEditUrl || '';
    d.cups.forEach(function (c) {
      var dateCell = editUrl
        ? el('a', { href: editUrl.replace('/0/', '/' + c.id + '/'), title: 'Open this cup', text: fmtDateShort(c.date) })
        : fmtDateShort(c.date);
      tbody.appendChild(el('tr', {}, [
        el('td', {}, [dateCell]),
        el('td', { text: c.format_label }),
        el('td', { class: 'num', text: String(c.n_players) }),
        el('td', { class: 'num', text: String(c.a_score) }),
        el('td', { class: 'num', text: String(c.b_score) }),
        el('td', { class: 'num', text: fmtSigned(c.margin, 0) }),
        el('td', { class: 'num', text: fmtLine(c.line_used) })
      ]));
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    box.appendChild(wrap);
    box.appendChild(el('p', { class: 'lf-note', text:
      plural(d.cups.length, 'completed cup') + ' with both players' + (state.twoPlayer ? ' and nobody else' : '') +
      '. Margin is raw score minus raw score, before any line; "Line" is what ' + d.players.b + ' actually got in that cup.' }));
  }

  // ---- go ---------------------------------------------------------------

  syncControls();
  load();
})();
