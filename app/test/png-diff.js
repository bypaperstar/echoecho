#!/usr/bin/env node
// Pixel-diff two PNGs (as written by Electron capturePage) with zero npm
// deps: zlib inflate + per-row unfilter, 8-bit RGB/RGBA non-interlaced only.
// Usage: node png-diff.js before.png after.png [minChangedPixels]
// Exits 0 iff both images are non-blank AND the changed-pixel count sits in
// [minChangedPixels (default 300), 25% of the frame] — a typed marker is a
// small visible change; ~0 means input never landed, near-full-frame means
// the session died and an error state got captured. Not eyeballed.
'use strict';

const fs = require('fs');
const zlib = require('zlib');

function decodePNG(file) {
  const buf = fs.readFileSync(file);
  if (buf.length < 8 || buf.readUInt32BE(0) !== 0x89504e47) {
    throw new Error(`${file}: not a PNG`);
  }
  let pos = 8;
  let width = 0, height = 0, bpp = 0;
  const idat = [];
  while (pos + 12 <= buf.length) {
    const len = buf.readUInt32BE(pos);
    const type = buf.toString('ascii', pos + 4, pos + 8);
    const data = buf.subarray(pos + 8, pos + 8 + len);
    if (type === 'IHDR') {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      const bitDepth = data[8], colorType = data[9], interlace = data[12];
      if (bitDepth !== 8 || (colorType !== 6 && colorType !== 2) || interlace !== 0) {
        throw new Error(`${file}: unsupported PNG variant (depth ${bitDepth}, color ${colorType})`);
      }
      bpp = colorType === 6 ? 4 : 3;
    } else if (type === 'IDAT') {
      idat.push(data);
    } else if (type === 'IEND') {
      break;
    }
    pos += 12 + len;
  }
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const stride = width * bpp;
  const out = Buffer.alloc(height * stride);
  for (let y = 0; y < height; y++) {
    const filter = raw[y * (stride + 1)];
    const rowIn = raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1));
    const rowOut = out.subarray(y * stride, (y + 1) * stride);
    const prev = y > 0 ? out.subarray((y - 1) * stride, y * stride) : null;
    for (let x = 0; x < stride; x++) {
      const a = x >= bpp ? rowOut[x - bpp] : 0;             // left
      const b = prev ? prev[x] : 0;                          // up
      const c = x >= bpp && prev ? prev[x - bpp] : 0;        // up-left
      let v = rowIn[x];
      switch (filter) {
        case 0: break;
        case 1: v = (v + a) & 0xff; break;
        case 2: v = (v + b) & 0xff; break;
        case 3: v = (v + ((a + b) >> 1)) & 0xff; break;
        case 4: {
          const p = a + b - c;
          const pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
          v = (v + (pa <= pb && pa <= pc ? a : pb <= pc ? b : c)) & 0xff;
          break;
        }
        default: throw new Error(`${file}: bad filter ${filter} on row ${y}`);
      }
      rowOut[x] = v;
    }
  }
  return { width, height, bpp, data: out };
}

function countDistinctSample(img, cap) {
  const seen = new Set();
  const px = img.width * img.height;
  const step = Math.max(1, Math.floor(px / 20000)); // sample, plenty for "blank?"
  for (let i = 0; i < px; i += step) {
    const o = i * img.bpp;
    seen.add((img.data[o] << 16) | (img.data[o + 1] << 8) | img.data[o + 2]);
    if (seen.size >= cap) break;
  }
  return seen.size;
}

const [, , beforeFile, afterFile, minArg] = process.argv;
if (!beforeFile || !afterFile) {
  console.error('usage: node png-diff.js before.png after.png [minChangedPixels]');
  process.exit(2);
}
const MIN_CHANGED = Number(minArg || 300);

const a = decodePNG(beforeFile);
const b = decodePNG(afterFile);
if (a.width !== b.width || a.height !== b.height) {
  console.error(`size mismatch: ${a.width}x${a.height} vs ${b.width}x${b.height}`);
  process.exit(1);
}

// a blank capture on either side makes the diff meaningless: a blank
// "before" means nothing rendered, a blank "after" means the session died
for (const [name, img] of [['before', a], ['after', b]]) {
  const distinct = countDistinctSample(img, 8);
  if (distinct < 3) {
    console.error(`FAIL: ${name} image is (near-)blank — ${distinct} distinct sampled colors`);
    process.exit(1);
  }
}

let changed = 0;
const px = a.width * a.height;
for (let i = 0; i < px; i++) {
  const o = i * a.bpp, p = i * b.bpp;
  if (Math.abs(a.data[o] - b.data[p]) > 8 ||
      Math.abs(a.data[o + 1] - b.data[p + 1]) > 8 ||
      Math.abs(a.data[o + 2] - b.data[p + 2]) > 8) {
    changed++;
  }
}

console.log(`png-diff: ${changed} of ${px} pixels changed (bounds [${MIN_CHANGED}, 25%]); both images non-blank`);
if (changed < MIN_CHANGED) {
  console.error('FAIL: typed marker did not visibly change the screen');
  process.exit(1);
}
if (changed > px * 0.25) {
  console.error('FAIL: near-full-frame change — an error/blank state was captured, not the typed marker');
  process.exit(1);
}
console.log('png-diff: PASS');
