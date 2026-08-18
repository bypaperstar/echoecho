// Client for echoecho's Python viewer server (127.0.0.1:8765 by default).
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
const SAFE_EVENT_TYPES = new Set([
  'run', 'wake', 'state', 'session', 'audio', 'audio_processing', 'tool_call',
  'task', 'injection', 'user_text', 'assistant_text',
]);

class ViewerClient extends EventEmitter {
  constructor(base, options = {}) {
    super();
    this.base = base.replace(/\/$/, '');
    this.diag = options.diagnostics || null;
    this.fetchTimeoutMs = Number(options.fetchTimeoutMs) > 0 ?
      Number(options.fetchTimeoutMs) : 5000;
    // Python may spend up to 15s asking lume for a freshly cloned VM's VNC
    // endpoint. Keep routine polling bounded without pre-empting that valid
    // server-side operation.
    this.vncTimeoutMs = Number(options.vncTimeoutMs) > 0 ?
      Number(options.vncTimeoutMs) : Math.max(this.fetchTimeoutMs, 20000);
    this.lastTs = 0;
    this.lastRunId = null;
    this.lastSeq = 0;
    this.primed = false;
    this.stopped = false;
    this.req = null;
    this.retryTimer = null;
    this.streamAttempts = 0;
    this.streamConnectedAt = 0;
    this.streamGapStartedAt = 0;
    this.streamBytes = 0;
    this.streamFrames = 0;
    this.reloadInFlight = 0;
    this.reloadPending = false;
    this.reloadOverlapCount = 0;
    this.httpStats = new Map();
  }

  _log(level, event, fields) {
    if (this.diag && typeof this.diag[level] === 'function') this.diag[level](event, fields);
  }

  _error(err) {
    return this.diag && this.diag.errorFields ? this.diag.errorFields(err) : {
      name: String((err && err.name) || 'Error'),
      message: String((err && err.message) || err || 'unknown error'),
    };
  }

  _endpoint(raw) {
    const pathname = String(raw || '').split('?', 1)[0];
    if (pathname === '/transcript') return 'transcript';
    if (pathname === '/doc') return 'document';
    if (pathname === '/vnc-info') return 'vnc-info';
    return 'other';
  }

  _noteHttp(endpoint, result) {
    const now = Date.now();
    let s = this.httpStats.get(endpoint);
    if (!s) {
      s = { count: 0, failed: 0, totalMs: 0, maxMs: 0, consecutive: 0,
            failureStartedAt: 0, lastSummaryAt: now };
      this.httpStats.set(endpoint, s);
    }
    s.count++;
    s.totalMs += result.durationMs;
    s.maxMs = Math.max(s.maxMs, result.durationMs);
    if (result.error) {
      s.failed++;
      s.consecutive++;
      if (!s.failureStartedAt) s.failureStartedAt = now;
      // First failure is actionable; powers of two show persistence without a
      // three-second status poll flooding the log while the daemon is down.
      if (s.consecutive === 1 || (s.consecutive & (s.consecutive - 1)) === 0) {
        this._log('warn', 'viewer.http_failed', {
          endpoint, duration_ms: result.durationMs, status: result.status || null,
          consecutive: s.consecutive, error: this._error(result.error),
        });
      }
    } else {
      if (s.consecutive) {
        this._log('info', 'viewer.http_recovered', {
          endpoint, duration_ms: result.durationMs, status: result.status,
          failed_attempts: s.consecutive,
          downtime_ms: s.failureStartedAt ? now - s.failureStartedAt : null,
        });
      } else if (s.count === 1) {
        this._log('info', 'viewer.http_ready', {
          endpoint, duration_ms: result.durationMs, status: result.status,
        });
      }
      s.consecutive = 0;
      s.failureStartedAt = 0;
    }
    if (now - s.lastSummaryAt >= 60000) this._flushHttpStat(endpoint, s, now);
  }

  _flushHttpStat(endpoint, s, now = Date.now()) {
    if (!s || !s.count) return;
    this._log('info', 'viewer.http_summary', {
      endpoint, requests: s.count, failures: s.failed,
      avg_ms: Math.round(s.totalMs / s.count), max_ms: s.maxMs,
      consecutive_failures: s.consecutive,
    });
    s.count = 0; s.failed = 0; s.totalMs = 0; s.maxMs = 0; s.lastSummaryAt = now;
  }

  async _request(rawPath, responseType, headers) {
    const endpoint = this._endpoint(rawPath);
    const started = Date.now();
    const controller = new AbortController();
    const timeoutMs = endpoint === 'vnc-info' ? this.vncTimeoutMs : this.fetchTimeoutMs;
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    if (timer.unref) timer.unref();
    let status = null;
    try {
      const res = await fetch(this.base + rawPath, {
        ...(headers ? { headers } : {}), signal: controller.signal,
      });
      status = res.status;
      if (!res.ok) {
        const err = new Error(`${endpoint} request returned HTTP ${res.status}`);
        err.code = 'HTTP_STATUS';
        throw err;
      }
      const value = responseType === 'json' ? await res.json() : await res.text();
      this._noteHttp(endpoint, { durationMs: Date.now() - started, status });
      return value;
    } catch (err) {
      if (controller.signal.aborted) {
        err = new Error(`${endpoint} request timed out`);
        err.code = 'ETIMEDOUT';
      }
      this._noteHttp(endpoint, { durationMs: Date.now() - started, status, error: err });
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  async json(path, headers) {
    return this._request(path, 'json', headers);
  }

  async text(path) {
    return this._request(path, 'text');
  }

  transcript() {
    return this.json('/transcript');
  }

  doc(relpath) {
    return this.text('/doc?f=' + encodeURIComponent(relpath));
  }

  // /vnc-info serves credentials, so it alone requires the viewer token the
  // server writes at startup (env ECHOECHO_VIEWER_TOKEN_FILE or ~/.echoecho/viewer.token,
  // regenerated per run). Read fresh per call; missing file means the viewer
  // server isn't running.
  async vncInfo() {
    const file = process.env.ECHOECHO_VIEWER_TOKEN_FILE ||
      path.join(os.homedir(), '.echoecho', 'viewer.token');
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
    this.lastTs = 0;
    this.lastRunId = null;
    this.lastSeq = 0;
    this._log('info', 'viewer.stream_start', {});
    this._connect();
  }

  stop() {
    this.stopped = true;
    clearTimeout(this.retryTimer);
    this.retryTimer = null;
    if (this.req) this.req.destroy();
    this.req = null;
    if (this.streamConnectedAt) {
      this._log('info', 'viewer.stream_stop', {
        connected_ms: Date.now() - this.streamConnectedAt,
        bytes: this.streamBytes, frames: this.streamFrames,
      });
    }
    for (const [endpoint, stat] of this.httpStats) this._flushHttpStat(endpoint, stat);
  }

  _connect() {
    if (this.stopped) return;
    this.retryTimer = null;
    this.streamAttempts++;
    const attempt = this.streamAttempts;
    if (attempt === 1 || (attempt & (attempt - 1)) === 0) {
      this._log('info', 'viewer.stream_connecting', { attempt });
    }
    const url = new URL(this.base + '/events');
    let responseStarted = false;
    const req = http.get(
      { host: url.hostname, port: url.port, path: url.pathname, headers: { Accept: 'text/event-stream' } },
      (res) => {
        responseStarted = true;
        if (res.statusCode !== 200) {
          res.resume();
          this._retry('http-status', null, res.statusCode);
          return;
        }
        const gapMs = this.streamGapStartedAt ? Date.now() - this.streamGapStartedAt : 0;
        const priorAttempts = this.streamAttempts;
        this.streamAttempts = 0;
        this.streamGapStartedAt = 0;
        this.streamConnectedAt = Date.now();
        this.streamBytes = 0;
        this.streamFrames = 0;
        this._log('info', 'viewer.stream_connected', {
          attempt: priorAttempts, gap_ms: gapMs, status: res.statusCode,
        });
        this.emit('connected');
        let buf = '';
        res.setEncoding('utf8');
        res.on('data', (chunk) => {
          this.streamBytes += Buffer.byteLength(chunk);
          buf += chunk;
          let idx;
          while ((idx = buf.indexOf('\n\n')) !== -1) {
            const frame = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            this.streamFrames++;
            if (/^event: reload$/m.test(frame)) this._onReload();
          }
        });
        res.on('end', () => this._retry('stream-end'));
        res.on('error', (err) => this._retry('stream-error', err));
      }
    );
    const connectTimer = setTimeout(() => {
      if (!responseStarted) {
        const err = new Error('viewer event stream connection timed out');
        err.code = 'ETIMEDOUT';
        req.destroy(err);
      }
    }, this.fetchTimeoutMs);
    if (connectTimer.unref) connectTimer.unref();
    req.once('response', () => clearTimeout(connectTimer));
    req.on('error', (err) => {
      clearTimeout(connectTimer);
      this._retry(err && err.code === 'ETIMEDOUT' ? 'connect-timeout' : 'connect-error', err);
    });
    this.req = req;
  }

  _retry(reason, err, status) {
    // A dropped connection fires several close callbacks; dedup so one drop
    // schedules exactly one reconnect.
    if (this.stopped || this.retryTimer) return;
    this.req = null;
    const now = Date.now();
    const connectedMs = this.streamConnectedAt ? now - this.streamConnectedAt : 0;
    if (!this.streamGapStartedAt) this.streamGapStartedAt = now;
    this.streamConnectedAt = 0;
    const attempt = this.streamAttempts;
    if (connectedMs || attempt <= 1 || (attempt & (attempt - 1)) === 0) {
      this._log('warn', 'viewer.stream_disconnected', {
        reason, status: status || null, connected_ms: connectedMs,
        attempt, bytes: this.streamBytes, frames: this.streamFrames,
        error: err ? this._error(err) : null, retry_ms: 2000,
      });
    }
    this.emit('disconnected');
    this.retryTimer = setTimeout(() => this._connect(), 2000);
    if (this.retryTimer.unref) this.retryTimer.unref();
  }

  _onReload() {
    if (this.reloadInFlight) {
      this.reloadPending = true;
      this.reloadOverlapCount++;
      return;
    }
    this.reloadInFlight = 1;
    this._drainReloads().catch((err) => {
      this._log('warn', 'viewer.reload_processing_failed', {
        error: this._error(err),
      });
    });
  }

  async _drainReloads() {
    let reruns = 0;
    try {
      do {
        this.reloadPending = false;
        await this._reloadOnce();
        if (this.reloadPending) reruns++;
      } while (this.reloadPending && !this.stopped);
    } finally {
      this.reloadInFlight = 0;
      if (this.reloadOverlapCount) {
        this._log('info', 'viewer.reload_coalesced', {
          overlapping_triggers: this.reloadOverlapCount, reruns,
        });
        this.reloadOverlapCount = 0;
      }
    }
  }

  _sequenceCursor(event) {
    if (!event || typeof event.run_id !== 'string' || !event.run_id ||
        !Number.isSafeInteger(event.seq) || event.seq < 0) return null;
    return { runId: event.run_id, seq: event.seq };
  }

  _latestSequenceRun(events) {
    for (let index = events.length - 1; index >= 0; index--) {
      const cursor = this._sequenceCursor(events[index]);
      if (cursor) return cursor.runId;
    }
    return null;
  }

  _primeTranscriptWatermark(events) {
    this.lastRunId = this._latestSequenceRun(events);
    this.lastSeq = 0;
    this.lastTs = 0;
    for (const event of events) {
      const cursor = this._sequenceCursor(event);
      if (cursor && cursor.runId === this.lastRunId) {
        this.lastSeq = Math.max(this.lastSeq, cursor.seq);
      } else if (!cursor && event && Number.isFinite(event.ts)) {
        // Old daemons have no run/sequence cursor. Keep their timestamp
        // watermark independent from sequenced records so a clock rollback
        // cannot suppress a later record that does have a sequence number.
        this.lastTs = Math.max(this.lastTs, event.ts);
      }
    }
  }

  _eventsAfterTranscriptWatermark(events) {
    const activeRunId = this._latestSequenceRun(events);
    const adoptingSequence = activeRunId !== null && this.lastRunId === null;
    const runChanged = activeRunId !== null && this.lastRunId !== null &&
      activeRunId !== this.lastRunId;
    let start = 0;

    if (adoptingSequence || runChanged) {
      // A Python restart truncates the feed and starts sequence numbers over.
      // If a partial/mixed read still contains prior-run records, begin at the
      // first record from the final run rather than replaying that stale tail.
      let lastForeign = -1;
      for (let index = 0; index < events.length; index++) {
        const cursor = this._sequenceCursor(events[index]);
        if (cursor && cursor.runId !== activeRunId) lastForeign = index;
      }
      const firstCurrent = events.findIndex((event, index) => {
        if (index <= lastForeign) return false;
        const cursor = this._sequenceCursor(event);
        return cursor && cursor.runId === activeRunId;
      });
      if (firstCurrent !== -1) start = firstCurrent;
      this.lastRunId = activeRunId;
      this.lastSeq = 0;
      this.lastTs = 0;
    }

    const priorSeq = this.lastSeq;
    const priorTs = this.lastTs;
    const fresh = [];
    for (let index = start; index < events.length; index++) {
      const event = events[index];
      const cursor = this._sequenceCursor(event);
      if (cursor) {
        if (cursor.runId !== activeRunId) continue;
        if (cursor.seq > priorSeq) fresh.push(event);
        this.lastSeq = Math.max(this.lastSeq, cursor.seq);
      } else if (event && Number.isFinite(event.ts)) {
        if (event.ts > priorTs) fresh.push(event);
        this.lastTs = Math.max(this.lastTs, event.ts);
      }
    }
    return { fresh, runChanged, sequenced: activeRunId !== null };
  }

  async _reloadOnce() {
    const started = Date.now();
    let events;
    try {
      events = await this.transcript();
    } catch (err) {
      this._log('warn', 'viewer.reload_failed', {
        duration_ms: Date.now() - started, error: this._error(err),
      });
      return; // transient; the next reload retries
    }
    if (!Array.isArray(events)) {
      this._log('warn', 'viewer.reload_invalid', { response_type: typeof events });
      return;
    }
    if (!this.primed) {
      this.primed = true;
      this._primeTranscriptWatermark(events);
      this._log('info', 'viewer.reload_primed', {
        duration_ms: Date.now() - started, events: events.length,
        watermark: this.lastRunId === null ? 'timestamp' : 'sequence',
      });
      return;
    }
    const { fresh, runChanged, sequenced } =
      this._eventsAfterTranscriptWatermark(events);
    if (!fresh.length) return;
    const types = {};
    for (const e of fresh) {
      const type = SAFE_EVENT_TYPES.has(e.type) ? e.type : 'unknown';
      types[type] = (types[type] || 0) + 1;
    }
    this._log('info', 'viewer.reload_events', {
      duration_ms: Date.now() - started, fresh: fresh.length, types,
      watermark: sequenced ? 'sequence' : 'timestamp',
      run_changed: runChanged,
    });
    this.emit('events', fresh);
    // Only the voice daemon emits type='wake'; text/script modes emit just the
    // FSM transition (state IDLE->ACTIVE with reason='wake').
    const isWake = (e) => e.type === 'wake' ||
      (e.type === 'state' && e.to === 'ACTIVE' && e.reason === 'wake');
    if (fresh.some(isWake)) this.emit('wake');
  }
}

module.exports = { ViewerClient };
