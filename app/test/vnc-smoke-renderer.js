// Harness driver: open the real echoVnc item, then type a marker through it.
// The xterm on the Xvnc display already has focus, and tty echo renders the
// keystrokes — pixels must change between the two main-process captures.
'use strict';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const container = document.getElementById('screen');
  await window.echoVnc.open(container);
  if (!window.echoVnc.connected) {
    throw new Error('open() resolved but echoVnc.connected is false');
  }
  await window.smoke.phase('connected'); // main settles + captures "before"

  const marker = 'ECHO VNC E2E MARKER';
  for (const ch of marker) {
    window.echoVnc.sendKey(ch.charCodeAt(0)); // ASCII keysyms == char codes
    await sleep(40);
  }
  window.echoVnc.sendKey(0xff0d); // Return

  if (!window.echoVnc.connected) {
    throw new Error('VNC session dropped while typing the marker');
  }
  await window.smoke.phase('typed'); // main settles + captures "after"
})().catch((err) => window.smoke.fail(String((err && err.stack) || err)));
