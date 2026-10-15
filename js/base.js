/* Collaborative Intelligence: shared helpers, data loading and the full-run replay.
   Every number on the page comes from site/data/*.json (built by site/build_data.py); the article text names
   where each one goes with data-k. figures.js and explain.js register their renderers with CI.add(). */
(function () {
  'use strict';
  const CI = window.CI = { D: {}, M: {}, F: {}, V: {}, parts: [] };
  const $ = CI.$ = (s, el) => (el || document).querySelector(s);
  const $$ = CI.$$ = (s, el) => Array.from((el || document).querySelectorAll(s));
  const clean = kids => kids.flat(Infinity).filter(k => k !== null && k !== undefined && k !== false);
  const el = CI.el = (tag, attrs, ...kids) => {
    const n = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else if (k === 'html') n.innerHTML = v;
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? '' : v);
    }
    for (const k of clean(kids)) n.append(k.nodeType ? k : document.createTextNode(String(k)));
    return n;
  };
  const NS = 'http://www.w3.org/2000/svg';
  const svg = CI.svg = (tag, attrs, ...kids) => {
    const n = document.createElementNS(NS, tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'text') n.textContent = v;
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v);
    }
    for (const k of clean(kids)) n.append(k.nodeType ? k : document.createTextNode(String(k)));
    return n;
  };
  const rc = CI.rc = (node, ...kids) => { if (node) node.replaceChildren(...clean(kids)); };
  CI.fmt0 = v => (v === null || v === undefined) ? '–' : Math.round(v).toString();
  CI.fmt1 = v => (v === null || v === undefined) ? '–' : (Math.round(v * 10) / 10).toFixed(1);
  CI.fmtN = v => typeof v === 'number' ? v.toLocaleString('en-US') : String(v);
  CI.low = s => s.charAt(0).toLowerCase() + s.slice(1);
  CI.andList = a => a.length < 2 ? (a[0] || '') : a.slice(0, -1).join(', ') + ' and ' + a[a.length - 1];
  CI.mean = a => a.reduce((x, y) => x + y, 0) / a.length;
  CI.sleep = ms => new Promise(r => setTimeout(r, ms));
  CI.reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  CI.add = fn => CI.parts.push(fn);
  // the deploy stamps one version on the page's scripts, styles and data, so a browser never mixes old and new files
  const VERSION = (() => { try { return new URL(document.currentScript.src).searchParams.get('v') || ''; } catch (e) { return ''; } })();
  CI.onVisible = (node, fn, threshold) => {
    const io = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) { io.disconnect(); fn(); } }), { threshold: threshold || 0.3 });
    io.observe(node);
  };
  // charts are drawn at their container's width and redrawn when it changes, so text stays at its set size
  CI.responsive = (box, draw) => {
    let w = 0;
    const go = () => { const nw = box.clientWidth; if (nw && nw !== w) { w = nw; draw(nw); } };
    new ResizeObserver(go).observe(box);
    go();
  };
  // a value quoted from a run, checked against the text it comes from: a number the page shows is never typed in
  CI.num = (text, re, label) => {
    const m = text && text.match(re);
    if (!m) { console.error('quoted value not found:', label || re); return null; }
    return Number(m[1].replace(/,/g, ''));
  };

  // ---------------------------------------------------------------- names
  // "Taking over a compromised session" is shown as "Handoff integrity": the capability the dimension measures
  const RENAME = { taking_over: { name: 'Handoff integrity', short: 'Handoff' } };
  CI.dim = key => CI.D.dimensions.dimensions.find(d => d.key === key) || { key, name: key, short: key };
  CI.byName = name => CI.D.leaderboard.models.find(x => x.name === name) || null;
  CI.isOpen = id => !!(CI.M[id] && CI.M[id].open);

  // ---------------------------------------------------------------- avatars and logos
  CI.logo = (m, cls) => {
    if (!m.logo) return el('span', { class: 'nologo' + (cls ? ' ' + cls : ''), 'aria-hidden': 'true', text: m.maker.slice(0, 1) });
    const img = el('img', { src: m.logo, alt: '', class: cls || null });
    img.addEventListener('error', () => img.replaceWith(el('span', { class: 'nologo', text: m.maker.slice(0, 1) })));
    return img;
  };
  CI.robot = (m, opts) => {
    opts = opts || {};
    const av = el('span', { class: 'av model-av' + (m ? '' : ' partner') + (opts.small ? ' small' : ''), title: m ? m.name : 'LLM colleague' });
    if (m) av.append(m.logo ? el('img', { src: m.logo, alt: '' }) : el('span', { class: 'ini', text: m.maker.slice(0, 1) }));
    return av;
  };
  CI.person = opts => {
    opts = opts || {};
    const t = opts.title || 'teammate';
    return el('span', { class: 'av person' + (opts.small ? ' small' : '') + (opts.cls ? ' ' + opts.cls : ''), title: t },
      el('span', { class: 'ini', text: t.replace(/^(the|a|an)\s+/i, '').charAt(0).toUpperCase() }));
  };

  // ---------------------------------------------------------------- tooltip (hover and keyboard focus; tap shows it too)
  const tip = () => $('#tooltip');
  CI.showTip = (target, node) => {
    const t = tip(); t.replaceChildren(node); t.hidden = false;
    const r = target.getBoundingClientRect(), tw = t.offsetWidth, th = t.offsetHeight;
    let x = r.left + r.width / 2 - tw / 2, y = r.top - th - 10;
    x = Math.max(8, Math.min(window.innerWidth - tw - 8, x));
    if (y < 64) y = r.bottom + 10;
    t.style.left = x + 'px'; t.style.top = y + 'px';
  };
  CI.hideTip = () => { const t = tip(); if (t) t.hidden = true; };
  CI.tipOn = (target, make) => {
    target.addEventListener('mouseenter', () => CI.showTip(target, make()));
    target.addEventListener('mouseleave', CI.hideTip);
    target.addEventListener('focus', () => CI.showTip(target, make()));
    target.addEventListener('blur', CI.hideTip);
  };
  window.addEventListener('scroll', CI.hideTip, { passive: true });
  // hover with a mouse, focus with a keyboard, tap with a finger: all three select, and a tap stays selected
  CI.hoverable = (node, on, off) => {
    let touch = false;
    node.addEventListener('pointerdown', e => { touch = e.pointerType !== 'mouse'; });
    node.addEventListener('pointerenter', e => { if (e.pointerType === 'mouse') on(); });
    node.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse') off(); });
    node.addEventListener('focus', () => on());
    node.addEventListener('blur', () => { if (!touch) off(); });
    node.addEventListener('click', () => on());
  };
  CI.trow = (k, v) => el('tr', null, el('td', { text: k }), el('td', { text: v }));

  // ---------------------------------------------------------------- the numbers the text refers to
  const PARTNERS = { 'claude-haiku-4-5': 'Claude Haiku 4.5', 'gpt-5-mini': 'GPT-5 mini', 'Qwen/Qwen3-14B': 'Qwen3-14B' };
  function computeValues() {
    const { D, M, F, V } = CI, L = D.leaderboard, C = D.dataset.counts, val = D.dataset.validation;
    const ranked = L.models.slice().sort((a, b) => a.rank - b.rank), main = ranked.filter(m => !m.recent);
    const fr = L.groups.frontier.ids, op = L.groups.open.ids;
    const dmean = (ids, k) => CI.mean(ids.map(id => M[id].dims[k].test).filter(v => v !== null));
    Object.assign(V, {
      nScen: C.scenarios, nDims: C.dimensions, perDim: C.per_dimension, nModels: L.models.length, nMain: main.length, nRecent: L.models.length - main.length,
      realWorld: C.real_world, benchmarks: C.benchmarks, nFamilies: D.dataset.families.length,
      seatsLo: C.seats[0], seatsHi: C.seats[1], roundsLo: C.rounds[0], roundsHi: C.rounds[1], sandbox: C.sandbox, chatOnly: C.chat_only,
      partners: CI.andList(C.partners.map(p => PARTNERS[p] || p)), bootReps: L.bootstrap.reps,
      topName: ranked[0].name, topScore: ranked[0].score, secondName: ranked[1].name, secondScore: ranked[1].score,
      lowScore: ranked[ranked.length - 1].score, lowName: ranked[ranked.length - 1].name,
      frontier: L.groups.frontier.score, open: L.groups.open.score, gapPts: L.groups.frontier.score - L.groups.open.score,
      frontierAlone: L.groups.frontier.alone, frontierBase: L.groups.frontier.baseline, openAlone: L.groups.open.alone, openBase: L.groups.open.baseline,
      notRunNote: L.not_run_note, auditNote: L.audit_note, readingsNote: L.readings_note,
      confFrontier: dmean(fr, 'handling_conflict'), eiFrontier: dmean(fr, 'emotional_intelligence'), eiOpen: dmean(op, 'emotional_intelligence'),
      shareFrontier: dmean(fr, 'sharing_and_seeking_information'), shareOpen: dmean(op, 'sharing_and_seeking_information'),
      judgeAgree: val.judge_agreement, judgeMsgs: val.judge_messages, reread: val.failed_runs_reread, relabelled: val.disputed_relabelled,
      initFrontier: dmean(fr, 'taking_initiative'), initOpen: dmean(op, 'taking_initiative'),
      nPublic: L.split.n_public, nPrivate: L.split.n_private, pubPerDim: L.split.per_dimension_public, privPerDim: L.split.per_dimension_private,
      nStress: L.stress_set.n_scenarios, stressPublic: L.split.stress_public, stressPrivate: L.split.stress_private, splitTakeBias: L.split.take_bias,
      judges: CI.andList(val.judges), runs: D.dataset.runs.runs, messagesK: D.dataset.runs.messages_k, seeds: C.seeds,
    });
    const s = F.safe, h = F.hardening, i = F.inherited, c = F.conflict, e = F.ei, n = F.initiative;
    if (s) { V.safeMin = s.frontier_safety_min; V.collabFrontier = CI.mean(s.rows.filter(r => !CI.isOpen(r.model)).map(r => r.other)); }
    if (h) Object.assign(V, { hardenRuns: h.frontier_test_runs, hardenMechs: h.mechanisms_tried, hardenGenuine: h.genuine_failures === 0 ? 'no' : h.genuine_failures,
      hardenModels: CI.andList(h.approval_table.map(r => r.Model)) });
    if (i) Object.assign(V, { takeTest: i.frontier.test, takeStart: i.frontier.start, takeSafety: i.frontier.safety, codeFailed: i.code.failed, codeRuns: i.code.runs, codeLeft: i.code.left,
      codePass: 100 * (1 - i.code.failed / i.code.runs), recFailed: i.records.failed, recRuns: i.records.runs, takeStates: i.states, codeStates: i.code_states });
    if (c) { const f = c.means.frontier; Object.assign(V, { confAlone: f.alone.best_deal, confNormal: f.normal.best_deal, confPushy: f.pushy.best_deal, confAsked: f.pushy.asked, confCaved: f.pushy.caved,
      confEffPushy: c.means.all.pushy.efficiency, humCasino: c.humans.CaSiNo.best_deal, humCasinoN: c.humans.CaSiNo.dialogues }); }
    if (e) Object.assign(V, { probeRuns: e.runs, probePrompted: e.prompted_both, probeUnprompted: e.unprompted_pass });
    if (n) {
      Object.assign(V, { initFails: n.failures, initNever: n.never_noticed, initNeverPct: n.never_pct });
      const fr = n.rows.filter(r => !CI.isOpen(r.model)), byRate = n.rows.slice().sort((x, y) => y.passed / y.tests - x.passed / x.tests);
      const b = byRate[0], w = byRate[byRate.length - 1];
      Object.assign(V, { initQuiet: CI.mean(fr.map(r => r.quiet_day)), initBusy: CI.mean(fr.map(r => r.busy_shift)),
        initBestName: M[b.model].name, initBest: `${b.passed} of ${b.tests}`, initWorstName: M[w.model].name, initWorst: `${w.passed} of ${w.tests}` });
    }
    if (CI.F.gap) { const g = CI.F.gap.rows.find(r => r.dimension === 'resisting_unsafe_pressure'); if (g) V.resistOpen = g.open; }
  }
  function fillValues() {
    for (const n of $$('[data-k]')) {
      const v = CI.V[n.dataset.k];
      if (v === undefined || v === null) { console.error('no value for', n.dataset.k); n.textContent = '–'; continue; }
      const f = n.dataset.f;
      n.textContent = typeof v === 'number' ? (f === '0' ? CI.fmt0(v) : f === '1' ? CI.fmt1(v) : CI.fmtN(v)) : v;
    }
  }

  // ---------------------------------------------------------------- the full run, replayed in a dialog
  function fileTree(R) {
    const S = R.summary || {}, read = new Set(S.read_paths || []), edited = new Set(S.edits || []);
    const keys = {}; (S.key_files || []).forEach(k => { keys[k.path] = k; });
    const root = { dirs: {}, files: [] };
    for (const p of R.files) { const parts = p.split('/'); let node = root; for (const d of parts.slice(0, -1)) node = node.dirs[d] || (node.dirs[d] = { dirs: {}, files: [] }); node.files.push(p); }
    const rows = new Map(), folders = [];
    const stats = node => { let n = 0, r = 0, open = false; for (const f of node.files) { n++; if (read.has(f) || edited.has(f)) r++; if (keys[f] || edited.has(f)) open = true; } for (const d of Object.values(node.dirs)) { const s = stats(d); n += s.n; r += s.r; open = open || s.open; } return { n, r, open }; };
    const sumText = (st, kfolder, r) => kfolder ? `${r ? r + ' of ' : ''}${st.n} files${r ? ' read' : ', never opened'}` : `${st.n} files${r ? `, ${r} read` : ''}`;
    const build = (node, prefix, depth) => {
      const frag = document.createDocumentFragment();
      for (const d of Object.keys(node.dirs).sort()) {
        const sub = node.dirs[d], st = stats(sub), path = prefix + d + '/', kfolder = keys[path];
        const isOpen = st.open || !!kfolder || depth === 0 && Object.keys(root.dirs).length <= 2;
        const sum = el('span', { class: 'sum', text: sumText(st, kfolder, st.r) });
        const row = el('div', { class: 'd' + (isOpen ? ' open' : ''), style: `--depth:${depth}`, role: 'button', tabindex: '0', 'aria-expanded': String(isOpen) }, el('span', { class: 'chev', 'aria-hidden': 'true' }), d + '/', sum);
        folders.push({ path, row, sum, st, kfolder });
        const kids = el('div', { class: 'kids' }, build(sub, path, depth + 1));
        kids.hidden = !isOpen;
        const toggle = () => { const o = kids.hidden; kids.hidden = !o; row.classList.toggle('open', o); row.setAttribute('aria-expanded', String(o)); };
        row.addEventListener('click', toggle); row.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } });
        frag.append(row, kids);
      }
      for (const f of node.files.sort()) {
        const k0 = keys[f], isEdit = edited.has(f), isRead = read.has(f);
        const k = k0 && k0.status !== 'read' && k0.status !== 'not opened' ? k0 : null;
        const left = k && k.status === 'left as is';
        const status = isEdit ? 'edited' : left ? 'left as is' : isRead ? 'read' : k ? 'never opened' : '';
        const row = el('div', { class: 'f' + (isEdit ? ' edited' : isRead ? ' read' : '') + (k ? ' key ' + (status === 'never opened' || left ? 'missed' : 'hit') : ''), style: `--depth:${depth}`, title: f },
          el('span', { class: 'fi', 'aria-hidden': 'true' }), el('span', { class: 'fn', text: f.split('/').pop() }), el('span', { class: 'st', text: status }));
        rows.set(f, row); frag.append(row);
      }
      return frag;
    };
    const tree = el('div', { class: 'ws-tree', role: 'tree', 'aria-label': 'Workspace files' });
    tree.append(build(root, '', 0));
    return tree;
  }
  function workspace(run, m) {
    const R = run.replay, S = R.summary || {}, seat = CI.low(R.model_role || 'model');
    const ws = el('div', { class: 'ws' });
    ws.append(el('div', { class: 'ws-role' }, CI.robot(m, { small: true }), el('span', null, el('strong', { text: m.name }), ` as the ${seat}`)));
    if (R.take) ws.append(el('div', { class: 'ws-role pred' }, CI.robot(CI.byName(R.take.pred), { small: true }), el('span', null, `took over from ${R.take.pred} in round ${R.take.round}`)));
    if (R.sandbox) {
      if (S.key_files && S.key_files.length) ws.append(el('div', { class: 'ws-label', text: 'The files that matter' }), el('div', { class: 'ws-keys' }, ...S.key_files.map(k => {
        const cls = k.status === 'edited' ? 'edited' : k.status === 'opened' ? 'hit' : k.status === 'read' || k.status === 'not opened' ? 'seen' : 'missed';
        return el('div', { class: 'k ' + cls }, el('span', { class: 'kd' }), el('code', { text: k.path }), el('span', { class: 'lab', text: k.label }), el('span', { class: 'verb', text: k.status }));
      })));
      ws.append(el('details', { class: 'ws-all' }, el('summary', { text: `All ${R.n_files} files` }), fileTree(R)));
      ws.append(el('div', { class: 'ws-sum', text: `${S.reads || 0} file${S.reads === 1 ? '' : 's'} read · ${(S.edits || []).length} edited${S.test_runs ? ` · ${S.test_runs} test run${S.test_runs > 1 ? 's' : ''}` : ''} · ${R.rounds} rounds` }));
    } else {
      ws.append(el('div', { class: 'ws-label', text: 'In the room' }), el('div', { class: 'ws-people' },
        el('div', { class: 'p' }, CI.robot(m), el('span', { text: m.short })),
        ...(R.colleagues || []).map(c => el('div', { class: 'p' }, CI.person({ title: c.role, cls: c.kind === 'partner' ? 'partner' : '' }), el('span', { text: c.role })))));
      ws.append(el('div', { class: 'ws-sum', text: `${R.rounds} rounds · ${R.n_messages} messages` }));
    }
    return ws;
  }
  function msgNode(t, m) {
    if (t.kind === 'note') return el('div', { class: 'msg note' }, el('div', { class: 'bubble note', text: t.text }));
    if (t.kind === 'round') return el('div', { class: 'msg round' }, el('span', { class: 'round-chip', text: `Round ${t.round}` }));
    if (t.kind === 'switch') return el('div', { class: 'msg switchline' }, el('span', { class: 'ln' }), el('span', { text: `Round ${t.round}: ${m.name} takes over from ${t.pred}` }), el('span', { class: 'ln' }));
    const isModel = t.kind.startsWith('model');
    const who = isModel ? (t.who ? `${m.name}, ${t.who}` : t.kind === 'model-thought' ? `${m.name}, to itself` : m.name) : t.kind === 'pred' ? `${t.who}, before the switch` : t.who;
    const av = isModel ? CI.robot(m) : t.kind === 'pred' ? CI.robot(CI.byName(t.who)) : CI.person({ title: t.who, cls: t.kind === 'partner' ? 'partner' : '' });
    return el('div', { class: 'msg ' + t.kind }, av, el('div', { class: 'bubble ' + t.kind }, el('span', { class: 'who', text: who + (t.round ? ` · round ${t.round}` : '') }), t.text));
  }
  CI.stamp = r => r.label || (r.outcome === 'pass' ? 'Pass' : r.outcome === 'fail' ? 'Fail' : 'Probe');
  CI.openRun = (ex, i) => {
    const dlg = $('#run-dialog'), r = ex.runs[i], m = CI.M[r.model], R = r.replay;
    const chat = el('div', { class: 'chat' });
    if (R && R.events) {
      let round = 0;
      for (const e of R.events) {
        if (e.t === 'msg') {
          if (e.round !== round) { if (R.take && round < R.take.round && e.round >= R.take.round) chat.append(msgNode({ kind: 'switch', round: R.take.round, pred: R.take.pred }, m)); round = e.round; chat.append(msgNode({ kind: 'round', round }, m)); }
          chat.append(msgNode({ kind: e.kind, who: e.who, text: e.text }, m));
        } else if (e.t === 'env') chat.append(msgNode({ kind: 'note', text: e.text.startsWith('[') ? e.text : `[${e.who}] ${e.text}` }, m));
      }
    } else r.turns.forEach(t => chat.append(msgNode(t, m)));
    chat.append(el('div', { class: 'chat-verdict ' + r.outcome }, el('span', { class: 'stamp ' + r.outcome, text: CI.stamp(r) }), el('div', null, r.verdict)));
    rc($('#rd-title'), CI.robot(m, { small: true }), el('strong', { text: m.name }), el('span', { class: 'stamp ' + r.outcome, text: CI.stamp(r) }), el('span', { class: 'ttl', text: ex.title }));
    rc($('.rd-body', dlg), el('div', { class: 'run-body' + (R ? '' : ' chat-only') }, R ? workspace(r, m) : null, chat),
      el('p', { class: 'rd-note', text: 'The full run as it happened: every message, word for word. Square brackets replace names.' }));
    dlg.showModal();
    $('.rd-body', dlg).scrollTop = 0;
  };
  function setupDialog() {
    const dlg = $('#run-dialog'); if (!dlg) return;
    $('.rd-close', dlg).addEventListener('click', () => dlg.close());
    dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });
    dlg.addEventListener('close', () => rc($('.rd-body', dlg)));
  }

  // ---------------------------------------------------------------- figure numbers, contents, header
  function numberFigures() {
    if (document.body.classList.contains('landing')) return;  // the landing page has one figure, unnumbered
    const n = {}, labels = {};
    for (const f of $$('figure.fig')) {
      const cap = $('figcaption', f); if (!cap) continue;
      const kind = f.dataset.kind || 'Figure';
      n[kind] = (n[kind] || 0) + 1;
      const lab = `${kind} ${n[kind]}`;
      cap.prepend(el('span', { class: 'lab', text: lab + ': ' }));
      if (f.id) labels[f.id] = lab;
    }
    for (const a of $$('a.xref')) a.textContent = labels[a.getAttribute('href').slice(1)] || '';
  }
  function setupToc() {
    // the contents list sits in the left margin on wide screens (blog post only)
    const secs = $$('.post > section[id]'), side = $('#toc-side');
    if (side && secs.length) rc(side, ...secs.map(x => el('li', null, el('a', { href: '#' + x.id, text: x.dataset.toc || $('h2', x).textContent }))));
    const onScroll = () => {
      $('.site-header').classList.toggle('scrolled', window.scrollY > 8);
      if (!side || !secs.length) return;
      let c = null;
      for (const x of secs) if (x.getBoundingClientRect().top < 160) c = x;
      if (c && window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) c = secs[secs.length - 1];
      $$('a', side).forEach(a => a.classList.toggle('on', !!c && a.getAttribute('href') === '#' + c.id));
      $('.toc-side').classList.toggle('shown', secs[0].getBoundingClientRect().top < window.innerHeight * 0.6);
    };
    window.addEventListener('scroll', onScroll, { passive: true }); onScroll();
  }

  function setupLinks() {
    $$('[data-placeholder]').forEach(a => { a.title = 'Coming soon'; a.setAttribute('aria-disabled', 'true'); a.addEventListener('click', e => e.preventDefault()); });
  }
  function footer() {
    const d = new Date(CI.D.leaderboard.generated + 'T00:00:00Z');
    const nice = d.toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
    CI.rc($('#post-date'), nice);
  }

  // ---------------------------------------------------------------- boot
  async function boot() {
    // a page names the data it needs (the landing page skips the large examples file)
    const names = (document.body.dataset.data || 'leaderboard,dimensions,examples,findings,dataset').split(',');
    const got = await Promise.all(names.map(n => fetch(`data/${n}.json${VERSION ? '?v=' + VERSION : ''}`).then(r => { if (!r.ok) throw new Error(n); return r.json(); })));
    names.forEach((n, i) => { CI.D[n] = got[i]; });
    for (const d of [...CI.D.dimensions.dimensions, ...CI.D.leaderboard.dimensions]) if (RENAME[d.key]) Object.assign(d, RENAME[d.key]);
    for (const ex of (CI.D.examples ? CI.D.examples.examples : [])) if (RENAME[ex.dimension]) ex.dimension_name = RENAME[ex.dimension].name;
    for (const m of CI.D.leaderboard.models) CI.M[m.id] = m;
    CI.F = Object.fromEntries(CI.D.findings.findings.map(f => [f.id, f.data]));
    // each part renders on its own: a problem in one never blanks the rest of the page
    for (const f of [computeValues, fillValues, setupDialog, ...CI.parts, numberFigures, setupToc, setupLinks, footer]) {
      try { f(); } catch (err) { console.error(`${f.name || 'part'}:`, err); }
    }
    document.documentElement.classList.add('ready');
  }
  document.addEventListener('DOMContentLoaded', () => boot().catch(err => {
    console.error(err);
    rc($('#chart-hero'), el('p', { class: 'load-error', text: 'The data files did not load. Run site/build_data.py, then serve the site folder over HTTP (for example: python -m http.server).' }));
  }));
})();
