'use strict';

// Local, privacy-preserving diagnostics for the Electron app.  Events are
// deliberately metadata-only: this module recursively redacts suspicious
// fields and string patterns before appending bounded JSON lines.
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');

const DEFAULT_MAX_BYTES = 5 * 1024 * 1024;
const DEFAULT_MAX_PARTS = 10;
const DEFAULT_MAX_RUNS = 20;
const DEFAULT_MAX_AGE_DAYS = 14;
const MAX_DEPTH = 5;
const MAX_KEYS = 48;
const MAX_ARRAY = 32;
const MAX_STRING = 600;
const MAX_CLASSIFICATION_KEY = 1024;
const MAX_RETENTION_ENTRIES = 50000;
const MAX_RETENTION_FILES = 25000;
const MAX_RETENTION_RUNS = 1000;
const MAX_FINGERPRINT_PREFIX_CHARS = 4096;

// Only names produced by this module are eligible for retention. Diagnostics
// roots may be operator-selected or shared, so a broad `orb-run-*` match could
// otherwise delete an unrelated file which merely resembles an Orb log.
const ORB_RUN_FILE_RE = /^(orb-run-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z-[1-9]\d*-[0-9a-f]{8})(?:\.([1-9]\d*))?\.jsonl$/;

const SENSITIVE_KEY = /(?:^|[_-])(?:auth(?:orization)?|password|passwd|secret|token|api[_-]?key|cookie|credential|transcript|instructions?|prompt|content|body|request[_-]?body|response[_-]?body|output|stdout|stderr|text|audio|pcm|args?|arguments?|query|markdown|document|doc|artifact|filename|filepath|path|url|host|stack(?:trace)?)(?:$|[_-])/i;
const SAFE_METADATA_SUFFIXES = new Set([
  'available', 'bytes', 'chars', 'count', 'counts', 'depth', 'digest',
  'duration_ms', 'enabled', 'encoding', 'fingerprint', 'format', 'frames',
  'hash', 'high_water', 'latency_ms', 'len', 'length', 'ms', 'peak',
  'percent', 'present', 'rate', 'ratio', 'returncode', 's', 'seconds', 'size',
  'status', 'tokens', 'type', 'version',
]);
const SAFE_EVENT = /^[a-z0-9][a-z0-9_.-]{0,79}$/;
const DISABLED_VALUES = new Set(['0', 'false', 'no', 'off']);

function normalizedKey(value) {
  return safeString(value)
    .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1_$2')
    .replace(/[^A-Za-z0-9]+/g, '_')
    .toLowerCase();
}

function safeMetadataKey(key, value) {
  const normalized = normalizedKey(key);
  for (const suffix of SAFE_METADATA_SUFFIXES) {
    if (normalized !== suffix && !normalized.endsWith(`_${suffix}`)) continue;
    if (value === null || typeof value === 'boolean' ||
        (typeof value === 'number' && Number.isFinite(value))) return true;
    if (['fingerprint', 'hash', 'digest'].includes(suffix) &&
        typeof value === 'string' && /^[0-9a-f]{8,128}$/i.test(value)) return true;
    return false;
  }
  return false;
}

function safeString(value, fallback = '[unprintable]') {
  try { return String(value); } catch { return fallback; }
}

function diagnosticsEnabled(raw = process.env.ECHOECHO_DIAGNOSTICS) {
  return !DISABLED_VALUES.has(String(raw === undefined ? '' : raw).trim().toLowerCase());
}

function boundedInt(value, fallback, min, max) {
  const n = Number(value);
  return Number.isFinite(n) ? Math.max(min, Math.min(max, Math.trunc(n))) : fallback;
}

function redactString(value) {
  let out = safeString(value);
  out = out.replace(/\bBearer\s+[^\s,;]+/gi, 'Bearer [REDACTED]');
  out = out.replace(/\bsk-[A-Za-z0-9_-]{8,}\b/g, '[REDACTED_API_KEY]');
  out = out.replace(/\b(?:token|password|passwd|secret|api[_-]?key)=([^\s&,;]+)/gi,
    (_m, _v) => _m.split('=')[0] + '=[REDACTED]');
  out = out.replace(/\b(?:vnc|wss?|https?|file):\/\/[^\s)'"<>]+/gi, '[REDACTED_URL]');
  out = out.replace(/(?:^|[\s("'])(?:\/[A-Za-z0-9._~%+@-]+){2,}(?::\d+(?::\d+)?)?/g,
    (m) => `${m[0].trim() ? '' : m[0]}[REDACTED_PATH]`);
  out = out.replace(/\b[A-Za-z]:\\(?:[^\\\s]+\\)+[^\\\s]*/g, '[REDACTED_PATH]');
  out = out.replace(/\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b/g, '[REDACTED_HOST]');
  if (out.length > MAX_STRING) out = out.slice(0, MAX_STRING) + '…[truncated]';
  return out;
}

function redactStack(value) {
  // Error messages may span multiple stack lines. Keep only V8 call frames;
  // dropping headlines/non-frame lines prevents request bodies or transcripts
  // embedded by SDK errors from becoming a second content channel.
  const frames = safeString(value).split(/\r?\n/)
    .filter((line) => /^\s*at\s+/.test(line))
    .slice(0, 10);
  return frames.length ? redactString(frames.join('\n')) : '[REDACTED_STACK]';
}

function sanitize(value, depth = 0, seen = new Set(), budget = { remaining: 512 }) {
  if (budget.remaining-- <= 0) return '[MAX_FIELDS]';
  if (value === null || value === undefined) return value === undefined ? null : value;
  if (typeof value === 'string') return redactString(value);
  if (typeof value === 'number') return Number.isFinite(value) ? value : String(value);
  if (typeof value === 'boolean') return value;
  if (typeof value === 'bigint') return String(value);
  if (typeof value === 'function' || typeof value === 'symbol') return `[${typeof value}]`;
  if (depth >= MAX_DEPTH) return '[MAX_DEPTH]';
  if (seen.has(value)) return '[CIRCULAR]';
  seen.add(value);
  try {
    if (value instanceof Error) return sanitize(errorFields(value), depth + 1, seen, budget);
    if (Array.isArray(value)) {
      const out = value.slice(0, MAX_ARRAY).map((v) => sanitize(v, depth + 1, seen, budget));
      if (value.length > MAX_ARRAY) out.push(`[${value.length - MAX_ARRAY} more]`);
      return out;
    }
    const out = {};
    let count = 0;
    let truncated = false;
    try {
      // Avoid Object.entries/Object.keys: both materialize every property
      // before slicing and let a huge diagnostic object cause a large
      // allocation. Read at most MAX_KEYS own fields, one at a time.
      for (const rawKey in value) {
        if (!Object.prototype.hasOwnProperty.call(value, rawKey)) continue;
        if (count >= MAX_KEYS) { truncated = true; break; }
        count++;
        let child;
        try { child = value[rawKey]; } catch { child = '[UNREADABLE]'; }
        const rawKeyString = safeString(rawKey);
        // Privacy classification must see suffixes that the display-key bound
        // omits. Exceptionally large keys fail closed instead of being run
        // through several attacker-controlled regular expressions.
        const classificationKey = rawKeyString.length > MAX_CLASSIFICATION_KEY
          ? 'oversized_secret' : rawKeyString;
        const displayKey = rawKeyString.slice(0, 240);
        const baseKey = redactString(displayKey).slice(0, 80) || '[empty-key]';
        let key = baseKey;
        let collision = 2;
        while (Object.prototype.hasOwnProperty.call(out, key)) {
          key = `${baseKey.slice(0, 72)}#${collision++}`;
        }
        const normalized = normalizedKey(classificationKey);
        if (normalized === 'stack' || normalized === 'stacktrace' ||
            normalized === 'stack_trace' || normalized.endsWith('_stack') ||
            normalized.endsWith('_stacktrace') || normalized.endsWith('_stack_trace')) {
          out[key] = redactStack(child);
        } else out[key] = (!safeMetadataKey(classificationKey, child) &&
          SENSITIVE_KEY.test(normalized))
          ? '[REDACTED]' : sanitize(child, depth + 1, seen, budget);
      }
    } catch {
      out._unreadable_object = true;
    }
    if (truncated) out._truncated_keys = true;
    return out;
  } finally {
    seen.delete(value);
  }
}

function errorFields(err) {
  try {
    if (!err) return { name: 'Error', message_fingerprint: fingerprint('unknown error') };
    if (typeof err !== 'object') {
      const raw = safeString(err);
      return {
        name: typeof err,
        message_fingerprint: fingerprint(raw),
        message_length: raw.length,
      };
    }
    const rawMessage = safeString(err.message || safeString(err));
    const out = {
      name: safeString(err.name || 'Error').slice(0, 80),
      message_fingerprint: fingerprint(rawMessage),
      message_length: rawMessage.length,
    };
    if (err.code !== undefined) out.code = redactString(err.code);
    // Skip the first stack line because it repeats the arbitrary error message;
    // subsequent frames retain useful call sites after path redaction.
    if (err.stack) out.stack = redactStack(err.stack);
    return out;
  } catch {
    return { name: 'Error', message_fingerprint: fingerprint('unreadable error') };
  }
}

function fingerprint(value) {
  return crypto.createHash('sha256').update(safeString(value || '')).digest('hex').slice(0, 16);
}

function boundedFingerprint(value, maxChars = MAX_FINGERPRINT_PREFIX_CHARS) {
  const raw = safeString(value);
  const limit = boundedInt(maxChars, MAX_FINGERPRINT_PREFIX_CHARS, 1,
    MAX_FINGERPRINT_PREFIX_CHARS);
  // Include the original length so equal prefixes of different-sized values do
  // not collapse together, while hashing work remains strictly bounded.
  return {
    fingerprint: fingerprint(`${raw.length}:${raw.slice(0, limit)}`),
    length: raw.length,
    truncated: raw.length > limit,
  };
}

function defaultDir() {
  return process.env.ECHOECHO_DIAGNOSTICS_DIR ||
    path.join(os.homedir(), '.echoecho', 'diagnostics');
}

class Diagnostics {
  constructor(options = {}) {
    this.enabled = options.enabled === undefined ? diagnosticsEnabled() : !!options.enabled;
    this.dir = path.resolve(options.dir || defaultDir());
    this.runId = safeString(options.runId || crypto.randomUUID()).slice(0, 128) ||
      crypto.randomUUID();
    this.surface = options.surface || 'electron-main';
    this.latestName = options.latestName || 'latest-orb.json';
    this.build = sanitize(options.build || {});
    this.startedAt = new Date().toISOString();
    this.seq = 0;
    this.part = 0;
    this.files = [];
    this.fd = null;
    this.bytes = 0;
    this.rotationRetentionFailed = false;
    this.closed = false;
    this.writeFailed = false;
    this.maxBytes = boundedInt(options.maxBytes || process.env.ECHOECHO_DIAGNOSTICS_MAX_BYTES,
      DEFAULT_MAX_BYTES, 64 * 1024, 100 * 1024 * 1024);
    this.maxParts = boundedInt(options.maxParts || process.env.ECHOECHO_DIAGNOSTICS_MAX_PARTS,
      DEFAULT_MAX_PARTS, 1, 100);
    this.maxRuns = boundedInt(options.maxRuns || process.env.ECHOECHO_DIAGNOSTICS_MAX_RUNS,
      DEFAULT_MAX_RUNS, 1, 200);
    this.maxAgeDays = boundedInt(options.maxAgeDays || process.env.ECHOECHO_DIAGNOSTICS_MAX_AGE_DAYS,
      DEFAULT_MAX_AGE_DAYS, 1, 365);
    const stamp = this.startedAt.replace(/[:.]/g, '-');
    const runPrefix = this.runId.slice(0, 8);
    // Default run ids are UUIDs. Hash custom/non-UUID ids before using them in
    // a filename so the generated ownership grammar stays exact and path-safe.
    const runTag = /^[0-9a-f]{8}$/i.test(runPrefix)
      ? runPrefix.toLowerCase() : fingerprint(this.runId).slice(0, 8);
    this.runTag = runTag;
    this.baseName = `orb-run-${stamp}-${process.pid}-${runTag}`;
    if (!this.enabled) return;
    try {
      const directoryExisted = fs.existsSync(this.dir);
      fs.mkdirSync(this.dir, { recursive: true, mode: 0o700 });
      // A custom existing directory may intentionally be shared. Keep every
      // diagnostics file private without mutating that directory's policy.
      if (!directoryExisted) {
        try { fs.chmodSync(this.dir, 0o700); } catch { /* best effort on non-POSIX */ }
      }
      // Retention runs before creating this run. Reserve one run slot for it;
      // if a bounded shared-directory scan cannot enforce the ceiling,
      // diagnostics disable themselves instead of adding unbounded storage.
      if (!this._retain(1)) {
        const retentionError = new Error('diagnostics retention unavailable');
        retentionError.code = 'ERETENTION';
        throw retentionError;
      }
      this._openPart();
      this._writeLatest({ state: 'running' });
      this.info('diagnostics.start', { pid: process.pid });
    } catch (err) {
      this.enabled = false;
      try {
        process.stderr.write(`[diagnostics] disabled (${err && err.code ? err.code : 'initialization error'})\n`);
      } catch { /* diagnostics are never load-bearing */ }
    }
  }

  _partName() {
    return this.part ? `${this.baseName}.${this.part}.jsonl` : `${this.baseName}.jsonl`;
  }

  _openPart() {
    const partName = this._partName();
    const nextFile = path.join(this.dir, partName);
    // Every run/part name must be newly owned by this process. In a shared
    // diagnostics directory, append mode would follow a pre-planted symlink.
    const fd = fs.openSync(nextFile, 'wx', 0o600);
    try { fs.fchmodSync(fd, 0o600); } catch { /* best effort on non-POSIX */ }
    let nextBytes = 0;
    try { nextBytes = fs.fstatSync(fd).size; } catch { /* new file starts empty */ }
    const previousFd = this.fd;
    this.fd = fd;
    this.file = nextFile;
    this.bytes = nextBytes;
    if (previousFd !== null) {
      try { fs.closeSync(previousFd); } catch { /* best effort */ }
    }
    if (!this.files.includes(partName)) this.files.push(partName);
    while (this.files.length > this.maxParts) {
      const expired = this.files.shift();
      if (expired === partName) {
        this.files.unshift(expired);
        break;
      }
      try {
        fs.unlinkSync(path.join(this.dir, expired));
      } catch (err) {
        if (!err || err.code !== 'ENOENT') this.rotationRetentionFailed = true;
      }
    }
  }

  _rotate(nextBytes) {
    if (this.bytes === 0 || this.bytes + nextBytes <= this.maxBytes) return;
    if (this.rotationRetentionFailed) {
      const err = new Error('diagnostics rotation retention failed');
      err.code = 'ERETENTION';
      throw err;
    }
    this.part += 1;
    this._openPart();
    this._writeLatest({ state: 'running' });
  }

  _writeLatest(extra = {}) {
    const latest = {
      run_id: this.runId,
      started_at: this.startedAt,
      last_event_at: new Date().toISOString(),
      seq: this.seq,
      state: extra.state || 'running',
      files: this.files.slice(),
      build: this.build,
    };
    if (extra.closed_at) latest.closed_at = extra.closed_at;
    const target = path.join(this.dir, this.latestName);
    const tmp = path.join(
      this.dir,
      `.latest-${process.pid}-${this.runTag}-${crypto.randomUUID()}.tmp`);
    try {
      fs.writeFileSync(tmp, JSON.stringify(latest, null, 2) + '\n', {
        mode: 0o600, flag: 'wx',
      });
      fs.renameSync(tmp, target);
    } catch {
      try { fs.unlinkSync(tmp); } catch { /* absent */ }
    }
  }

  _retain(reservedRuns = 0) {
    const entries = [];
    let directory;
    let complete = true;
    try {
      directory = fs.opendirSync(this.dir);
      let scanned = 0;
      while (true) {
        const entry = directory.readSync();
        if (!entry) break;
        if (++scanned > MAX_RETENTION_ENTRIES) { complete = false; break; }
        // The diagnostics directory is shared with Python. Retain only this
        // component's regular files and never follow planted symlinks.
        const match = ORB_RUN_FILE_RE.exec(entry.name);
        if (!entry.isFile() || !match) continue;
        if (entries.length >= MAX_RETENTION_FILES) { complete = false; break; }
        const full = path.join(this.dir, entry.name);
        const info = fs.lstatSync(full);
        if (!info.isFile()) continue;
        entries.push({
          name: entry.name,
          full,
          mtime: info.mtimeMs,
          root: `${match[1]}.jsonl`,
        });
      }
    } catch {
      complete = false;
    } finally {
      if (directory) {
        try { directory.closeSync(); } catch { complete = false; }
      }
    }
    if (!complete) return false;
    entries.sort((a, b) => b.mtime - a.mtime);
    const cutoff = Date.now() - this.maxAgeDays * 86400000;
    const runRoots = [];
    const seenRoots = new Set();
    for (const entry of entries) {
      const root = entry.root;
      if (!seenRoots.has(root)) {
        if (seenRoots.size >= MAX_RETENTION_RUNS) return false;
        seenRoots.add(root);
        runRoots.push(root);
      }
    }
    const keepRoots = new Set(runRoots.slice(
      0, Math.max(0, this.maxRuns - reservedRuns)));
    for (const entry of entries) {
      const root = entry.root;
      if ((entry.mtime < cutoff || !keepRoots.has(root)) && !entry.name.startsWith(this.baseName)) {
        try {
          fs.unlinkSync(entry.full);
        } catch (err) {
          if (!err || err.code !== 'ENOENT') complete = false;
        }
      }
    }
    return complete;
  }

  log(level, event, fields = {}, surface) {
    if (!this.enabled || this.closed) return null;
    let record;
    try {
      const rawEvent = safeString(event || '');
      const safeEvent = SAFE_EVENT.test(rawEvent) ? rawEvent : 'diagnostics.invalid_event';
      record = {
        time: new Date().toISOString(),
        run_id: this.runId,
        seq: ++this.seq,
        level: ['debug', 'info', 'warn', 'error'].includes(level) ? level : 'info',
        event: safeEvent,
        surface: safeString(surface || this.surface).slice(0, 80),
        build: this.build,
        fields: sanitize(fields || {}),
      };
      let line = JSON.stringify(record) + '\n';
      let size = Buffer.byteLength(line);
      if (size > this.maxBytes) {
        const originalBytes = size;
        record.fields = { _truncated: true, original_bytes: originalBytes };
        record.build = { _truncated: true };
        record.truncated = true;
        line = JSON.stringify(record) + '\n';
        size = Buffer.byteLength(line);

        // This should only be reachable if a future metadata field becomes
        // unexpectedly large. Keep a final minimal representation so one
        // event can never make a JSONL part exceed its configured ceiling.
        if (size > this.maxBytes) {
          record = {
            run_id: fingerprint(this.runId),
            seq: record.seq,
            level: record.level,
            event: 'diagnostics.truncated_event',
            truncated: true,
            fields: { _truncated: true, original_bytes: originalBytes },
          };
          line = JSON.stringify(record) + '\n';
          size = Buffer.byteLength(line);
        }
        if (size > this.maxBytes) {
          const oversizedError = new Error('diagnostics event exceeds part limit');
          oversizedError.code = 'EOVERSIZE';
          throw oversizedError;
        }
      }
      this._rotate(size);
      fs.writeSync(this.fd, line, null, 'utf8');
      this.bytes += size;
      if (record.level === 'error' || this.seq % 25 === 0) this._writeLatest({ state: 'running' });
    } catch (err) {
      if (!this.writeFailed) {
        this.writeFailed = true;
        try {
          process.stderr.write(`[diagnostics] write failed (${err && err.code ? err.code : 'error'})\n`);
        } catch { /* diagnostics are never load-bearing */ }
      }
      return null;
    }
    return record;
  }

  debug(event, fields, surface) { return this.log('debug', event, fields, surface); }
  info(event, fields, surface) { return this.log('info', event, fields, surface); }
  warn(event, fields, surface) { return this.log('warn', event, fields, surface); }
  error(event, fields, surface) { return this.log('error', event, fields, surface); }

  child(surface) {
    return {
      debug: (event, fields) => this.debug(event, fields, surface),
      info: (event, fields) => this.info(event, fields, surface),
      warn: (event, fields) => this.warn(event, fields, surface),
      error: (event, fields) => this.error(event, fields, surface),
      errorFields,
      fingerprint,
    };
  }

  close(state = 'closed') {
    if (this.closed) return;
    if (!this.enabled) { this.closed = true; return; }
    this.info('diagnostics.stop', { state });
    this._writeLatest({ state, closed_at: new Date().toISOString() });
    if (this.fd !== null) {
      try { fs.closeSync(this.fd); } catch { /* best effort */ }
      this.fd = null;
    }
    this.closed = true;
  }
}

module.exports = {
  Diagnostics,
  createDiagnostics: (options) => new Diagnostics(options),
  sanitize,
  redactString,
  errorFields,
  fingerprint,
  boundedFingerprint,
  diagnosticsEnabled,
};
