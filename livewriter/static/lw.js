'use strict';
/* Live Writer page engine.
   The typewriter/doc mechanics are ported from mockups/live-writer-demo.html
   (the agreed UX spec); the scripted scenario machinery is replaced by a
   websocket to livewriter/server.py: server-validated edit ops in, smooth
   character-by-character animation out. */

/* ============================ 0 · utils ============================ */
const $ = s => document.querySelector(s);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const QP = new URLSearchParams(location.search);
const TEST = QP.has('test');
const tlog = (...a) => { if (!TEST) return; $('#testlog').textContent += a.join(' ') + '\n'; };
const now = () => performance.now();

/* ============================ 1 · params ============================ */
const P = Object.assign({
  baseCps: 45, catchup: 1.8, maxCps: 350, delMult: 3, punctPause: 40,
  editStyle: 'backspace', ghost: true, freshOn: true, freshMs: 1400,
  shimmer: true, cursorBlink: true, ticks: true, vol: 0.5,
}, JSON.parse(localStorage.getItem('lwP') || '{}'));
const saveP = () => localStorage.setItem('lwP', JSON.stringify(P));

/* ============================ 2 · sounds ============================ */
const Snd = {
  ctx: null, last: 0,
  init() { if (this.ctx) return; try { this.ctx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) {} },
  _blip(freq, dur, gain, type = 'sine') {
    if (!this.ctx || P.vol <= 0) return;
    const t = this.ctx.currentTime, o = this.ctx.createOscillator(), g = this.ctx.createGain();
    o.type = type; o.frequency.value = freq; g.gain.setValueAtTime(gain * P.vol, t);
    g.gain.exponentialRampToValueAtTime(1e-4, t + dur);
    o.connect(g); g.connect(this.ctx.destination); o.start(t); o.stop(t + dur);
  },
  tick(del) {
    if (!P.ticks || !this.ctx) return;
    const n = now(); if (n - this.last < 42) return; this.last = n;
    this._blip(del ? 700 + Math.random() * 150 : 1500 + Math.random() * 700, .018, .05, 'triangle');
  },
  thud() { this._blip(120, .14, .3); },
  chime() { this._blip(660, .18, .1); setTimeout(() => this._blip(880, .28, .1), 140); },
};

/* ==================== 3 · inline markdown -> runs ==================== */
/* Mirrors livewriter/doc.py parse_md exactly: **b**, *i*, `c`, ~~x~~;
   unbalanced markers are literal. Returns [{t, s}] runs. */
function mdToRuns(md) {
  const parts = String(md).split(/(\*\*|\*|`|~~)/);
  const MARK = { '**': 'b', '*': 'i', '`': 'c', '~~': 'x' };
  const counts = {};
  for (const p of parts) if (MARK[p]) counts[p] = (counts[p] || 0) + 1;
  const flags = new Set();
  const runs = [];
  const push = (text) => {
    if (!text) return;
    const s = [...flags].sort().join('');
    const last = runs[runs.length - 1];
    if (last && last.s === s) last.t += text; else runs.push({ t: text, s });
  };
  for (const p of parts) {
    if (MARK[p]) {
      const f = MARK[p];
      if (flags.has(f)) { flags.delete(f); counts[p] -= 2; continue; }
      if ((counts[p] || 0) >= 2) { flags.add(f); continue; }
      counts[p] -= 1; push(p); continue;
    }
    push(p);
  }
  return runs;
}
function runsToMd(runs) {
  const order = ['b', 'i', 'c', 'x'], marker = { b: '**', i: '*', c: '`', x: '~~' };
  let out = '', prev = '';
  for (const r of [...runs, { t: '', s: '' }]) {
    const fl = r.s || '';
    if (fl !== prev) {
      for (const f of [...order].reverse()) if (prev.includes(f) && !fl.includes(f)) out += marker[f];
      for (const f of order) if (fl.includes(f) && !prev.includes(f)) out += marker[f];
      prev = fl;
    }
    out += r.t;
  }
  return out;
}

/* ==================== 4 · document model (mockup port) ==================== */
const atoms = line => { const a = []; for (const r of line.runs) for (const ch of r.t) a.push({ ch, s: r.s }); return a; };
const setAtoms = (line, a) => { const runs = []; for (const x of a) { const l = runs[runs.length - 1]; if (l && l.s === x.s) l.t += x.ch; else runs.push({ t: x.ch, s: x.s }); } line.runs = runs; };
const lineLen = line => atoms(line).length;
const lineText = line => line.runs.map(r => r.t).join('');
const addFlag = (s, f) => s.includes(f) ? s : (s + f).split('').sort().join('');

function makeCtx() { return { lines: [], cursor: { line: null, off: 0 } }; }
function resolveLine(ctx, id, type) {
  let l = ctx.lines.find(x => x.id === id);
  if (!l) { l = { id, type: type || 'p', runs: [] }; ctx.lines.push(l); }
  return l;
}
let fresh = [];
function shiftFresh(lineId, pos, delta) {
  for (const r of fresh) {
    if (r.line !== lineId) continue;
    if (pos <= r.from) r.from = Math.max(0, r.from + delta);
    else if (pos < r.from + r.len) r.len = Math.max(0, r.len + delta);
  }
}
function freshAdd(lineId, off, t) {
  const last = fresh[fresh.length - 1];
  if (last && last.line === lineId && off === last.from + last.len && t - last.born < 400) { last.len++; last.born = t; return; }
  fresh.push({ line: lineId, from: off, len: 1, born: t });
}

function applyStep(ctx, st, liveCtx) {
  switch (st.op) {
    case 'move': {
      const l = resolveLine(ctx, st.line);
      ctx.cursor = { line: l.id, off: st.off === 'end' ? lineLen(l) : clamp(st.off, 0, lineLen(l)) };
      break; }
    case 'nl': {
      let l = ctx.lines.find(x => x.id === st.id);
      if (!l) {
        l = { id: st.id, type: st.ltype, runs: [] };
        let idx = ctx.lines.length;
        if (st.after !== undefined && st.after !== null) {
          const i = ctx.lines.findIndex(x => x.id === st.after);
          if (i >= 0) idx = i + 1;
        }
        ctx.lines.splice(idx, 0, l);
      }
      ctx.cursor = { line: l.id, off: lineLen(l) };
      break; }
    case 'insCh': {
      const l = resolveLine(ctx, ctx.cursor.line !== null ? ctx.cursor.line : st.line);
      if (ctx.cursor.line === null) ctx.cursor = { line: l.id, off: lineLen(l) };
      const a = atoms(l); a.splice(ctx.cursor.off, 0, { ch: st.ch, s: st.s }); setAtoms(l, a);
      if (liveCtx) { shiftFresh(l.id, ctx.cursor.off, 1); if (P.freshOn) freshAdd(l.id, ctx.cursor.off, now()); }
      ctx.cursor.off++;
      break; }
    case 'del': {
      const l = resolveLine(ctx, ctx.cursor.line); if (ctx.cursor.off <= 0) break;
      const a = atoms(l); a.splice(ctx.cursor.off - 1, 1); setAtoms(l, a);
      if (liveCtx) shiftFresh(l.id, ctx.cursor.off - 1, -1);
      ctx.cursor.off--;
      break; }
    case 'delRange': {
      const l = resolveLine(ctx, st.line); const a = atoms(l);
      a.splice(st.from, st.len); setAtoms(l, a);
      if (liveCtx) shiftFresh(l.id, st.from, -st.len);
      if (ctx.cursor.line === l.id) {
        if (ctx.cursor.off >= st.from + st.len) ctx.cursor.off -= st.len;
        else if (ctx.cursor.off > st.from) ctx.cursor.off = st.from;
      }
      break; }
    case 'strike': {
      const l = resolveLine(ctx, st.line); const a = atoms(l);
      for (let i = st.from; i < Math.min(a.length, st.from + st.len); i++) a[i].s = addFlag(a[i].s, 'x');
      setAtoms(l, a);
      break; }
    case 'delLine': {
      const i = ctx.lines.findIndex(x => x.id === st.line);
      if (i < 0) break;
      ctx.lines.splice(i, 1);
      if (ctx.cursor.line === st.line) {
        const prev = ctx.lines[Math.min(i, ctx.lines.length - 1)];
        ctx.cursor = prev ? { line: prev.id, off: lineLen(prev) } : { line: null, off: 0 };
      }
      break; }
    case 'insText': {
      const l = resolveLine(ctx, st.line); const a = atoms(l);
      const add = []; for (const r of st.runs) for (const ch of r.t) add.push({ ch, s: r.s });
      a.splice(st.at, 0, ...add); setAtoms(l, a);
      if (liveCtx) { shiftFresh(l.id, st.at, add.length); if (P.freshOn) fresh.push({ line: l.id, from: st.at, len: add.length, born: now() }); }
      if (ctx.cursor.line === l.id && ctx.cursor.off >= st.at) ctx.cursor.off += add.length;
      break; }
  }
}

/* ==================== 5 · typewriter engine (mockup port) ==================== */
let live = makeCtx();
let plan = makeCtx();
let dirty = true;
let pendingThinks = 0;

const TW = {
  q: [], qCost: 0, budget: 0, dwellUntil: 0, typedChars: 0, delChars: 0, cpsEma: 0,
  push(steps) { for (const s of steps) { this.q.push(s); this.qCost += s._cost || 0; } },
  clear() { this.q = []; this.qCost = 0; this.budget = 0; this.dwellUntil = 0; },
  backlog() { return Math.max(0, this.qCost); },
  step(dtMs) {
    if (dtMs <= 0) return;
    const t = now();
    if (t < this.dwellUntil) return;
    if (!this.q.length) { this.budget = 0; return; }
    const speed = clamp(this.backlog() / (P.catchup || 1), P.baseCps, P.maxCps);
    this.budget = Math.min(this.budget + speed * dtMs / 1000, Math.max(30, speed / 6));
    while (this.q.length) {
      if (now() < this.dwellUntil) break;
      const st = this.q[0];
      if (st.op === 'insCh_seq') {
        if (this.budget < 1) break;
        const ch = st.chars[st.i];
        applyStep(live, { op: 'insCh', ch, s: st.s, line: st.line }, true);
        st.i++; this.budget -= 1; this.qCost -= 1; this.typedChars++;
        dirty = true; Snd.tick(false);
        if ('.,;:!?—'.includes(ch) && P.punctPause > 0) this.dwellUntil = now() + P.punctPause * (ch === '.' ? 1.6 : 1);
        if (st.i >= st.chars.length) this.q.shift();
        continue;
      }
      if (st.op === 'del_seq') {
        const cost = 1 / P.delMult;
        if (this.budget < cost) break;
        applyStep(live, { op: 'del' }, true);
        st.n--; this.budget -= cost; this.qCost -= cost; this.delChars++;
        dirty = true; Snd.tick(true);
        if (st.n <= 0) this.q.shift();
        continue;
      }
      if (st.op === 'delAllLine') {
        const l = live.lines.find(x => x.id === st.line);
        if (!l || lineLen(l) === 0) { this.qCost -= st._cost || 0; st._cost = 0; this.q.shift(); continue; }
        const cost = 1 / P.delMult;
        if (this.budget < cost) break;
        live.cursor = { line: l.id, off: lineLen(l) };
        applyStep(live, { op: 'del' }, true);
        this.budget -= cost; st._cost = Math.max(0, (st._cost || 0) - cost); this.qCost -= cost;
        this.delChars++; dirty = true; Snd.tick(true);
        continue;
      }
      this.q.shift();
      switch (st.op) {
        case 'move': case 'nl': case 'delRange': case 'strike': case 'insText': case 'delLine':
          applyStep(live, st, true); dirty = true; break;
        case 'dwell': this.dwellUntil = now() + st.ms; break;
        case 'chip': chip(st.text); break;
        case 'consume': ghostConsume(st.through); break;
      }
    }
  }
};

/* ==================== 6 · server ops -> steps ==================== */
function stepsForRuns(runs, into) {
  const out = [];
  if (into !== undefined && into !== null) out.push(mk({ op: 'move', line: into, off: 'end' }));
  for (const r of runs) {
    const chars = [...r.t];
    if (!chars.length) continue;
    out.push(mk({ op: 'insCh_seq', chars, i: 0, s: r.s || '', _cost: chars.length }));
  }
  return out;
}
function mk(st) {
  if (st.op === 'insCh_seq') { for (const ch of st.chars) applyStep(plan, { op: 'insCh', ch, s: st.s }, false); return st; }
  if (st.op === 'del_seq') { for (let i = 0; i < st.n; i++) applyStep(plan, { op: 'del' }, false); return st; }
  if (['move', 'nl', 'delRange', 'strike', 'insText', 'delLine'].includes(st.op)) applyStep(plan, st, false);
  return st;
}
function repSteps(lineId, find, runs) {
  const out = [];
  const l = resolveLine(plan, lineId);
  const text = atoms(l).map(a => a.ch).join('');
  const fi = text.lastIndexOf(find);
  if (fi < 0) { tlog('rep-miss', find); return stepsForRuns(runs, lineId); }
  const flen = [...find].length;
  /* find index is in code units; convert to atom (code point) index */
  const fiAtoms = [...text.slice(0, fi)].length;
  const newLen = runs.reduce((n, r) => n + [...r.t].length, 0);
  const saved = { ...plan.cursor };
  if (P.editStyle === 'strike') {
    out.push(mk({ op: 'strike', line: lineId, from: fiAtoms, len: flen }));
    out.push(mk({ op: 'move', line: lineId, off: fiAtoms + flen }));
    out.push(...stepsForRuns(runs));
    out.push({ op: 'dwell', ms: 560 });
    out.push(mk({ op: 'delRange', line: lineId, from: fiAtoms, len: flen }));
  } else if (P.editStyle === 'instant') {
    out.push(mk({ op: 'delRange', line: lineId, from: fiAtoms, len: flen }));
    out.push(mk({ op: 'insText', line: lineId, at: fiAtoms, runs }));
  } else {
    out.push(mk({ op: 'move', line: lineId, off: fiAtoms + flen }));
    out.push(mk({ op: 'del_seq', n: flen, _cost: flen / P.delMult }));
    out.push(...stepsForRuns(runs));
  }
  if (saved.line === lineId && saved.off >= fiAtoms + flen) {
    out.push(mk({ op: 'move', line: lineId, off: saved.off + newLen - flen }));
  } else if (saved.line !== null && saved.line !== undefined) {
    out.push(mk({ op: 'move', line: saved.line, off: saved.off }));
  }
  return out;
}

let epoch = 0;
const lineKey = id => epoch + ':' + id;
function compileOp(o) {
  const op = o.op;
  if (op === 'chip') return [{ op: 'chip', text: o.text }];
  if (op === 'new') {
    const key = lineKey(o.id);
    const after = (o.after !== undefined && o.after !== null) ? lineKey(o.after) : undefined;
    return [mk({ op: 'nl', id: key, ltype: o.kind, after }), ...stepsForRuns(mdToRuns(o.md))];
  }
  if (op === 'append') return stepsForRuns(mdToRuns(o.md), lineKey(o.line));
  if (op === 'replace') {
    const steps = repSteps(lineKey(o.line), o.find, mdToRuns(o.md));
    if (o.empty_delete) steps.push(mk({ op: 'delLine', line: lineKey(o.line) }));
    return steps;
  }
  if (op === 'delete') {
    const key = lineKey(o.line);
    const l = plan.lines.find(x => x.id === key);
    const cost = l ? lineLen(l) / P.delMult : 0;
    const steps = [{ op: 'delAllLine', line: key, _cost: cost }, mk({ op: 'delLine', line: key })];
    const pl = plan.lines.find(x => x.id === key);
    if (pl) { pl.runs = []; }
    return steps;
  }
  return [];
}

/* ==================== 7 · ghost & metrics ==================== */
let ghostUtts = [];        // [{id, text, heardT}]
let pendingGhost = null;   // {text, heardT}
const metrics = { samples: [], stops: [] };
function ghostConsume(through) {
  const t = now();
  for (const g of ghostUtts) {
    if (g.id <= through) {
      const ms = Math.round(t - g.heardT);
      metrics.samples.push({ utt: g.id, ms });
      wsSend({ type: 'metric', name: 'heard_to_written_ms', utt: g.id, ms });
      tlog('written', g.id, ms + 'ms');
    }
  }
  ghostUtts = ghostUtts.filter(g => g.id > through);
  dirty = true;
}

/* ==================== 8 · render ==================== */
const STYLE_CLS = { b: 'b', i: 'i', c: 'c', x: 'x' };
function runCls(s) { return [...(s || '')].map(f => STYLE_CLS[f] || '').join(' ').trim(); }
function cursorHtml() {
  const idle = TW.q.length === 0 && pendingThinks === 0;
  const think = pendingThinks > 0 && TW.q.length === 0;
  const stopped = now() - stopFlashT < 900;
  const cls = 'cur' + (idle && !stopped ? ' idle' : '') + (think && P.shimmer ? ' think' : '') + (stopped ? ' stopped' : '');
  const pill = (think && P.shimmer) ? '<span class="thinkpill">thinking…</span>' : '';
  return `<span class="${cls}"></span>` + pill;
}
function renderDoc() {
  const el = $('#doc');
  const cur = live.cursor;
  fresh = fresh.filter(r => now() - r.born < P.freshMs && r.len > 0);
  let html = ''; let inUl = false;
  const anyGhost = (ghostUtts.length || (pendingGhost && pendingGhost.text));
  if (!live.lines.length && !anyGhost && sessionStarted) html += '<p id="emptyhint">listening…</p>';
  for (const line of live.lines) {
    if (line.type === 'li' && !inUl) { html += '<ul>'; inUl = true; }
    if (line.type !== 'li' && inUl) { html += '</ul>'; inUl = false; }
    const a = atoms(line);
    const fr = P.freshOn ? fresh.filter(r => r.line === line.id) : [];
    let inner = ''; let span = []; let lastKey = null;
    const flush = () => {
      if (!span.length) return;
      inner += lastKey ? `<span class="${lastKey.cls}"${lastKey.st ? ` style="${lastKey.st}"` : ''}>${esc(span.join(''))}</span>` : esc(span.join(''));
      span = [];
    };
    for (let i = 0; i <= a.length; i++) {
      if (cur.line === line.id && cur.off === i) { flush(); lastKey = null; inner += cursorHtml(); }
      if (i === a.length) break;
      const at = a[i];
      let alpha = 0;
      for (const r of fr) if (i >= r.from && i < r.from + r.len) alpha = Math.max(alpha, .34 * (1 - (now() - r.born) / P.freshMs));
      const cls = runCls(at.s);
      const st = alpha > 0.01 ? `background:rgba(57,135,229,${alpha.toFixed(3)})` : '';
      const key = (cls || st) ? { cls, st } : null;
      if ((key ? key.cls + '|' + key.st : '') !== (lastKey ? lastKey.cls + '|' + lastKey.st : '')) { flush(); lastKey = key; }
      span.push(at.ch);
    }
    flush();
    const tag = line.type === 'h1' ? 'h1' : line.type === 'h2' ? 'h2' : line.type === 'h3' ? 'h3'
      : line.type === 'li' ? 'li' : line.type === 'quote' ? 'blockquote' : line.type === 'code' ? 'pre' : 'p';
    const cls2 = line.type === 'small' ? ' class="sm"' : '';
    html += `<${tag}${cls2}>${inner || (cur.line === line.id ? '' : '&#8203;')}</${tag}>`;
  }
  if (inUl) html += '</ul>';
  if (P.ghost && anyGhost) {
    const parts = [];
    for (const g of ghostUtts) parts.push(`<span>${esc(g.text)}</span>`);
    if (pendingGhost && pendingGhost.text) parts.push(`<span class="gu">${esc(pendingGhost.text)}</span>`);
    html += `<p class="ghostline"><span class="mic">🎙</span>${parts.join(' ')}</p>`;
  }
  el.innerHTML = html;
  const c = $('#docscroll');
  const ce = el.querySelector('.cur');
  const anchor = ce || el.lastElementChild;
  if (anchor) {
    const bottom = anchor.offsetTop + 40;
    if (bottom > c.scrollTop + c.clientHeight - 60) c.scrollTop = bottom - c.clientHeight + 60;
  }
}
function chip(text) {
  const c = document.createElement('div'); c.className = 'chip'; c.textContent = text;
  const box = $('#chips'); box.appendChild(c);
  while (box.children.length > 4) box.removeChild(box.firstChild);
  setTimeout(() => { c.classList.add('fade'); setTimeout(() => c.remove(), 700); }, 4600);
  tlog('chip', text);
}

/* HUD */
const sparkSamples = []; let lastSample = 0, lastHud = 0;
function oldestHeard() {
  let t = null;
  if (ghostUtts.length) t = ghostUtts[0].heardT;
  if (pendingGhost && pendingGhost.text && (t === null || pendingGhost.heardT < t)) t = pendingGhost.heardT;
  return t;
}
function hud() {
  const t = now();
  if (t - lastSample >= 120) {
    lastSample = t;
    const oh = oldestHeard();
    sparkSamples.push(oh !== null ? t - oh : (TW.backlog() > 0 ? 400 : 0));
    if (sparkSamples.length > 100) sparkSamples.shift();
  }
  if (t - lastHud < 120) return; lastHud = t;
  const oh = oldestHeard();
  const lag = oh !== null ? t - oh : 0;
  $('#hud-lag').textContent = oh === null ? '—' : (lag < 1000 ? Math.round(lag) + ' ms' : (lag / 1000).toFixed(1) + ' s');
  $('#hud-backlog').textContent = Math.round(TW.backlog()) + ' ch';
  const target = TW.q.length ? clamp(TW.backlog() / (P.catchup || 1), P.baseCps, P.maxCps) : 0;
  TW.cpsEma = TW.cpsEma * 0.6 + target * 0.4;
  $('#hud-cps').textContent = Math.round(TW.cpsEma) + ' cps';
  const cv = $('#spark'), ctx = cv.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  if (cv.width !== 150 * dpr) { cv.width = 150 * dpr; cv.height = 34 * dpr; cv.style.width = '150px'; cv.style.height = '34px'; }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, 150, 34);
  if (sparkSamples.length > 1) {
    const max = Math.max(1200, ...sparkSamples);
    ctx.beginPath(); ctx.lineWidth = 2; ctx.strokeStyle = '#3987e5'; ctx.lineJoin = 'round';
    sparkSamples.forEach((v, i) => {
      const x = 150 * i / (sparkSamples.length - 1), y = 31 - 28 * (v / max);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.stroke();
    const lv = sparkSamples[sparkSamples.length - 1];
    ctx.fillStyle = '#3987e5'; ctx.beginPath(); ctx.arc(148, 31 - 28 * (lv / max), 2.5, 0, 7); ctx.fill();
  }
}

/* ==================== 9 · stop ==================== */
let stopFlashT = -1e9;
let curGen = 0;
let sessionStarted = false;
function agentStop(local) {
  TW.clear();
  ghostUtts = []; pendingGhost = null;
  pendingThinks = 0;
  plan = JSON.parse(JSON.stringify(live));
  Snd.thud();
  stopFlashT = now();
  const dp = $('#docpanel'); dp.classList.remove('stopflash'); void dp.offsetWidth; dp.classList.add('stopflash');
  dirty = true;
  if (local) wsSend({ type: 'halt' });
  metrics.stops.push({ t: Date.now() });
}

/* ==================== 10 · websocket ==================== */
let ws = null, wsReady = false, wsTries = 0;
function wsSend(obj) { if (wsReady) try { ws.send(JSON.stringify(obj)); } catch (e) {} }
function wsSendBinary(buf) { if (wsReady) try { ws.send(buf); } catch (e) {} }
function connect() {
  status('connecting…', '');
  ws = new WebSocket('ws://' + location.host + '/ws');
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    wsReady = true; wsTries = 0;
    if (epoch > 0) { chip('reconnected — new server session'); }
    ws.send(JSON.stringify({ type: 'hello' }));
  };
  ws.onclose = () => {
    wsReady = false;
    epoch++;  // a new server session has a fresh doc: never mix line ids
    status('connection lost — retrying…', 'warn');
    setTimeout(connect, Math.min(5000, 800 * ++wsTries));
  };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
  ws.onmessage = e => {
    let m; try { m = JSON.parse(e.data); } catch (err) { return; }
    handle(m);
  };
}
function status(text, kind) {
  $('#statustext').textContent = text;
  const d = $('#statusdot');
  d.className = '';
  if (kind) d.classList.add(kind);
}
function handle(m) {
  switch (m.type) {
    case 'ready':
      $('#modeltag').textContent = m.fake ? 'keyless fake mode' : `${m.asr} → ${m.fmt}`;
      status(Mic.on ? 'listening' : 'ready — press Start talking', Mic.on ? 'rec' : 'ok');
      tlog('ready', JSON.stringify(m));
      break;
    case 'ghost':
      if (m.text) {
        if (!pendingGhost || pendingGhost.text !== m.text)
          pendingGhost = { text: m.text, heardT: now() - (m.age_ms || 0) };
      } else pendingGhost = null;
      sessionStarted = true;
      dirty = true;
      break;
    case 'utt':
      pendingGhost = null;
      ghostUtts.push({ id: m.id, text: m.text, heardT: now() - (m.age_ms || 0) });
      sessionStarted = true;
      dirty = true;
      tlog('utt', m.id, m.text);
      break;
    case 'think':
      pendingThinks = m.on ? 1 : 0;
      dirty = true;
      break;
    case 'op': {
      if (m.gen < curGen) return;
      curGen = m.gen;
      const steps = compileOp(m.op);
      TW.push(steps);
      tlog('op', JSON.stringify(m.op));
      break; }
    case 'wrote':
      if (m.gen < curGen) return;
      TW.push([{ op: 'consume', through: m.through_utt }]);
      break;
    case 'halted':
      curGen = m.gen;
      agentStop(false);
      chip('⏹ stopped');
      break;
    case 'reset_ok':
      epoch++;
      live = makeCtx(); plan = makeCtx(); TW.clear();
      ghostUtts = []; pendingGhost = null; fresh = [];
      dirty = true;
      break;
    case 'status':
      tlog('status', m.text);
      if (/error|failed|lost/.test(m.text)) status(m.text, 'warn');
      break;
  }
}

/* ==================== 11 · mic ==================== */
const Mic = { on: false, ctx: null, node: null, stream: null };
async function micStart() {
  if (Mic.on) return micStop();
  Snd.init();
  try {
    Mic.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (e) {
    status('mic denied: ' + e.name, 'warn');
    return;
  }
  Mic.ctx = new (window.AudioContext || window.webkitAudioContext)();
  await Mic.ctx.resume();
  await Mic.ctx.audioWorklet.addModule('/worklet.js');
  const src = Mic.ctx.createMediaStreamSource(Mic.stream);
  Mic.node = new AudioWorkletNode(Mic.ctx, 'pcm16', { numberOfInputs: 1, numberOfOutputs: 0 });
  Mic.node.port.onmessage = e => wsSendBinary(e.data);
  src.connect(Mic.node);
  Mic.on = true;
  sessionStarted = true;
  $('#micbtn').textContent = '⏸ Pause mic';
  $('#micbtn').classList.remove('primary'); $('#micbtn').classList.add('rec');
  $('#intro').style.display = 'none';
  status('listening', 'rec');
  dirty = true;
}
function micStop() {
  Mic.on = false;
  try { Mic.node && Mic.node.disconnect(); } catch (e) {}
  try { Mic.stream && Mic.stream.getTracks().forEach(t => t.stop()); } catch (e) {}
  try { Mic.ctx && Mic.ctx.close(); } catch (e) {}
  Mic.node = Mic.ctx = Mic.stream = null;
  $('#micbtn').textContent = '🎤 Start talking';
  $('#micbtn').classList.add('primary'); $('#micbtn').classList.remove('rec');
  status('mic paused', 'ok');
}

/* ==================== 12 · markdown export ==================== */
function docMarkdown() {
  const blocks = [];
  for (const l of live.lines) {
    const md = runsToMd(l.runs);
    if (l.type === 'h1') blocks.push(['h', '# ' + md]);
    else if (l.type === 'h2') blocks.push(['h', '## ' + md]);
    else if (l.type === 'h3') blocks.push(['h', '### ' + md]);
    else if (l.type === 'li') {
      if (blocks.length && blocks[blocks.length - 1][0] === 'ul') blocks[blocks.length - 1][1] += '\n- ' + md;
      else blocks.push(['ul', '- ' + md]);
    } else if (l.type === 'quote') blocks.push(['q', '> ' + md]);
    else if (l.type === 'code') blocks.push(['c', '```\n' + lineText(l) + '\n```']);
    else blocks.push(['p', md]);
  }
  return blocks.map(b => b[1]).join('\n\n') + (blocks.length ? '\n' : '');
}

/* ==================== 13 · settings ==================== */
const SETTINGS = [
  { k: 'editStyle', l: 'Edit animation', opts: { backspace: 'Backspace & retype', strike: 'Strike, then collapse', instant: 'Instant swap' } },
  { k: 'baseCps', l: 'Base typing speed', min: 10, max: 120, step: 1 },
  { k: 'catchup', l: 'Catch-up window (s)', min: .4, max: 4, step: .1 },
  { k: 'maxCps', l: 'Max typing speed', min: 80, max: 800, step: 10 },
  { k: 'ghost', l: 'Ghost tail', b: 1 },
  { k: 'freshOn', l: 'Highlight fresh text', b: 1 },
  { k: 'ticks', l: 'Typing sounds', b: 1 },
  { k: 'cursorBlink', l: 'Cursor blink', b: 1 },
];
function buildSettings() {
  const box = $('#setpanel'); box.innerHTML = '';
  for (const c of SETTINGS) {
    const row = document.createElement('div'); row.className = 'crow';
    if (c.b) {
      row.innerHTML = `<label>${c.l}</label>`;
      const cb = document.createElement('input'); cb.type = 'checkbox'; cb.checked = !!P[c.k];
      cb.onchange = () => { P[c.k] = cb.checked; saveP(); applyLook(); };
      row.appendChild(cb);
    } else if (c.opts) {
      row.innerHTML = `<label>${c.l}</label>`;
      const sel = document.createElement('select');
      for (const [v, txt] of Object.entries(c.opts)) sel.innerHTML += `<option value="${v}"${P[c.k] === v ? ' selected' : ''}>${txt}</option>`;
      sel.onchange = () => { P[c.k] = sel.value; saveP(); };
      row.appendChild(sel);
    } else {
      row.innerHTML = `<label>${c.l}</label><span style="text-align:right;font-variant-numeric:tabular-nums">${P[c.k]}</span>`;
      const inp = document.createElement('input');
      inp.type = 'range'; inp.min = c.min; inp.max = c.max; inp.step = c.step; inp.value = P[c.k];
      inp.oninput = () => { P[c.k] = +inp.value; row.children[1].textContent = P[c.k]; saveP(); };
      row.appendChild(inp);
    }
    box.appendChild(row);
  }
}
function applyLook() {
  document.body.classList.toggle('blink', P.cursorBlink);
  dirty = true;
}

/* ==================== 14 · wiring ==================== */
$('#micbtn').onclick = micStart;
$('#introstart').onclick = micStart;
$('#introtype').onclick = () => { Snd.init(); $('#intro').style.display = 'none'; sessionStarted = true; $('#typebox').focus(); dirty = true; };
$('#stopbtn').onclick = () => { Snd.init(); agentStop(true); chip('⏹ stopped (button)'); };
$('#newbtn').onclick = () => { wsSend({ type: 'reset' }); };
$('#copybtn').onclick = () => {
  const reset = () => setTimeout(() => $('#copybtn').textContent = '📋 Copy markdown', 1400);
  if (!navigator.clipboard) { $('#copybtn').textContent = '✗ clipboard unavailable'; reset(); return; }
  navigator.clipboard.writeText(docMarkdown())
    .then(() => { $('#copybtn').textContent = '✓ copied'; reset(); })
    .catch(() => { $('#copybtn').textContent = '✗ copy failed'; reset(); });
};
$('#dlbtn').onclick = () => {
  const blob = new Blob([docMarkdown()], { type: 'text/markdown' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'live-writer-' + new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-') + '.md';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
};
$('#gearbtn').onclick = () => $('#setpanel').classList.toggle('open');
document.addEventListener('click', e => {
  if (!$('#settings').contains(e.target)) $('#setpanel').classList.remove('open');
});
$('#typeform').onsubmit = e => {
  e.preventDefault();
  const text = $('#typebox').value.trim();
  if (!text) return;
  Snd.init();
  wsSend({ type: 'text_input', text });
  $('#typebox').value = '';
  $('#intro').style.display = 'none';
  sessionStarted = true;
};
document.addEventListener('keydown', e => {
  if (/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) return;
  if (e.key === 'Escape') { agentStop(true); chip('⏹ stopped (esc)'); }
});

/* ==================== 15 · main loop ==================== */
let lastRaf = now();
function frame() {
  const t = now();
  const dt = Math.min(100, t - lastRaf);
  if (dt <= 0) return;
  lastRaf = t;
  TW.step(dt);
  if (fresh.length || (pendingGhost && pendingGhost.text)) dirty = true;
  if (dirty) { renderDoc(); dirty = false; }
  hud();
}
function rafLoop() { frame(); requestAnimationFrame(rafLoop); }
buildSettings(); applyLook();
connect();
requestAnimationFrame(rafLoop);
setInterval(frame, 33); /* survive rAF throttling (occluded/headless windows) */

/* test hooks */
window.__lw = {
  TW, metrics,
  get doc() { return live.lines.map(l => l.type + ': ' + lineText(l)).join('\n'); },
  get md() { return docMarkdown(); },
  get ghost() { return ghostUtts.map(g => g.text).join(' | ') + (pendingGhost ? ' ~ ' + pendingGhost.text : ''); },
  send: wsSend,
  micStart, micStop,
  get connected() { return wsReady; },
};
if (QP.has('autostart')) {
  const tryStart = () => { if (wsReady) micStart(); else setTimeout(tryStart, 200); };
  window.addEventListener('load', () => setTimeout(tryStart, 300));
}
if (QP.has('notype')) $('#bottombar').style.display = 'none';
