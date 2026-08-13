// Procedural tray icon — no assets. A small blobby orb drawn into raw RGBA:
// a circle whose radius is perturbed by a few sine lobes, anti-aliased by a
// smooth distance falloff. On macOS it's marked as a template image so the
// menu bar recolors it for light/dark automatically.
'use strict';

const { nativeImage } = require('electron');

function blobIconBuffer(size, phase) {
  const buf = Buffer.alloc(size * size * 4);
  const cx = size / 2, cy = size / 2;
  const base = size * 0.34;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const dx = x - cx, dy = y - cy;
      const ang = Math.atan2(dy, dx);
      const r = Math.sqrt(dx * dx + dy * dy);
      const wobble =
        Math.sin(ang * 3 + phase) * 0.10 +
        Math.sin(ang * 5 - phase * 1.7) * 0.06;
      const edge = base * (1 + wobble);
      // 1px-wide smooth edge for anti-aliasing
      const a = Math.max(0, Math.min(1, (edge - r) + 0.5));
      const i = (y * size + x) * 4;
      buf[i] = 0;       // B (BGRA on most platforms; black either way)
      buf[i + 1] = 0;   // G
      buf[i + 2] = 0;   // R
      buf[i + 3] = Math.round(a * 255);
    }
  }
  return buf;
}

function trayIcon() {
  const size = 44; // 22pt @2x
  const img = nativeImage.createFromBuffer(blobIconBuffer(size, 0.8), {
    width: size,
    height: size,
    scaleFactor: 2.0,
  });
  if (process.platform === 'darwin') img.setTemplateImage(true);
  return img;
}

module.exports = { trayIcon, blobIconBuffer };
