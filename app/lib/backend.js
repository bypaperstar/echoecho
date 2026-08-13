// Client for Echo's Python viewer server (127.0.0.1:8765 by default).
//
// All HTTP happens here in the main process on purpose: the viewer server
// deliberately sends no CORS headers (so a random website in the user's
// browser can't read the transcript), which also means a renderer on file://
// couldn't fetch it. Main-process Node has no such restriction; results are
// forwarded to the renderer over IPC.
'use strict';

const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');
const { EventEmitter } = require('events');

class ViewerClient extends EventEmitter {
  constructor(base) {
    super();
    this.base = base.replace(/\/$/, '');
    this.lastTs = 0;
    this.primed = false;
    this.stopped = false;
    this.req = null;
    this.retryTimer = null;
  }

  async json(path, headers) {
    const res = await fetch(this.base + path, headers ? { headers } : undefined);
    if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
    return res.json();
  }

  async text(path) {
    const res = await fetch(this.base + path);
    if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
    return res.text();
  }

  transcript() {
    return this.json('/transcript');
  }

  doc(relpath) {
    return this.text('/doc?f=' + encodeURIComponent(relpath));
  }

  // /vnc-info serves credentials, so it alone requires the viewer token the
  // server writes at startup (env ECHO_VIEWER_TOKEN_FILE or ~/.echo/viewer.token,
  // regenerated per run). Read fresh per call; missing file means the viewer
  // server isn't running.
  async vncInfo() {
    const file = process.env.ECHO_VIEWER_TOKEN_FILE ||
      path.join(os.homedir(), '.echo', 'viewer.token');
    let token;
    try {
      token = fs.readFileSync(file, 'utf8').trim();
    } catch {
      throw new Error(`viewer token file not found (${file})`);
    }
    return this.json('/vnc-info', { Authorization: `Bearer ${token}` });
  }

  // Long-lived SSE subscription to /events with reconnect. Each 'reload'
  // triggers a /transcript refetch; the first fetch only primes the watermark
  // (nothing emitted), so only events after launch are emitted as
  // ('events', [..]) and any wake event additionally as ('wake').
  start() {
    this.stopped = false;
    this.primed = false;
    this._connect();
  }

  stop() {
    this.stopped = true;
    clearTimeout(this.retryTimer);
    this.retryTimer = null;
    if (this.req) this.req.destroy();
  }

  _connect() {
    if (this.stopped) return;
    this.retryTimer = null;
    const url = new URL(this.base + '/events');
    const req = http.get(
      { host: url.hostname, port: url.port, path: url.pathname, headers: { Accept: 'text/event-stream' } },
      (res) => {
        if (res.statusCode !== 200) {
          res.resume();
          this._retry();
          return;
        }
        this.emit('connected');
        let buf = '';
        res.setEncoding('utf8');
        res.on('data', (chunk) => {
          buf += chunk;
          let idx;
          while ((idx = buf.indexOf('\n\n')) !== -1) {
            const frame = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            if (/^event: reload$/m.test(frame)) this._onReload();
          }
        });
        res.on('end', () => this._retry());
        res.on('error', () => this._retry());
      }
    );
    req.on('error', () => this._retry());
    this.req = req;
  }

  _retry() {
    // A dropped connection fires several close callbacks; dedup so one drop
    // schedules exactly one reconnect.
    if (this.stopped || this.retryTimer) return;
    this.emit('disconnected');
    this.retryTimer = setTimeout(() => this._connect(), 2000);
  }

  async _onReload() {
    let events;
    try {
      events = await this.transcript();
    } catch {
      return; // transient; the next reload retries
    }
    if (!this.primed) {
      this.primed = true;
      this.lastTs = events.reduce(
        (m, e) => (typeof e.ts === 'number' && e.ts > m ? e.ts : m), 0);
      return;
    }
    const fresh = events.filter((e) => typeof e.ts === 'number' && e.ts > this.lastTs);
    if (!fresh.length) return;
    this.lastTs = fresh[fresh.length - 1].ts;
    this.emit('events', fresh);
    // Only the voice daemon emits type='wake'; text/script modes emit just the
    // FSM transition (state IDLE->ACTIVE with reason='wake').
    const isWake = (e) => e.type === 'wake' ||
      (e.type === 'state' && e.to === 'ACTIVE' && e.reason === 'wake');
    if (fresh.some(isWake)) this.emit('wake');
  }
}

module.exports = { ViewerClient };
