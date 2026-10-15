/* Collaborative Intelligence: the data figures. Each is drawn from site/data at its container's width. */
(function () {
  'use strict';
  const CI = window.CI, { $, $$, el, svg, rc, fmt0, fmt1 } = CI;
  const fullName = m => m.name;

  // ---------------------------------------------------------------- shared: wrap SVG text into lines
  function wrapText(text, maxChars) {
    const words = text.split(' '), lines = []; let cur = '';
    for (const w of words) { if ((cur + ' ' + w).trim().length > maxChars && cur) { lines.push(cur); cur = w; } else cur = (cur + ' ' + w).trim(); }
    if (cur) lines.push(cur);
    return lines;
  }
  function tlines(x, y, lines, attrs, lh) {
    return svg('text', Object.assign({ x, y }, attrs), ...lines.map((l, i) => svg('tspan', { x, dy: i ? (lh || 14) : 0, text: l })));
  }
  // keep labels at least `gap` apart, inside [lo, hi]
  function dodge(items, gap, lo, hi) {
    items.sort((a, b) => a.y - b.y);
    for (let i = 1; i < items.length; i++) if (items[i].ly - items[i - 1].ly < gap) items[i].ly = items[i - 1].ly + gap;
    const over = items.length ? items[items.length - 1].ly - hi : 0;
    if (over > 0) { items[items.length - 1].ly -= over; for (let i = items.length - 2; i >= 0; i--) if (items[i + 1].ly - items[i].ly < gap) items[i].ly = items[i + 1].ly - gap; }
    if (items.length && items[0].ly < lo) { const d = lo - items[0].ly; items.forEach(t => { t.ly += d; }); }
    return items;
  }

  // ---------------------------------------------------------------- Figure 1: the leaderboard
  // one colour per maker, so families read at a glance; the whiskers are the 95% interval
  const MAKER = { OpenAI: '#1f9d6a', Anthropic: '#d97757', DeepSeek: '#4d6bfe', 'Moonshot AI': '#e0443e', 'Z.ai': '#14a3a3', Alibaba: '#7b5ce6' };
  CI.makerColor = m => MAKER[m.maker] || '#8d97a8';
  CI.add(function hero() {
    const box = $('#chart-hero'), L = CI.D.leaderboard, T = [0, 25, 50, 75, 100];
    const ranked = L.models.slice().sort((a, b) => a.rank - b.rank);
    const shown = new Set(ranked.map(m => m.id));
    // the headline is the score on all scenarios; Public and Private are the same score on the released and the held-out
    // scenarios (benchmark/split.json, read by build_data.py)
    const split = document.body.classList.contains('landing') ? null : L.split;  // the landing page shows the headline score only
    // one tab for the overall score and one per dimension, the least solved dimension first
    const meanOn = k => { const v = ranked.map(m => m.dims[k]).filter(c => c && c.test !== null).map(c => c.test); return v.reduce((a, b) => a + b, 0) / v.length; };
    const dimKeys = Object.keys(ranked[0].dims).sort((a, b) => meanOn(a) - meanOn(b));
    let view = 'all';
    const on = m => { if (view === 'all') return { s: m.score, ci: m.ci }; const c = m.dims[view]; return c && c.test !== null ? { s: c.test, ci: c.test_ci } : null; };
    const tip = m => {
      if (view === 'all') return el('div', null, el('strong', { text: m.name }), el('table', null, CI.trow('Score', fmt1(m.score)), CI.trow('95% interval', `${fmt1(m.ci[0])} to ${fmt1(m.ci[1])}`),
        split ? CI.trow(`Public (${split.n_public} released)`, fmt1(m.public)) : null, split ? CI.trow(`Private (${split.n_private} held out)`, fmt1(m.private)) : null,
        CI.trow('Alone', fmt1(m.alone) + '%'), CI.trow('Baseline', m.baseline === null ? 'not run' : fmt1(m.baseline) + '%'), CI.trow('Scenarios scored', `${m.n_gated} of ${m.n_scenarios}`)));
      const c = m.dims[view], head = el('strong', { text: `${m.name} · ${CI.dim(view).name}` });
      if (!c || c.test === null) return el('div', null, head, el('p', { text: c && c.not_run ? 'Not run: ' + c.not_run + '.' : 'Not run.' }));
      return el('div', null, head, el('table', null, CI.trow('Test pass rate', fmt1(c.test) + '%'), CI.trow('95% interval', `${fmt1(c.test_ci[0])} to ${fmt1(c.test_ci[1])}`),
        CI.trow('Alone', fmt1(c.alone) + '%'), CI.trow('Baseline', c.baseline === null ? 'not run' : fmt1(c.baseline) + '%'), CI.trow('Scenarios scored', `${c.n_gated} of ${c.n_scenarios}`)));
    };
    const rowFor = m => {
      const v = on(m), sp = view === 'all' ? split : null;
      const r = el('div', { class: 'lbr' + (v ? '' : ' na'), tabindex: 0, 'aria-label': v ? `${m.name}: ${fmt1(v.s)}, 95% interval ${fmt1(v.ci[0])} to ${fmt1(v.ci[1])}` + (sp ? `; public ${fmt1(m.public)}, private ${fmt1(m.private)}` : '') : `${m.name}: not run` },
        el('span', { class: 'lb' }, CI.logo(m), el('span', { class: 'nm', text: m.name }), m.open ? el('span', { class: 'tg', text: '[open]' }) : null, m.recent ? el('span', { class: 'tg', text: '[new]' }) : null),
        v ? el('span', { class: 'tr' },
          el('i', { class: 'bar', style: `width:${v.s}%; background:${CI.makerColor(m)}` }),
          el('i', { class: 'wh', style: `left:${v.ci[0]}%; width:${v.ci[1] - v.ci[0]}%` }),
          el('i', { class: 'mk', style: `left:${v.s}%` })) : el('span', { class: 'tr' }),
        v ? el('span', { class: 'vl' }, el('b', { text: fmt1(v.s) }), el('span', { class: 'rg', text: `${fmt0(v.ci[0])}–${fmt0(v.ci[1])}` })) : el('span', { class: 'vl', text: 'not run' }),
        sp ? el('span', { class: 'pp pu', text: fmt1(m.public) }) : null, sp ? el('span', { class: 'pp pr', text: fmt1(m.private) }) : null);
      CI.tipOn(r, () => tip(m));
      r.__m = m;
      return r;
    };
    // the model filter, as on most public leaderboards
    const count = el('span', { class: 'cnt' });
    const btn = el('button', { type: 'button', class: 'lb-filter', 'aria-expanded': 'false', 'aria-controls': 'lb-filter-menu' }, 'Models ', count, el('span', { class: 'chev', html: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 4.5 6 7.5 9 4.5"/></svg>' }));
    const checks = ranked.map(m => { const c = el('input', { type: 'checkbox', checked: true }); c.addEventListener('change', () => { if (c.checked) shown.add(m.id); else shown.delete(m.id); paint(); }); return el('label', null, c, CI.logo(m), m.name); });
    const quick = (t, f) => el('button', { type: 'button', text: t, onclick: () => { shown.clear(); ranked.filter(f).forEach(m => shown.add(m.id)); paint(); } });
    const menu = el('div', { class: 'lb-menu', id: 'lb-filter-menu', hidden: true },
      el('div', { class: 'quick' }, quick('All', () => true), quick('Frontier', m => !m.open), quick('Open weights', m => m.open), quick('Main runs', m => !m.recent)), el('div', { class: 'list' }, ...checks));
    btn.addEventListener('click', e => { e.stopPropagation(); menu.hidden = !menu.hidden; btn.setAttribute('aria-expanded', String(!menu.hidden)); });
    menu.addEventListener('click', e => e.stopPropagation());
    document.addEventListener('click', () => { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && !menu.hidden) { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); btn.focus(); } });
    const axis = () => el('div', { class: 'lbr axis' }, el('span', { class: 'lb' }), el('span', { class: 'tr' }, ...T.map(t => el('span', { style: `left:${t}%`, text: t }))), el('span', { class: 'vl' }));
    const hcell = (cls, name, n, title) => el('span', { class: cls, title }, el('b', { text: name }), el('i', { text: n }));
    const head = () => el('div', { class: 'lbr hd', 'aria-hidden': 'true' }, el('span', { class: 'lb', text: 'Model' }), el('span', { class: 'tr' }),
      hcell('vl', 'All', CI.fmtN(split.n_public + split.n_private), 'Score on all scenarios'), hcell('pp pu', 'Public', CI.fmtN(split.n_public), 'Score on the released scenarios'),
      hcell('pp pr', 'Private', CI.fmtN(split.n_private), 'Score on the held-out scenarios'));
    const card = el('div', { class: 'lb-card' });
    const tabs = el('div', { class: 'lb-tabs', role: 'tablist', 'aria-label': 'Score shown' },
      ...[['all', 'Overall', 'The average over the eight dimensions'], ...dimKeys.map(k => [k, CI.dim(k).short, CI.dim(k).name])].map(([k, t, title]) =>
        el('button', { type: 'button', role: 'tab', 'data-k': k, title, text: t, 'aria-selected': String(k === view), onclick: () => { view = k; render(); } })));
    let rows = [];
    function render() {
      $$('button', tabs).forEach(b => b.setAttribute('aria-selected', String(b.dataset.k === view)));
      const sp = view === 'all' ? split : null, val = m => (on(m) || { s: -1 }).s;
      rows = ranked.slice().sort((a, b) => val(b) - val(a) || a.rank - b.rank).map(rowFor);
      card.className = 'lb-card' + (sp ? ' split' : '') + (card.classList.contains('anim') ? ' anim' : '');
      rc(card, sp ? head() : null, ...rows, axis());
      paint();
    }
    function paint() {
      rows.forEach(r => { r.hidden = !shown.has(r.__m.id); });
      checks.forEach((l, i) => { $('input', l).checked = shown.has(ranked[i].id); });
      count.textContent = `(${shown.size}/${ranked.length})`;
    }
    rc(box, el('div', { class: 'lb-head' }, el('h2', { class: 'lb-title', text: 'Leaderboard' }), el('div', { class: 'lb-fwrap' }, btn, menu)), tabs, card);
    render();
    if (!CI.reduced()) CI.onVisible(card, () => card.classList.add('anim'), 0.2);
  });

  // ---------------------------------------------------------------- results: where models fall short, dimension by dimension
  CI.add(function results() {
    const box = $('#chart-results'), L = CI.D.leaderboard, M = CI.M, g = CI.F.gap;
    if (!g) return;
    const rowsD = g.rows.slice().sort((a, b) => b.frontier - a.frontier);
    // the least solved dimension first (lowest mean over every model), so the open problems lead
    const meanOn = k => { const v = L.models.map(m => m.dims[k]).filter(c => c && c.test !== null).map(c => c.test); return v.reduce((a, b) => a + b, 0) / v.length; };
    const order = rowsD.map(r => r.dimension).sort((a, b) => meanOn(a) - meanOn(b));
    const dname = k => CI.dim(k).name;
    let view = 'model';  // the heatmap of every model on every dimension
    const tabs = el('div', { class: 'tabs', role: 'tablist', 'aria-label': 'View' });
    const body = el('div');
    rc(box, tabs, body);
    // red below half, blue above, white at half, so the unsolved cells stand out
    const heat = v => { const p = Math.max(0, Math.min(100, v)), lo = p < 50, mix = 4 + 1.32 * Math.abs(p - 50); return `background: color-mix(in oklab, var(${lo ? '--problem' : '--model'}) ${mix}%, white); color: var(--ink);`; };
    function render() {
      $$('button', tabs).forEach(b => b.setAttribute('aria-selected', String(b.dataset.k === view)));
      if (view === 'dim') {
        rc(body,
          el('div', { class: 'legend' }, el('span', null, el('i', { class: 'k sq acc' }), 'Frontier models'), el('span', null, el('i', { class: 'k sq', style: 'background:var(--open)' }), 'Open-weight models')),
          el('div', { class: 'dimbars' }, ...rowsD.map(r => {
            const rw = el('div', { class: 'db', tabindex: 0 },
              el('span', { class: 'lb', text: dname(r.dimension) }),
              el('span', { class: 'tr' }, ...[25, 50, 75].map(t => el('i', { class: 'gl', style: `left:${t}%` })),
                el('i', { class: 'b f', style: `width:${r.frontier}%` }), el('i', { class: 'b o', style: `width:${r.open}%` })),
              el('span', { class: 'vl' }, el('b', { text: fmt0(r.frontier) }), el('span', { text: fmt0(r.open) })));
            CI.tipOn(rw, () => el('div', null, el('strong', { text: dname(r.dimension) }), el('table', null, CI.trow('Frontier mean', fmt1(r.frontier) + '%'), CI.trow('Open-weight mean', fmt1(r.open) + '%'))));
            return rw;
          }), el('div', { class: 'db axis' }, el('span'), el('span', { class: 'tr' }, ...[0, 25, 50, 75, 100].map(t => el('span', { style: `left:${t}%`, text: t }))), el('span'))));
      } else {
        const ranked = L.models.slice().sort((a, b) => a.rank - b.rank);
        const head = el('tr', null, el('th', { class: 'm', text: 'Model' }), ...order.map(k => el('th', { title: dname(k), text: CI.dim(k).short })), el('th', { class: 'ov', text: 'Overall' }));
        const trs = ranked.map(m => el('tr', null,
          el('td', { class: 'm' }, el('span', { class: 'in' }, CI.logo(m), el('span', { class: m.open ? 'o' : '', text: m.name }))),
          ...order.map(k => {
            const c = m.dims[k];
            if (!c || c.test === null) { const v = el('span', { class: 'cell na', text: '–', tabindex: 0 }); CI.tipOn(v, () => el('div', null, el('strong', { text: `${m.name} · ${dname(k)}` }), el('p', { text: c && c.not_run ? 'Not run: ' + c.not_run + '.' : 'Not run.' }))); return el('td', null, v); }
            const v = el('span', { class: 'cell', style: heat(c.test), text: fmt0(c.test), tabindex: 0 });
            CI.tipOn(v, () => el('div', null, el('strong', { text: `${m.name} · ${dname(k)}` }), el('table', null, CI.trow('Test pass rate', fmt1(c.test) + '%'), CI.trow('95% interval', `${fmt0(c.test_ci[0])} to ${fmt0(c.test_ci[1])}`), CI.trow('Scenarios scored', `${c.n_gated} of ${c.n_scenarios}`))));
            return el('td', null, v);
          }),
          el('td', { class: 'ov', text: fmt1(m.score) })));
        rc(body, el('div', { class: 'heat-wrap' }, el('table', { class: 'heat' }, el('thead', null, head), el('tbody', null, ...trs))));
      }
    }
    tabs.remove();
    render();
  });

})();
