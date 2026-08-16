// echoecho's blob — production metaball organism (ported from
// prototypes/blob-metaball2d.html; reveal choreography from blob-glslfield).
//
// Field:  core/ring metaballs + pseudopod chains + emission necks, splatted
//         with a finite-support Wyvill falloff into a 1/2-resolution Float32
//         buffer (FSCALE=2 — sharper silhouette than the prototype's 1/3).
// Shade:  finite-difference gradient -> approx signed distance -> antialiased
//         silhouette; fake hemisphere normal -> deep black body with only a
//         whisper of cool rim, a tight key specular and a slow orbiting sheen.
// Reveal: t in [0,1], set by the scene. The body balls string out along one
//         cubic bezier from the anchor, welded by stream links -> a single
//         coherent beaded strand pouring from the anchor (drips allowed, no
//         disconnected splatter). Dismissal runs it backwards for free.
// Motion: all randomness from mulberry32(0x5EED) at init; every position is
//         a pure function of the animation clock (+ the scene's call times).
//         ?t=SECONDS advances the clock on load (debugging only).
'use strict';

(() => {

const canvas = document.getElementById('blob');
const ctx = canvas.getContext('2d', { alpha: true });

// ---------------------------------------------------------------- utilities
const clamp = (x, a, b) => x < a ? a : x > b ? b : x;
const sstep = (a, b, x) => { x = clamp((x - a) / (b - a), 0, 1); return x * x * (3 - 2 * x); };
const lerp  = (a, b, p) => a + (b - a) * p;
const easeOutCubic  = p => 1 - (1 - p) * (1 - p) * (1 - p);
const easeInCubic   = p => p * p * p;
const easeInOut     = p => p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2;
const easeOutBack   = p => { const k = 1.35; const q = p - 1; return 1 + q * q * ((k + 1) * q + k); };

function mulberry32(a) {
  return function () {
    a |= 0; a = a + 0x6D2B79F5 | 0;
    let t = Math.imul(a ^ a >>> 15, 1 | a);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}
const rng = mulberry32(0x5EED);

// 3-octave seeded sine mix; incommensurate freqs => no visible loop
function makeWobble(amp, fLo, fHi) {
  const o = [];
  for (let k = 0; k < 3; k++) {
    o.push({ a: amp * (0.62 - 0.16 * k) * (0.7 + 0.6 * rng()),
             w: (fLo + (fHi - fLo) * rng()) * (1 + 0.6180339 * k),
             p: rng() * Math.PI * 2 });
  }
  return t => o[0].a * Math.sin(o[0].w * t + o[0].p)
            + o[1].a * Math.sin(o[1].w * t + o[1].p)
            + o[2].a * Math.sin(o[2].w * t + o[2].p);
}

// ------------------------------------------------------------------- layout
const FSCALE = 2;                       // field buffer is 1/2 device resolution
let W, H, FW, FH, fpx, field, img, fcan, fctx;
function resize() {
  W = window.innerWidth; H = window.innerHeight;
  // backing store in device px (dpr capped: the field pass is O(pixels));
  // CSS size comes from the #blob inset:0 rule. Public API stays CSS px.
  const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
  canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
  FW = Math.ceil(canvas.width / FSCALE); FH = Math.ceil(canvas.height / FSCALE);
  fpx = FSCALE / dpr;                   // CSS px per field cell
  field = new Float32Array(FW * FH);
  fcan = document.createElement('canvas'); fcan.width = FW; fcan.height = FH;
  fctx = fcan.getContext('2d');
  img = fctx.createImageData(FW, FH);
}
window.addEventListener('resize', resize);
resize();

// ---------------------------------------------------------------- the clock
const qs = new URLSearchParams(location.search);
const timeOffset = parseFloat(qs.get('t') || '0') || 0;
let t0 = null;
let lastT = 0;
const clock = () => lastT;

// ----------------------------------------------------- scene-driven params
let reveal = 0;
let anchor = { x: W - 40, y: 0 };

// rest pose: where the body idles; scene moves it, we ease between poses
const POSE_DUR = 0.9;
// placeholder until the scene's first setRest (see scene.js REST_R_FRAC)
let poseFrom = { x: W * 0.5, y: H * 0.5, r: Math.min(W, H) * 0.035 };
let poseTo = { ...poseFrom };
let poseT0 = -1e9;
let poseDur = POSE_DUR;
function evalPose(t) {
  const k = easeInOut(clamp((t - poseT0) / poseDur, 0, 1));
  return { x: lerp(poseFrom.x, poseTo.x, k),
           y: lerp(poseFrom.y, poseTo.y, k),
           r: lerp(poseFrom.r, poseTo.r, k) };
}

// activity: 0 calm .. 1 agitated; eased so pulses stay liquid
const ACT_DUR = 0.8;
let actFrom = 0, actTo = 0, actT0 = -1e9;
function evalAct(t) {
  return lerp(actFrom, actTo, easeInOut(clamp((t - actT0) / ACT_DUR, 0, 1)));
}

// ------------------------------------------------------------ ball ensemble
// offsets/radii in units of pose.r; the first ball is the pour head
const coreBalls = [
  { ox: 0,     oy: 0,    r: 0.62 },
  { ox: 0.16,  oy: 0.10, r: 0.52 },
];
const ringBalls = [];
const N_RING = 8;
for (let i = 0; i < N_RING; i++) {
  ringBalls.push({
    ang0: i / N_RING * Math.PI * 2 + (rng() - 0.5) * 0.5,
    spin: (rng() < 0.5 ? -1 : 1) * (0.035 + 0.05 * rng()),
    dist: 0.46 + 0.14 * rng(),
    r:    0.34 + 0.12 * rng(),
    wAng:  makeWobble(0.16, 0.10, 0.35),
    wDist: makeWobble(0.07, 0.12, 0.40),
    wR:    makeWobble(0.05, 0.15, 0.45),
  });
}
const N_BODY = coreBalls.length + N_RING;
// pseudopods: occasional tendrils reaching out and retracting
const pods = [];
for (let i = 0; i < 3; i++) {
  pods.push({
    ang0:   rng() * Math.PI * 2,
    wAng:   makeWobble(0.45, 0.04, 0.12),
    period: 6.5 + 4.5 * rng(),
    phase:  rng() * Math.PI * 2,
    len:    0.45 + 0.3 * rng(),
  });
}
const breatheA = makeWobble(0.033, 1.1, 1.7);
const breatheB = makeWobble(0.018, 2.4, 3.4);
const tremor   = makeWobble(0.030, 3.5, 6.0);   // extra shiver when agitated
const wanderX = [], wanderY = [];
for (let i = 0; i < 16; i++) { wanderX.push(makeWobble(0.08, 0.15, 0.5)); wanderY.push(makeWobble(0.08, 0.15, 0.5)); }

// --------------------------------------------------------------- the reveal
// One poured strand: each body ball lags the reveal by its chain rank, so the
// body stretches into a beaded stream between anchor and rest (glslfield look).
let revealDoneT = Infinity;             // when g last crossed to ~1 (settle)
function bez(u, pose) {
  const p1x = anchor.x - W * 0.02, p1y = anchor.y + H * 0.34;
  const p2x = pose.x + W * 0.18,   p2y = pose.y - H * 0.38;
  const v = 1 - u;
  const a = v * v * v, b = 3 * v * v * u, c = 3 * v * u * u, d = u * u * u;
  return [a * anchor.x + b * p1x + c * p2x + d * pose.x,
          a * anchor.y + b * p1y + c * p2y + d * pose.y];
}

// ------------------------------------------------------- emissions / necks
// emitTo: bulge -> beaded neck grows to the target, holds until release(),
// then snaps back. absorbFrom: tongue reaches the target and gulps it in.
const EMIT = { bulge: 0.55, grow: 1.0, release: 0.5, cancel: 0.3 };
const ABSORB = { reach: 0.5, pull: 0.9 };
const emissions = [];

function emissionBalls(em, t, pose, out) {
  const R = pose.r;
  let dx = em.tx - pose.x, dy = em.ty - pose.y;
  const dl = Math.hypot(dx, dy) || 1;
  dx /= dl; dy /= dl;
  const px = -dy, py = dx;
  const ex = pose.x + dx * R * 0.88, ey = pose.y + dy * R * 0.88;
  const tau = t - em.t0;

  if (em.mode === 'absorb') {
    const total = ABSORB.reach + ABSORB.pull;
    if (tau >= total) { em.dead = true; return; }
    let tip, taper;
    if (tau < ABSORB.reach) {                  // tongue reaches out
      const p = easeOutCubic(tau / ABSORB.reach);
      tip = p; taper = p;
    } else {                                   // pulls the item in
      const q = (tau - ABSORB.reach) / ABSORB.pull;
      tip = 1 - easeInCubic(q);
      taper = 1 - q * 0.5;
      const gulp = Math.sin(Math.PI * clamp(q * 1.15, 0, 1));
      out.push([pose.x + dx * R * (0.5 + 0.42 * gulp),
                pose.y + dy * R * (0.5 + 0.42 * gulp), R * 0.46 * gulp]);
    }
    const tx = lerp(ex, em.tx, tip), ty = lerp(ey, em.ty, tip);
    const nk = 10;
    for (let j = 0; j <= nk; j++) {
      const s = j / nk;
      const sag = Math.sin(s * Math.PI) * 5 * Math.sin(t * 2.9 + s * 6 + em.t0);
      out.push([lerp(ex, tx, s) + px * sag, lerp(ey, ty, s) + py * sag,
                lerp(R * 0.28, R * 0.12, s) * taper]);
    }
    return;
  }

  // ---- emit
  let retract = 0, die = 1;
  if (em.cancelT < Infinity) {
    const q = (t - em.cancelT) / EMIT.cancel;
    if (q >= 1) { em.dead = true; return; }
    retract = easeOutCubic(q); die = 1 - q;
  } else if (em.releaseT < Infinity && t >= em.releaseT) {
    // a release scheduled in the future keeps normal grow/hold until its time
    const q = (t - em.releaseT) / EMIT.release;
    if (q >= 1) { em.dead = true; return; }
    retract = easeOutBack(q); die = 1 - q;
  }
  if (tau < EMIT.bulge) {                      // lean + bulge toward the spawn
    const bp = sstep(0, 1, tau / EMIT.bulge);
    out.push([pose.x + dx * R * (0.5 + 0.35 * bp),
              pose.y + dy * R * (0.5 + 0.35 * bp), R * 0.42 * bp]);
    return;
  }
  const p = easeOutCubic(clamp((tau - EMIT.bulge) / EMIT.grow, 0, 1));
  const tipX = lerp(ex, em.tx, p), tipY = lerp(ey, em.ty, p);
  out.push([ex, ey, R * (0.42 - 0.12 * p) * Math.max(die, 0.4)]);
  const nk = 10;
  // holding: slow peristalsis along the arm so the grip stays alive
  const hold = (p >= 1 && retract === 0) ? 1 : 0;
  for (let j = 1; j <= nk; j++) {
    let s = (j / nk) * (1 - retract);
    const sag = Math.sin(s * Math.PI) * 6 * Math.sin(t * 2.6 + s * 7 + em.t0);
    const rr = lerp(R * 0.28, R * 0.12, j / nk) * sstep(0, 0.15, p)
             * (retract > 0 ? die * die : 1)
             * (1 + hold * 0.06 * Math.sin(t * 2.2 + s * 4.2));
    if (rr < 0.5) continue;
    out.push([lerp(ex, tipX, s) + px * sag, lerp(ey, tipY, s) + py * sag, rr]);
  }
}

// --------------------------------------------------- assemble balls (px, t)
function collectBalls(t) {
  const g = reveal;
  const pose = evalPose(t);
  const act = evalAct(t);
  const R = pose.r;
  if (g >= 0.999) { if (revealDoneT === Infinity) revealDoneT = t; }
  else if (g < 0.95) revealDoneT = Infinity;
  let settle = 1;
  if (revealDoneT < Infinity) {
    const ts = t - revealDoneT;
    settle = 1 + 0.05 * Math.sin(ts * 7) * Math.exp(-ts * 2.4);
  }
  const breathe = (1 + (breatheA(t) + breatheB(t)) * (1 + 1.5 * act)
                  + tremor(t) * act) * settle;
  const wob = 1 + 0.9 * act;
  const out = [];

  // rest-pose body positions (chain order: head core ball first)
  const rest = [];
  let bi = 0;
  for (const b of coreBalls) {
    rest.push([pose.x + (b.ox + wanderX[bi](t) * wob) * R,
               pose.y + (b.oy + wanderY[bi](t) * wob) * R,
               b.r * R * breathe]);
    bi++;
  }
  for (const b of ringBalls) {
    const ang = b.ang0 + b.spin * t + b.wAng(t) * wob;
    const d = (b.dist + b.wDist(t) * wob) * R * breathe;
    rest.push([pose.x + Math.cos(ang) * d, pose.y + Math.sin(ang) * d,
               (b.r + b.wR(t) * wob) * R * breathe]);
    bi++;
  }

  if (g >= 0.999) {
    for (const p of rest) out.push(p);
  } else {
    // pour: string the chain along the bezier, welded into one strand
    const st = 0.85;
    const chain = [];
    for (let i = 0; i < N_BODY; i++) {
      const fi = i / (N_BODY - 1);
      const si = clamp(g * (1 + st) - st * fi, 0, 1);
      const u = 1 - (1 - si) * (1 - si);
      const [bx, by] = bez(u, pose);
      const k = si * si;
      const rr = lerp(R * (0.055 + 0.06 * (1 - fi)), rest[i][2], Math.pow(si, 0.7));
      chain.push([bx + (rest[i][0] - pose.x) * k,
                  by + (rest[i][1] - pose.y) * k, rr]);
    }
    for (const p of chain) out.push(p);
    // stream links between chain neighbours: the beads become one strand
    const weld = 1 - sstep(0.92, 1, g);
    for (let i = 0; i + 1 < N_BODY; i++) {
      const a = chain[i], b = chain[i + 1];
      const rl = Math.min(a[2], b[2]) * 0.62 * weld;
      if (rl < 0.5) continue;
      for (const m of [1 / 3, 2 / 3]) {
        out.push([lerp(a[0], b[0], m),
                  lerp(a[1], b[1], m) + Math.sin(t * 3.1 + i * 2.1) * 2 * (1 - g),
                  rl]);
      }
    }
    // clinging droplet at the anchor, detaching late, with trailing drips
    const rA = R * 0.14 * Math.pow(1 - g, 1.4);
    if (rA > 0.5) out.push([anchor.x, anchor.y + 2 * g, rA]);
    const fade = 1 - sstep(0.55, 0.9, g);
    if (fade > 0.02) {
      const ss = [0.02 + 0.05 * g, 0.08 + 0.10 * g];
      for (let j = 0; j < 2; j++) {
        const [bx, by] = bez(ss[j], pose);
        out.push([bx, by, R * (0.11 - j * 0.035) * fade]);
      }
    }
  }

  // pseudopods only once the reveal has landed and settled
  const podGate = revealDoneT < Infinity ? sstep(0.4, 1.5, t - revealDoneT) : 0;
  if (podGate > 0.01) {
    for (const p of pods) {
      const s = Math.sin(Math.PI * 2 * t / p.period + p.phase);
      const e = sstep(0.55, 0.95, s) * podGate * (0.7 + 0.6 * act);
      const ang = p.ang0 + p.wAng(t);
      const ca = Math.cos(ang), sa = Math.sin(ang);
      const segN = 7, gate = sstep(0.04, 0.25, e);
      for (let k = 0; k < segN; k++) {
        const fr = k / (segN - 1);
        const d = 0.55 + e * p.len * fr;
        const r = lerp(0.24, 0.10, fr) * (k === 0 ? 1 : gate) * (1 - 0.2 * e * fr);
        if (r < 0.01) continue;
        out.push([pose.x + ca * d * R, pose.y + sa * d * R, r * R]);
      }
    }
  }

  // emissions belong to the landed body: they don't ride the reveal
  // transform, so while pouring (either direction) their radii are gated to
  // zero — otherwise a neck/tongue would float mid-air, detached from the
  // strand. Timelines still advance while gated, so they self-expire.
  if (emissions.length) {
    const emGate = sstep(0.55, 0.95, g);
    const eb = [];
    for (let i = emissions.length - 1; i >= 0; i--) {
      emissionBalls(emissions[i], t, pose, eb);
      if (emissions[i].dead) emissions.splice(i, 1);
    }
    if (emGate > 0.01) {
      for (const p of eb) { p[2] *= emGate; out.push(p); }
    }
  }
  return out;
}

// ----------------------------------------------------- field splat + shade
const LX = -0.4657, LY = -0.6985, LZ = 0.5433;          // key from top-left
const HX = -0.2651, HY = -0.3976, HZ = 0.8784;          // its half-vector
function renderBlob(t, balls, R) {
  field.fill(0);
  let x0 = FW, y0 = FH, x1 = 0, y1 = 0;
  const K = 2.2;                                        // influence = K * r
  for (const [bx, by, br] of balls) {
    if (br <= 0.5) continue;
    const cx = bx / fpx, cy = by / fpx, Ri = K * br / fpx;
    const invR2 = 1 / (Ri * Ri);
    const ax = Math.max(1, Math.floor(cx - Ri)), bxx = Math.min(FW - 2, Math.ceil(cx + Ri));
    const ay = Math.max(1, Math.floor(cy - Ri)), byy = Math.min(FH - 2, Math.ceil(cy + Ri));
    if (ax > bxx || ay > byy) continue;
    if (ax < x0) x0 = ax; if (bxx > x1) x1 = bxx;
    if (ay < y0) y0 = ay; if (byy > y1) y1 = byy;
    for (let y = ay; y <= byy; y++) {
      const dy = y - cy, dy2 = dy * dy;
      let i = y * FW + ax;
      for (let x = ax; x <= bxx; x++, i++) {
        const dx = x - cx;
        const q = (dx * dx + dy2) * invR2;
        if (q < 1) { const u = 1 - q; field[i] += u * u * 1.5; }
      }
    }
  }
  const data = img.data;
  data.fill(0);
  if (x1 < x0) { fctx.putImageData(img, 0, 0); return; }
  x0 = Math.max(1, x0 - 3); y0 = Math.max(1, y0 - 3);
  x1 = Math.min(FW - 2, x1 + 3); y1 = Math.min(FH - 2, y1 + 3);

  const HDEPTH = 0.55 * R / fpx;                        // hemisphere depth, px
  // slow-orbiting secondary sheen light (half-vector, view = +z)
  const a2 = t * 0.21;
  const l2x = Math.cos(a2) * 0.52, l2y = Math.sin(a2) * 0.52, l2z = 0.75;
  let h2x = l2x, h2y = l2y, h2z = l2z + 1;
  const h2l = Math.sqrt(h2x * h2x + h2y * h2y + h2z * h2z);
  h2x /= h2l; h2y /= h2l; h2z /= h2l;

  for (let y = y0; y <= y1; y++) {
    for (let x = x0, i = y * FW + x0; x <= x1; x++, i++) {
      const f = field[i];
      if (f < 0.22) continue;
      const gx = (field[i + 1] - field[i - 1]) * 0.5;
      const gy = (field[i + FW] - field[i - FW]) * 0.5;
      const gl = Math.sqrt(gx * gx + gy * gy) + 1e-6;
      const sd = (f - 1) / gl;                          // approx signed dist, px
      if (sd < -3.2) continue;
      const j = i * 4;
      if (sd < -0.75) {                                 // outside: faint cool aura
        const aura = 1 + sd / 3.2;
        const A = aura * aura * 0.05;
        if (A < 0.006) continue;
        data[j] = 8; data[j + 1] = 12; data[j + 2] = 22;
        data[j + 3] = A * 255;
        continue;
      }
      const edgeA = clamp(sd / 1.5 + 0.5, 0, 1);
      const h  = clamp(sd / HDEPTH, 0, 1);
      const nz = Math.sqrt(h * (2 - h));
      const nxy = 1 - h;
      const nx = -gx / gl * nxy, ny = -gy / gl * nxy;
      let ndl = nx * LX + ny * LY + nz * LZ; if (ndl < 0) ndl = 0;
      let ndh = nx * HX + ny * HY + nz * HZ; if (ndh < 0) ndh = 0;
      const s2 = ndh * ndh, s4 = s2 * s2, s8 = s4 * s4, s16 = s8 * s8;
      const spec = s16 * s16 * s16;                     // ndh^48
      let n2 = nx * h2x + ny * h2y + nz * h2z; if (n2 < 0) n2 = 0;
      let t2 = n2 * n2; t2 *= t2; t2 *= t2; t2 *= t2;   // n2^16
      const sheen = t2 * 0.12;
      const fres = 1 - nz, f2 = fres * fres;
      const rim = f2 * (0.35 + 0.65 * fres);
      const ig = nxy * nxy;
      // deep black body: rim/inner glow pulled way down vs the prototype
      const r = 2 + ndl * 4 + ig * 3  + rim * 26 + spec * 175 + sheen * 42;
      const g = 3 + ndl * 5 + ig * 5  + rim * 36 + spec * 195 + sheen * 50;
      const b = 4 + ndl * 7 + ig * 10 + rim * 58 + spec * 225 + sheen * 68;
      const A = edgeA + (1 - edgeA) * 0.06;             // blends into the aura
      const w = edgeA / A, iw = (1 - w);
      data[j]     = clamp(r * w + 8 * iw, 0, 255);
      data[j + 1] = clamp(g * w + 12 * iw, 0, 255);
      data[j + 2] = clamp(b * w + 22 * iw, 0, 255);
      data[j + 3] = A * 255;
    }
  }
  fctx.putImageData(img, 0, 0);
}

// -------------------------------------------------------------------- frame
function draw(t) {
  const balls = collectBalls(t);
  renderBlob(t, balls, evalPose(t).r);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(fcan, 0, 0, FW, FH, 0, 0, FW * FSCALE, FH * FSCALE);
}

requestAnimationFrame(function frame(nowMs) {
  if (t0 === null) t0 = nowMs;
  lastT = (nowMs - t0) / 1000 + timeOffset;
  draw(lastT);
  requestAnimationFrame(frame);
});

// ---------------------------------------------------------- public contract
window.echoechoBlob = {
  // reveal 0..1 (scene eases it); anchor in CSS px
  setReveal(t, a) {
    reveal = clamp(+t || 0, 0, 1);
    if (a) anchor = a;
  },

  // where the body idles; the blob flows there over ~0.9 s (or `dur` seconds
  // — dragging passes a short one so the body trails the cursor, not lags it)
  setRest(cx, cy, r, dur) {
    const t = clock();
    poseFrom = evalPose(t);
    poseTo = { x: cx, y: cy, r };
    poseT0 = t;
    poseDur = dur > 0 ? dur : POSE_DUR;
  },

  // is (x, y) on the rendered body? Samples last frame's field: 1.0 is the
  // drawn iso-surface, 0.8 sits a few px outside it — a tight hit region that
  // hugs the silhouette (pods, necks and all) with just enough grab slack.
  hitTest(x, y) {
    const fx = Math.round(x / fpx), fy = Math.round(y / fpx);
    if (fx < 0 || fy < 0 || fx >= FW || fy >= FH) return false;
    return field[fy * FW + fx] >= 0.8;
  },

  // grow a neck toward (x, y); progress is eased 0..1, the neck tip sits at
  // lerp(edgePoint, target, progress)
  emitTo(x, y) {
    const em = { mode: 'emit', tx: x, ty: y, t0: clock(),
                 releaseT: Infinity, cancelT: Infinity, dead: false };
    emissions.push(em);
    return {
      get progress() {
        return easeOutCubic(clamp((clock() - em.t0 - EMIT.bulge) / EMIT.grow, 0, 1));
      },
      release() {
        if (em.releaseT === Infinity && em.cancelT === Infinity) {
          em.releaseT = Math.max(clock(), em.t0 + EMIT.bulge + EMIT.grow);
        }
      },
      cancel() {
        if (em.cancelT === Infinity) { em.cancelT = clock(); em.releaseT = Infinity; }
      },
    };
  },

  // tongue out to (x, y), swallow; resolves when the gulp lands (the scene
  // hides the item then)
  absorbFrom(x, y) {
    emissions.push({ mode: 'absorb', tx: x, ty: y, t0: clock(),
                     releaseT: Infinity, cancelT: Infinity, dead: false });
    return new Promise((res) => setTimeout(res, (ABSORB.reach + ABSORB.pull) * 1000));
  },

  // point on the rendered silhouette nearest the target (field iso-crossing)
  edgePoint(towardX, towardY) {
    const pose = evalPose(clock());
    let dx = towardX - pose.x, dy = towardY - pose.y;
    const dl = Math.hypot(dx, dy) || 1;
    dx /= dl; dy /= dl;
    let last = -1, misses = 0;
    const maxD = pose.r * 3;
    for (let d = 0; d <= maxD; d += 2) {
      const fx = Math.round((pose.x + dx * d) / fpx);
      const fy = Math.round((pose.y + dy * d) / fpx);
      if (fx < 0 || fy < 0 || fx >= FW || fy >= FH) break;
      if (field[fy * FW + fx] >= 1) { last = d; misses = 0; }
      else if (last >= 0 && ++misses > 3) break;
    }
    const d = last >= 0 ? last : pose.r * 0.85;
    return { x: pose.x + dx * d, y: pose.y + dy * d };
  },

  // 0 calm .. 1 agitated (working); eased internally
  setActivity(level) {
    const t = clock();
    actFrom = evalAct(t);
    actTo = clamp(+level || 0, 0, 1);
    actT0 = t;
  },
};

})();
