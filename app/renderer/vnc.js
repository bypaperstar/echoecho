// PLACEHOLDER — the VNC item (noVNC RFB wiring) lands here.
// Contract: window.echoVnc = { open(container) -> Promise<void>, close() }.
'use strict';

window.echoVnc = {
  async open(container) {
    container.textContent = "Echo's Mac is asleep (VNC not wired yet)";
  },
  close() {},
};
