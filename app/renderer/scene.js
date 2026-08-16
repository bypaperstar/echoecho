// echoecho Orb scene — genie reveal/dismiss, items emerging out of the blob
// (doc cards, transcript wisps, echoecho's Mac), expansion to ~92% of the scene,
// and the live viewer wiring. Lifecycle contract with main.js:
//   orb:reveal -> ease reveal to 1; orb:dismiss -> ease to 0 then orb.hidden();
// The window is click-through except tightly over the blob and its items
// (hover-tracked via orb.setPassthrough); the blob itself is draggable.
// Demo mode (?demo=1, passed by main from ECHOECHO_ORB_DEMO=1) runs a scripted,
// deterministic timeline with no server: wisps, a fake doc, echoecho's Mac
// (asleep), expand/restore, absorb.
'use strict';

(() => {

const qs = new URLSearchParams(location.search);
const DEMO = qs.get('demo') === '1';
const blob = window.echoechoBlob;
const itemsEl = document.getElementById('items');
const canvas = document.getElementById('blob');

const clamp = (x, a, b) => x < a ? a : x > b ? b : x;
const lerp = (a, b, p) => a + (b - a) * p;
const sstep = (a, b, x) => { x = clamp((x - a) / (b - a), 0, 1); return x * x * (3 - 2 * x); };
const easeOutCubic = (t) => 1 - Math.pow(1 - t, 3);
const easeOutBack = (p) => { const k = 1.35; const q = p - 1; return 1 + q * q * ((k + 1) * q + k); };
const now = () => performance.now() / 1000;

// -------------------------------------------------------------- rest layout
// Where the user last dragged the blob, as window fractions (survives
// restarts and resizes); null means the default rest spot.
let userPose = null;
try {
  const saved = JSON.parse(localStorage.getItem('echoecho.pose'));
  if (saved && isFinite(saved.fx) && isFinite(saved.fy)) userPose = saved;
} catch { /* default spot */ }

function restPose(shrunk) {
  if (shrunk) {
    // companion: peeks from the free bottom-left corner beside the expanded
    // item (which anchors top-right), still breathing
    const free = Math.max(56, innerWidth * 0.08 - 24);
    return { x: free * 0.55, y: innerHeight - free * 0.68,
             r: clamp(free * 0.7, 36, 64) };
  }
  const m = Math.min(innerWidth, innerHeight);
  const r = m * 0.135;
  if (userPose) {
    return { x: clamp(userPose.fx * innerWidth, r * 0.5, innerWidth - r * 0.5),
             y: clamp(userPose.fy * innerHeight, r * 0.5, innerHeight - r * 0.5),
             r };
  }
  return { x: innerWidth * 0.38, y: innerHeight * 0.62, r };
}
let pose = restPose(false);
function applyRest(shrunk) {
  pose = restPose(shrunk);
  blob.setRest(pose.x, pose.y, pose.r);
}

// ----------------------------------------------------------- reveal driver
const REVEAL_DUR = 2.4, DISMISS_DUR = 1.4;
let rv = { value: 0, from: 0, target: 0, t0: -1e9, dur: 1 };
let anchor = { x: innerWidth - 40, y: 0 };
let hideWhenDone = false;
function revealTo(target, dur) {
  rv = { value: rv.value, from: rv.value, target, t0: now(),
         dur: Math.max(0.05, dur * Math.abs(target - rv.value)) };
}

// ------------------------------------------------------------------- items
// item: { el, kind, x, y, w, h, base:{x,y}, spawn:{x,y}, edge:{x,y},
//         mode: emerging|held|float|expanded|absorbing, handle, ph, origin }
const items = [];
const wisps = [];
let expanded = null;
let macItem = null;

// emit angles (rad) off the blob: steep enough that necks meet an item's
// flat bottom edge (a corner graze reads as "beside", not "held")
const SLOTS = [-0.92, 0.30, -0.45];
let slotIdx = 0;

function setTransform(it, sx, sy, alpha) {
  it.el.style.transform =
    `translate(${it.x}px, ${it.y}px) translate(-50%, -50%) scale(${Math.max(sx, 0.01)}, ${Math.max(sy, 0.01)})`;
  if (alpha !== undefined) it.el.style.opacity = alpha;
}

function spawnPointFor(w, h) {
  const ang = SLOTS[slotIdx++ % SLOTS.length];
  const d = pose.r * 1.15 + Math.hypot(w, h) / 2 + 26;
  return { x: clamp(pose.x + Math.cos(ang) * d, w / 2 + 12, innerWidth - w / 2 - 12),
           y: clamp(pose.y + Math.sin(ang) * d, h / 2 + 12, innerHeight - h / 2 - 12) };
}

function emergeItem(el, kind, w, h, opts = {}) {
  el.classList.add('item');
  el.style.width = w + 'px';
  el.style.height = h + 'px';
  itemsEl.appendChild(el);
  const spawn = opts.spawn || spawnPointFor(w, h);
  const edge = blob.edgePoint(spawn.x, spawn.y);
  const it = {
    el, kind, w, h, spawn, edge,
    x: edge.x, y: edge.y,
    base: { ...spawn },
    mode: 'emerging',
    holdNeck: !!opts.holdNeck,
    // the neck tip stops under the card's near region, so the arm stays
    // visible while the item unfurls past it
    handle: blob.emitTo(lerp(edge.x, spawn.x, 0.82), lerp(edge.y, spawn.y, 0.82)),
    ph: (items.length * 1.7 + 0.6) % 6.28,
  };
  // unfurl out of the neck: transform-origin faces the blob
  const dx = spawn.x - edge.x, dy = spawn.y - edge.y;
  const dl = Math.hypot(dx, dy) || 1;
  el.style.transformOrigin =
    `${50 - (dx / dl) * 50}% ${50 - (dy / dl) * 50}%`;
  setTransform(it, 0.01, 0.01, 0);
  el.addEventListener('click', () => {
    if (now() - it.dragEnd < 0.4) return;   // a drag release is not a click
    if (it.mode === 'float' && !expanded) expand(it);
  });
  it.dragEnd = -1e9;
  items.push(it);
  return it;
}

function releaseNeck(it) {
  if (it.handle) { it.handle.release(); it.handle = null; }
  if (it.mode === 'held' || it.mode === 'emerging') it.mode = 'float';
}

function removeItem(it) {
  if (it.handle) { it.handle.cancel(); it.handle = null; }
  if (it === macItem) { try { window.echoVnc.close(); } catch {} macItem = null; }
  if (it === expanded) expanded = null;
  it.el.remove();
  const i = items.indexOf(it);
  if (i >= 0) items.splice(i, 1);
}

// ------------------------------------------------------------ item updates
function updateItems(t) {
  // items and wisps belong to the revealed blob: they fade with the pour in
  // both directions (without this, the per-frame opacity writes below would
  // clobber dismissScene's fade and items would pop out at the very end)
  const fade = sstep(0.55, 0.95, rv.value);
  for (const it of items) {
    if (it.mode === 'emerging') {
      const p = it.handle ? it.handle.progress : 1;
      it.x = lerp(it.edge.x, it.spawn.x, p);
      it.y = lerp(it.edge.y, it.spawn.y, p);
      const sx = 0.14 + 0.86 * easeOutBack(clamp(p * 1.15, 0, 1));
      const sy = 0.14 + 0.86 * easeOutBack(clamp(p * 1.3 - 0.15, 0, 1));
      setTransform(it, sx, sy, sstep(0.02, 0.2, p) * fade);
      if (p >= 0.999) {
        it.base = { x: it.spawn.x, y: it.spawn.y };
        if (it.holdNeck) it.mode = 'held';
        else { releaseNeck(it); it.mode = 'float'; }
      }
    } else if (it.mode === 'held' || it.mode === 'float') {
      const drift = it.mode === 'held' ? 2.5 : 5;
      it.x = it.base.x + Math.sin(t * 0.53 + it.ph) * drift;
      it.y = it.base.y + Math.sin(t * 0.71 + it.ph * 1.3) * drift * 1.2;
      setTransform(it, 1, 1, fade);
    }
    // expanded/absorbing: CSS transitions own the element
  }
  for (let i = wisps.length - 1; i >= 0; i--) {
    const w = wisps[i];
    const age = t - w.t0;
    if (age > w.life) { w.el.remove(); wisps.splice(i, 1); continue; }
    const rise = easeOutCubic(clamp(age / w.life, 0, 1)) * 110;
    const alpha = sstep(0, 0.06, age / w.life) * (1 - sstep(0.62, 1, age / w.life));
    w.el.style.transform =
      `translate(${w.x + Math.sin(age * 0.9 + w.ph) * 8}px, ${w.y - rise}px) translate(-50%, -100%)`;
    w.el.style.opacity = alpha * fade;
  }
}

// --------------------------------------------------------------- doc cards
function spawnDoc(title, body, opts = {}) {
  const el = document.createElement('div');
  el.className = 'doc';
  const head = document.createElement('div');
  head.className = 'doc-title';
  head.textContent = title;
  const pre = document.createElement('div');
  pre.className = 'doc-body';
  pre.textContent = (body || '').slice(0, 4000);
  el.append(head, pre);
  return emergeItem(el, 'doc', 250, 170, opts);
}

// -------------------------------------------------------------- echoecho's Mac
const MAC_TITLE_H = 22, MAC_PAD = 9;
function macSize(w) {
  const sw = w - MAC_PAD * 2;
  return { w, h: Math.round(sw * 10 / 16) + MAC_TITLE_H + MAC_PAD * 2, sw };
}
function spawnMac(opts = {}) {
  if (macItem) return macItem;
  const el = document.createElement('div');
  el.className = 'mac';
  const head = document.createElement('div');
  head.className = 'mac-title';
  const label = document.createElement('span');
  label.textContent = "echoecho's Mac";
  const btn = document.createElement('button');
  btn.className = 'mac-viewonly';
  btn.textContent = 'view-only: off';
  let viewOnly = false;
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    viewOnly = !viewOnly;
    window.echoVnc.setViewOnly(viewOnly);
    btn.textContent = viewOnly ? 'view-only: on' : 'view-only: off';
    btn.classList.toggle('on', viewOnly);
  });
  head.append(label, btn);
  const screen = document.createElement('div');
  screen.className = 'mac-screen';
  el.append(head, screen);
  const size = macSize(380);
  macItem = emergeItem(el, 'mac', size.w, size.h, opts);
  // echoVnc renders its own status ("echoecho's Mac is asleep") into the screen
  window.echoVnc.open(screen, {}).catch(() => {});
  return macItem;
}

// -------------------------------------------------------- expand / restore
const EXPAND_MS = 550;
function layoutExpanded(it) {
  let w, h;
  if (it.kind === 'mac') {
    w = Math.min(innerWidth * 0.92, ((innerHeight * 0.92) - MAC_TITLE_H - MAC_PAD * 2) * 1.6 + MAC_PAD * 2);
    h = macSize(Math.round(w)).h;
  } else {
    w = innerWidth * 0.92; h = innerHeight * 0.92;
  }
  // anchor top-right: the bottom-left corner stays free for the companion
  it.x = innerWidth - 24 - w / 2;
  it.y = 24 + h / 2;
  it.el.style.width = Math.round(w) + 'px';
  it.el.style.height = Math.round(h) + 'px';
  setTransform(it, 1, 1, 1);
}

function expand(it) {
  if (expanded || (it.mode !== 'float' && it.mode !== 'held')) return;
  releaseNeck(it);
  expanded = it;
  it.mode = 'expanded';
  it.el.style.transformOrigin = '50% 50%';
  it.el.classList.add('anim', 'expanded');
  layoutExpanded(it);
  applyRest(true);                       // blob becomes a small companion
}

function restore() {
  const it = expanded;
  if (!it) return;
  expanded = null;
  it.el.classList.remove('expanded');
  it.el.style.width = it.w + 'px';
  it.el.style.height = it.h + 'px';
  it.x = it.base.x; it.y = it.base.y;
  setTransform(it, 1, 1, 1);
  applyRest(false);
  setTimeout(() => {
    it.el.classList.remove('anim');
    if (it.mode === 'expanded') it.mode = 'float';
  }, EXPAND_MS);
}

// ------------------------------------------------------------------ absorb
function absorbItem(it) {
  if (!it || it.mode === 'absorbing') return Promise.resolve();
  if (it === expanded) restore();
  releaseNeck(it);
  it.mode = 'absorbing';
  const edge = blob.edgePoint(it.x, it.y);
  const done = blob.absorbFrom(it.x, it.y);
  it.el.classList.add('absorb');
  // matches the blob's tongue: reach 0.5 s, pull 0.9 s
  requestAnimationFrame(() => {
    it.x = edge.x; it.y = edge.y;
    setTransform(it, 0.08, 0.08, 0);
  });
  return done.then(() => removeItem(it));
}

// ------------------------------------------------------------------- wisps
function wisp(role, text) {
  if (!text) return;
  const el = document.createElement('div');
  el.className = 'wisp ' + (role === 'user' ? 'user' : 'assistant');
  el.textContent = text.length > 140 ? text.slice(0, 137) + '…' : text;
  el.style.opacity = 0;
  itemsEl.appendChild(el);
  // conversation drifts up the blob's left; items emerge to its right
  const x = clamp(pose.x - (pose.r * 1.5 + 64) + (role === 'user' ? -12 : 30),
                  140, innerWidth - 140);
  const y = pose.y - pose.r * (role === 'user' ? 0.55 : 1.1);
  wisps.push({ el, x, y, t0: now(), life: 7, ph: wisps.length * 2.4 });
  while (wisps.length > 5) { wisps[0].el.remove(); wisps.shift(); }
}

// ------------------------------------------------------------- live wiring
const running = new Set();
let baseActivity = 0.1;
function setBaseActivity() {
  baseActivity = running.size ? 0.75 : 0.1;
  blob.setActivity(baseActivity);
}
let pulseTimer = null;
function pulseActivity() {
  blob.setActivity(1);
  clearTimeout(pulseTimer);
  pulseTimer = setTimeout(() => blob.setActivity(baseActivity), 2600);
}

async function taskDocCard(e) {
  const arts = e.artifacts_touched || e.artifacts || [];
  let title = arts[0] || `task ${e.task_id || ''} · ${e.kind || 'done'}`;
  let body = e.say || '';
  if (arts[0]) {
    try { body = await window.orb.doc(arts[0]); } catch {}
  }
  const docs = items.filter((i) => i.kind === 'doc');
  if (docs.length >= 2) absorbItem(docs[0]);   // keep the scene calm
  spawnDoc(title, body);
}

window.orb.onEvents((evts) => {
  for (const e of evts) {
    if (e.type === 'user_text') wisp('user', e.text);
    else if (e.type === 'assistant_text') wisp('assistant', e.text);
    else if (e.type === 'task') {
      if (e.status === 'running' || e.status === 'progress') running.add(e.task_id);
      else if (e.status === 'done' || e.status === 'error') running.delete(e.task_id);
      if (e.status === 'done') taskDocCard(e);
      setBaseActivity();
    }
  }
});

// --------------------------------------------------------------- lifecycle
window.orb.onReveal(({ anchor: a, reason }) => {
  if (a) anchor = a;
  hideWhenDone = false;
  applyRest(!!expanded);
  revealTo(1, REVEAL_DUR);
  if (reason === 'wake') pulseActivity();
  if (DEMO && demoT0 === null) startDemo();
});

function dismissScene() {
  if (expanded) restore();
  // drop any in-flight grab: the window hides before its mouseup arrives, and
  // a stale drag state would hold passthrough off — a full-screen invisible
  // window silently eating every click until the next mouseup
  drag = null;
  if (itemDrag) { itemDrag.it.el.style.cursor = ''; itemDrag = null; }
  hideWhenDone = true;
  revealTo(0, DISMISS_DUR);
  for (const it of items) {
    // retract any live neck: emissions don't ride the reveal transform, so a
    // held arm would otherwise stay orphaned mid-air while the body pours away
    if (it.handle) { it.handle.cancel(); it.handle = null; }
    if (it.mode === 'emerging' || it.mode === 'held') {
      it.base = { x: it.x, y: it.y };
      it.mode = 'float';
    }
    it.el.style.opacity = 0;
  }
  for (const w of wisps) w.el.style.opacity = 0;
}
window.orb.onDismiss(dismissScene);

addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (expanded) restore();
  else window.orb.dismissRequest();
});

// clicking the companion blob restores; double-click summons echoecho's Mac
canvas.addEventListener('click', (e) => {
  if (!expanded) return;
  if (Math.hypot(e.clientX - pose.x, e.clientY - pose.y) < pose.r * 1.8) restore();
});
canvas.addEventListener('dblclick', (e) => {
  if (expanded || rv.value < 0.99 || now() - lastDragEnd < 0.4) return;
  if (Math.hypot(e.clientX - pose.x, e.clientY - pose.y) < pose.r * 1.8) spawnMac();
});

// ---------------------------------------------- tight hit region + dragging
// The window spans most of the screen; everywhere except the blob's own
// silhouette and the items must let clicks fall through to whatever is
// behind. Main keeps forwarding mousemove while ignored, so hover decides.
let passthrough = null;               // tri-state: avoid IPC spam per frame
function setPassthrough(on) {
  if (passthrough === on) return;
  passthrough = on;
  window.orb.setPassthrough(on);
}

function overItem(x, y) {
  for (const it of items) {
    const r = it.el.getBoundingClientRect();
    if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) return true;
  }
  return false;
}

let drag = null;                      // blob drag: { dx, dy, moved }
let itemDrag = null;                  // item drag: { it, dx, dy, moved }
let lastDragEnd = -1e9;

function updateHover(x, y) {
  const onBlob = blob.hitTest(x, y);
  setPassthrough(!(drag || itemDrag || onBlob || overItem(x, y)));
  canvas.style.cursor =
    drag ? 'grabbing' : onBlob ? (expanded ? 'pointer' : 'grab') : '';
}

// which item a press may drag: docs grab anywhere on the card, echoecho's Mac
// only by its title bar (the VNC screen owns its own mouse), never buttons,
// never mid-animation modes
function dragTargetItem(e) {
  const el = e.target.closest ? e.target.closest('.item') : null;
  if (!el) return null;
  const it = items.find((i) => i.el === el);
  // only settled items: mid-emerge the visual position lags `base`, so a
  // grab would snap the card to its landing spot
  if (!it || (it.mode !== 'float' && it.mode !== 'held')) return null;
  if (e.target.closest('button')) return null;
  if (it.kind === 'mac' && !e.target.closest('.mac-title')) return null;
  return it;
}

window.addEventListener('mousedown', (e) => {
  if (e.button !== 0 || expanded || rv.value < 0.95) return;
  // grab the landed blob (not the companion — clicking that restores); only
  // from the canvas itself, so item presses that overlap the silhouette
  // (bubbled here) never start a drag
  if (e.target === canvas && blob.hitTest(e.clientX, e.clientY)) {
    drag = { dx: pose.x - e.clientX, dy: pose.y - e.clientY, moved: false };
    updateHover(e.clientX, e.clientY);
    return;
  }
  const it = dragTargetItem(e);
  if (it) {
    itemDrag = { it, dx: it.base.x - e.clientX, dy: it.base.y - e.clientY, moved: false };
    updateHover(e.clientX, e.clientY);
  }
});

window.addEventListener('mousemove', (e) => {
  if (drag) {
    const nx = e.clientX + drag.dx, ny = e.clientY + drag.dy;
    if (!drag.moved && Math.hypot(nx - pose.x, ny - pose.y) > 3) drag.moved = true;
    if (drag.moved) {
      const r = pose.r;
      pose = { x: clamp(nx, r * 0.5, innerWidth - r * 0.5),
               y: clamp(ny, r * 0.5, innerHeight - r * 0.5), r };
      blob.setRest(pose.x, pose.y, r, 0.12);   // short ease: liquid trail
    }
  } else if (itemDrag) {
    const it = itemDrag.it;
    const nx = e.clientX + itemDrag.dx, ny = e.clientY + itemDrag.dy;
    if (!itemDrag.moved && Math.hypot(nx - it.base.x, ny - it.base.y) > 3) {
      itemDrag.moved = true;
      releaseNeck(it);                         // a held card comes free in hand
      it.el.style.cursor = 'grabbing';
    }
    if (itemDrag.moved) {
      // same margins as spawnPointFor: anywhere onscreen, never lost offscreen
      it.base.x = clamp(nx, it.w / 2 + 12, innerWidth - it.w / 2 - 12);
      it.base.y = clamp(ny, it.h / 2 + 12, innerHeight - it.h / 2 - 12);
    }
  } else if (e.buttons) {
    // mid-press over an item (VNC drag, text selection in a doc): freeze the
    // passthrough state so the gesture can stray off the item without the
    // window going click-through under it
    return;
  }
  updateHover(e.clientX, e.clientY);
});

window.addEventListener('mouseup', (e) => {
  if (itemDrag) {
    if (itemDrag.moved) itemDrag.it.dragEnd = now();  // swallow the trailing click
    itemDrag.it.el.style.cursor = '';
    itemDrag = null;
    updateHover(e.clientX, e.clientY);
    return;
  }
  if (!drag) return;
  if (drag.moved) {
    lastDragEnd = now();                       // swallow the trailing dblclick
    userPose = { fx: pose.x / innerWidth, fy: pose.y / innerHeight };
    try { localStorage.setItem('echoecho.pose', JSON.stringify(userPose)); } catch {}
  }
  drag = null;
  updateHover(e.clientX, e.clientY);
});

document.addEventListener('mouseleave', () => setPassthrough(true));

function clearItems() {
  while (items.length) removeItem(items[items.length - 1]);
  while (wisps.length) { wisps[0].el.remove(); wisps.shift(); }
}

// -------------------------------------------------------------- demo mode
const DEMO_DOC_TITLE = 'listings-notes.md';
const DEMO_DOC_BODY = [
  'Housing search — Wednesday notes',
  '',
  'Two new listings match the brief:',
  '',
  '1. Maple St duplex — $2,850/mo',
  '   south-facing, small yard,',
  '   10 min bike to campus.',
  '   Photos: bright kitchen, worn bath.',
  '',
  '2. Alder Ct apartment — $2,400/mo',
  '   quiet block, parking included.',
  '   Open house Saturday 11–1.',
  '',
  'Next: email both agents, ask about',
  'pet policy and lease start dates.',
].join('\n');

let demoT0 = null;
let demoDoc = null;
let demoScript = [];
function startDemo() {
  demoT0 = now();
  if (window.orb.demoStarted) window.orb.demoStarted();
  demoScript = [
    [2.6, () => wisp('user', 'echoecho — anything new on the housing search?')],
    [3.5, () => wisp('assistant', 'Two new listings this morning. I wrote up notes.')],
    [4.4, () => blob.setActivity(0.7)],
    [5.0, () => { demoDoc = spawnDoc(DEMO_DOC_TITLE, DEMO_DOC_BODY, { holdNeck: true }); }],
    [6.9, () => spawnMac()],
    [7.6, () => blob.setActivity(0.15)],
    [9.7, () => releaseNeck(demoDoc)],
    [10.3, () => expand(demoDoc)],
    [12.8, () => restore()],
    [14.0, () => absorbItem(demoDoc)],
  ];
}
if (DEMO) document.body.classList.add('demo');

// -------------------------------------------------------------- frame loop
function frame() {
  const t = now();
  const k = clamp((t - rv.t0) / rv.dur, 0, 1);
  rv.value = rv.from + (rv.target - rv.from) * easeOutCubic(k);
  blob.setReveal(rv.value, anchor);
  if (hideWhenDone && rv.target === 0 && k >= 1) {
    hideWhenDone = false;
    clearItems();
    setPassthrough(true);   // next summon starts click-through until hover
    window.orb.hidden();
  }
  if (demoT0 !== null) {
    const dt = t - demoT0;
    while (demoScript.length && demoScript[0][0] <= dt) demoScript.shift()[1]();
  }
  updateItems(t);
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

addEventListener('resize', () => {
  applyRest(!!expanded);
  if (expanded) layoutExpanded(expanded);
  // keep float/restore targets onscreen (same margins as spawnPointFor)
  for (const it of items) {
    it.base.x = clamp(it.base.x, it.w / 2 + 12, innerWidth - it.w / 2 - 12);
    it.base.y = clamp(it.base.y, it.h / 2 + 12, innerHeight - it.h / 2 - 12);
  }
});

})();
