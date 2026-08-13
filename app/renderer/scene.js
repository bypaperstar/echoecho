// PLACEHOLDER scene — drives the reveal parameter and wires lifecycle IPC.
// The real scene (items emerging from the blob, transcript wisps, Echo's Mac
// screen) replaces this; the lifecycle contract below must survive.
'use strict';

(() => {
  let reveal = 0;
  let target = 0;
  let anchor = { x: innerWidth - 40, y: 0 };
  let hideWhenDone = false;

  function tick() {
    const speed = 0.045;
    if (Math.abs(target - reveal) > 0.001) {
      reveal += Math.sign(target - reveal) * Math.min(speed, Math.abs(target - reveal));
      window.echoBlob.setReveal(easeOutCubic(reveal), anchor);
    } else if (hideWhenDone && target === 0) {
      hideWhenDone = false;
      window.orb.hidden();
    }
    requestAnimationFrame(tick);
  }
  const easeOutCubic = (t) => 1 - Math.pow(1 - t, 3);
  requestAnimationFrame(tick);

  window.orb.onReveal(({ anchor: a }) => {
    if (a) anchor = a;
    hideWhenDone = false;
    target = 1;
  });
  window.orb.onDismiss(() => {
    hideWhenDone = true;
    target = 0;
  });
  window.orb.onBlur(() => {
    // Placeholder: dismiss on blur like a popover. The real scene keeps the
    // window when Echo's Mac has the keyboard.
    window.orb.dismissRequest();
  });
  addEventListener('keydown', (e) => {
    if (e.key === 'Escape') window.orb.dismissRequest();
  });

  window.orb.onEvents((evts) => {
    console.log('[scene] events', evts.map((e) => e.type).join(','));
  });
})();
