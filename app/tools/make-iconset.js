// Writes build/Echo.iconset/ (the macOS iconset layout). The build script
// then runs `iconutil -c icns` on it — that tool is macOS-only, which is why
// this generator only emits PNGs.
'use strict';

const fs = require('fs');
const path = require('path');
const { iconPng } = require('../lib/icon');

const out = path.join(__dirname, '..', 'build', 'Echo.iconset');
fs.mkdirSync(out, { recursive: true });

// name -> pixel size, per Apple's iconset convention
const SIZES = {
  'icon_16x16.png': 16, 'icon_16x16@2x.png': 32,
  'icon_32x32.png': 32, 'icon_32x32@2x.png': 64,
  'icon_128x128.png': 128, 'icon_128x128@2x.png': 256,
  'icon_256x256.png': 256, 'icon_256x256@2x.png': 512,
  'icon_512x512.png': 512, 'icon_512x512@2x.png': 1024,
};

for (const [name, size] of Object.entries(SIZES)) {
  fs.writeFileSync(path.join(out, name), iconPng(size));
}
console.log(`[icon] wrote ${Object.keys(SIZES).length} PNGs to ${out}`);
