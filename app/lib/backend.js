// Client for Echo's Python viewer server (127.0.0.1:8765 by default).
//
// All HTTP happens here in the main process on purpose: the viewer server
// deliberately sends no CORS headers (so a random website in the user's
// browser can't read the transcript), which also means a renderer on file://
// couldn't fetch it. Main-process Node has no such restriction; results are
// forwarded to the renderer over IPC.
'use strict';

const http = require('http');
const { EventEmitter } = require('events');

class ViewerClient extends EventEmitter {
  constructor(base) {
    super();
    this.base = base.replace(/\/$/, '');
    this.lastTs = 0;
    this.stopped = false;
    this.req = null;
  }

  async json(path) {
    const res = await fetch(this.base + path);
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

  vncInfo() {
    return this.json('/vnc-info');
  }

  // Long-lived SSE subscription to /events with reconnect. Each 'reload'
  // triggers a /transcript refetch; events newer than the watermark are
  // emitted as ('events', [..]) and any wake event additionally as ('wake').
  start() {
    this.stopped = false;
    this._connect();
  }

  stop() {
    this.stopped = true;
    if (this.req) this.req.destroy();
  }

  _connect() {
    if (this.stopped) return;
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
    if (this.stopped) return;
    this.emit('disconnected');
    setTimeout(() => this._connect(), 2000);
  }

  async _onReload() {
    let events;
    try {
      events = await this.transcript();
    } catch {
      return; // transient; the next reload retries
    }
    const fresh = events.filter((e) => typeof e.ts === 'number' && e.ts > this.lastTs);
    if (!fresh.length) return;
    this.lastTs = fresh[fresh.length - 1].ts;
    this.emit('events', fresh);
    if (fresh.some((e) => e.type === 'wake')) this.emit('wake');
  }
}

module.exports = { ViewerClient };
