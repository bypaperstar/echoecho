// Procedural app icon — the blob as a dock/app icon, drawn per-pixel and
// encoded to PNG with zero dependencies (zlib is Node built-in; the PNG
// container is ~40 lines). Same no-assets rule as everything else.
'use strict';

const zlib = require('zlib');

// ---- minimal PNG encoder (8-bit RGBA, no interlace) -----------------------
const CRC_TABLE = (() => {
  const t = new Int32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c;
  }
  return t;
})();

function crc32(buf) {
  let c = -1;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ -1) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}

function pngEncode(width, height, rgba) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8;  // bit depth
  ihdr[9] = 6;  // color type RGBA
  // scanlines, each prefixed with filter byte 0
  const raw = Buffer.alloc(height * (1 + width * 4));
  for (let y = 0; y < height; y++) {
    raw[y * (1 + width * 4)] = 0;
    rgba.copy(raw, y * (1 + width * 4) + 1, y * width * 4, (y + 1) * width * 4);
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

// ---- the icon itself -------------------------------------------------------
// A glossy black blob: wobbled silhouette, vertical body gradient, cool rim
// light at the edge, one soft specular. Matches the scene's organism.
function drawIcon(size) {
  const rgba = Buffer.alloc(size * size * 4);
  const cx = size / 2, cy = size / 2;
  const base = size * 0.355;
  const sx = cx - size * 0.115, sy = cy - size * 0.155; // specular center
  const sr = size * 0.16;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const dx = x - cx, dy = y - cy;
      const ang = Math.atan2(dy, dx);
      const r = Math.sqrt(dx * dx + dy * dy);
      const edge = base * (1 + Math.sin(ang * 3 + 0.8) * 0.085 + Math.sin(ang * 5 - 1.4) * 0.05);
      const d = edge - r; // >0 inside, in px
      const a = Math.max(0, Math.min(1, d + 0.5));
      const i = (y * size + x) * 4;
      if (a <= 0) continue;
      // body: slightly lighter up top so it reads as a volume
      const grad = Math.max(0, Math.min(1, (y / size - 0.25) / 0.6));
      let R = 22 - 14 * grad, G = 22 - 14 * grad, B = 30 - 18 * grad;
      // cool rim light hugging the silhouette
      const rim = Math.max(0, 1 - d / (size * 0.028));
      R += 96 * rim * 0.55;
      G += 128 * rim * 0.55;
      B += 200 * rim * 0.55;
      // specular
      const ds = Math.hypot(x - sx, y - sy);
      const spec = Math.max(0, 1 - ds / sr);
      const s = spec * spec * 0.65;
      R += 225 * s;
      G += 230 * s;
      B += 245 * s;
      rgba[i] = Math.min(255, Math.round(R));
      rgba[i + 1] = Math.min(255, Math.round(G));
      rgba[i + 2] = Math.min(255, Math.round(B));
      rgba[i + 3] = Math.round(a * 255);
    }
  }
  return rgba;
}

function iconPng(size) {
  return pngEncode(size, size, drawIcon(size));
}

module.exports = { iconPng, pngEncode, drawIcon };
