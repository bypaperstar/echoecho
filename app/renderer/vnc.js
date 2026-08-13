// Echo's Mac item: a live, interactive noVNC canvas over the main process's
// WS<->TCP bridge (vnc-proxy.js, reached via window.orb.vncConnect).
//
// Contract: window.echoVnc = { open(container, opts) -> Promise, close(),
// setViewOnly(bool), sendKey(keysym, code, down), get connected() }.
//
// Classic script (index.html loads it with <script src>), so noVNC — pure
// ESM — is dynamic-import()ed on first open; no top-level await here. The
// import specifier resolves against THIS file's URL, so it works from any
// document (the scene and the e2e harness both load it unmodified).
'use strict';

(() => {
  // 1.7.x ships ESM under core/; 1.5.x-era builds compiled to lib/.
  const RFB_PATHS = [
    '../node_modules/@novnc/novnc/core/rfb.js',
    '../node_modules/@novnc/novnc/lib/rfb.js',
  ];
  let RFBClass = null;

  async function loadRFB() {
    if (RFBClass) return RFBClass;
    const failures = [];
    // Retry: right after Electron startup the file:// module fetch can fail
    // transiently, and a failed fetch poisons the module map for that exact
    // URL — the query string makes each retry a fresh entry.
    for (let attempt = 0; attempt < 3; attempt++) {
      if (attempt) await new Promise((r) => setTimeout(r, 300 * attempt));
      for (const path of RFB_PATHS) {
        const spec = attempt ? `${path}?attempt=${attempt}` : path;
        try {
          RFBClass = (await import(spec)).default;
          return RFBClass;
        } catch (err) {
          failures.push(`${spec}: ${(err && err.message) || err}`);
        }
      }
    }
    throw new Error(`noVNC failed to load — ${failures.join('; ')}`);
  }

  let rfb = null;
  let root = null;
  let screenEl = null;
  let statusEl = null;
  let connected = false;
  let viewOnly = false;

  function setStatus(text) {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.style.display = text ? 'flex' : 'none';
  }

  function buildDom(container) {
    root = document.createElement('div');
    root.className = 'vnc-item';
    root.style.cssText =
      'position:relative;width:100%;height:100%;overflow:hidden;background:#000;';
    screenEl = document.createElement('div');
    screenEl.className = 'vnc-screen';
    screenEl.style.cssText = 'position:absolute;inset:0;';
    statusEl = document.createElement('div');
    statusEl.className = 'vnc-status';
    statusEl.style.cssText =
      'position:absolute;inset:0;z-index:1;display:flex;align-items:center;' +
      'justify-content:center;text-align:center;padding:16px;color:#ddd;' +
      'font:14px system-ui, sans-serif;background:rgba(0,0,0,.55);';
    root.append(screenEl, statusEl);
    container.appendChild(root);
  }

  function teardown() {
    if (rfb) {
      try { rfb.disconnect(); } catch {}
      rfb = null;
    }
    connected = false;
    if (root && root.parentNode) root.parentNode.removeChild(root);
    root = screenEl = statusEl = null;
  }

  window.echoVnc = {
    // Resolves once the RFB session is up; rejects (with a state message
    // already rendered into the container) when the Mac is unreachable.
    async open(container, opts = {}) {
      teardown();
      buildDom(container);
      viewOnly = !!opts.viewOnly;
      setStatus("Waking Echo's Mac…");

      let info;
      try {
        info = await window.orb.vncConnect();
      } catch (err) {
        setStatus("Echo's Mac is asleep");
        throw err;
      }
      let RFB;
      try {
        RFB = await loadRFB();
      } catch (err) {
        setStatus("Echo's Mac view failed to load");
        throw err;
      }

      return new Promise((resolve, reject) => {
        let settled = false;
        rfb = new RFB(screenEl, info.wsUrl, {
          credentials: { password: info.password || '' },
        });
        // Fit the remote desktop to the item; RFB's own ResizeObserver keeps
        // the scale fresh as the scene resizes the container.
        rfb.scaleViewport = true;
        rfb.clipViewport = false;
        // Interactive by default: it's Echo's Mac, blast radius is the VM.
        rfb.viewOnly = viewOnly;

        rfb.addEventListener('connect', () => {
          connected = true;
          setStatus('');
          try { rfb.focus(); } catch {}
          if (!settled) { settled = true; resolve(); }
        });
        rfb.addEventListener('disconnect', (e) => {
          connected = false;
          const clean = !!(e.detail && e.detail.clean);
          setStatus(clean ? "Echo's Mac closed the session" : "Echo's Mac is asleep");
          if (!settled) {
            settled = true;
            reject(new Error('VNC disconnected before the session came up'));
          }
        });
        rfb.addEventListener('credentialsrequired', () => {
          if (info.password) rfb.sendCredentials({ password: info.password });
          else setStatus("Echo's Mac wants a password Echo doesn't have");
        });
        rfb.addEventListener('securityfailure', (e) => {
          const reason = e.detail && e.detail.reason;
          setStatus("Echo's Mac refused the connection" + (reason ? `: ${reason}` : ''));
        });
      });
    },

    close() {
      teardown();
      if (window.orb && window.orb.vncDisconnect) window.orb.vncDisconnect();
    },

    setViewOnly(v) {
      viewOnly = !!v;
      if (rfb) rfb.viewOnly = viewOnly;
    },

    // down omitted -> noVNC sends press+release (used by the scene for
    // shortcuts and by the e2e harness to prove input reaches the desktop)
    sendKey(keysym, code, down) {
      if (rfb) rfb.sendKey(keysym, code, down);
    },

    get connected() {
      return connected;
    },
  };
})();
