// PLACEHOLDER blob — a breathing black disc so the shell smoke-tests
// before the real procedural blob lands. Exposes the contract the scene
// integration relies on: window.echoBlob = { setReveal(t, anchor), tick(dt) }.
'use strict';

(() => {
  const canvas = document.getElementById('blob');
  const ctx = canvas.getContext('2d');
  let reveal = 0;
  let anchor = { x: 0, y: 0 };
  let t0 = performance.now();

  function resize() {
    canvas.width = innerWidth * devicePixelRatio;
    canvas.height = innerHeight * devicePixelRatio;
  }
  addEventListener('resize', resize);
  resize();

  function frame(now) {
    const t = (now - t0) / 1000;
    ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
    ctx.clearRect(0, 0, innerWidth, innerHeight);
    const cx = anchor.x + (innerWidth / 2 - anchor.x) * reveal;
    const cy = anchor.y + (innerHeight / 2 - anchor.y) * reveal;
    const r = (24 + 116 * reveal) * (1 + 0.04 * Math.sin(t * 1.7));
    ctx.beginPath();
    ctx.arc(cx, cy, Math.max(1, r), 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(8, 8, 12, 0.97)';
    ctx.fill();
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  window.echoBlob = {
    setReveal(t, a) {
      reveal = t;
      if (a) anchor = a;
    },
    // Screen-space point on the blob's edge nearest `toward`, for item spawns.
    edgePoint() {
      return { x: innerWidth / 2, y: innerHeight / 2 };
    },
  };
})();
