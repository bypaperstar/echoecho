'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  Diagnostics, sanitize, redactString, errorFields, diagnosticsEnabled,
  boundedFingerprint,
} = require('../lib/diagnostics');

function tempDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'echoecho-diag-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

function records(dir) {
  return fs.readdirSync(dir)
    .filter((name) => name.endsWith('.jsonl'))
    .sort()
    .flatMap((name) => fs.readFileSync(path.join(dir, name), 'utf8')
      .trim().split('\n').filter(Boolean).map(JSON.parse));
}

test('writes bounded JSONL records and latest metadata', (t) => {
  const dir = tempDir(t);
  const diag = new Diagnostics({
    dir,
    runId: '11111111-2222-4333-8444-555555555555',
    build: { version: '1.2.3', sha: 'abc123' },
  });
  diag.info('test.ready', { count: 3 });
  diag.close('test-complete');

  const got = records(dir);
  assert.deepEqual(got.map((r) => r.event),
    ['diagnostics.start', 'test.ready', 'diagnostics.stop']);
  assert.deepEqual(got.map((r) => r.seq), [1, 2, 3]);
  assert.equal(got[1].run_id, '11111111-2222-4333-8444-555555555555');
  assert.equal(got[1].fields.count, 3);
  assert.equal(got[1].build.version, '1.2.3');

  const latest = JSON.parse(fs.readFileSync(path.join(dir, 'latest-orb.json'), 'utf8'));
  assert.equal(latest.run_id, diag.runId);
  assert.equal(latest.state, 'test-complete');
  assert.equal(latest.seq, 3);
  assert.ok(latest.closed_at);
  assert.equal(latest.files.length, 1);
});

test('recursively redacts secrets, content, URLs, hosts, and paths', (t) => {
  const dir = tempDir(t);
  const diag = new Diagnostics({ dir });
  const canaries = [
    'super-secret-password', 'typed private sentence', 'viewer-token-123',
    'vnc-pass-456', '10.20.30.40', '/Users/alice/workspace/private/report.md',
    'sk-live-canary123456789',
  ];
  diag.error('test.redaction', {
    authorization: 'Bearer viewer-token-123',
    nested: {
      password: 'super-secret-password',
      text: 'typed private sentence',
      safe_count: 7,
      message: 'failed vnc://:vnc-pass-456@10.20.30.40:5900 token=viewer-token-123',
      stack: 'Error at /Users/alice/workspace/private/report.md:4:2',
      api_key_value: 'sk-live-canary123456789',
    },
  });
  diag.close();
  const raw = fs.readdirSync(dir).filter((n) => n.endsWith('.jsonl'))
    .map((n) => fs.readFileSync(path.join(dir, n), 'utf8')).join('');
  for (const canary of canaries) assert.equal(raw.includes(canary), false, canary);
  assert.match(raw, /REDACTED/);
  assert.match(raw, /"safe_count":7/);
});

test('sanitizer handles cycles, depth, arrays, keys, and long strings', () => {
  const cyclic = { ok: true };
  cyclic.self = cyclic;
  const value = sanitize({
    cyclic,
    deep: { a: { b: { c: { d: { e: 1 } } } } },
    many: Array.from({ length: 40 }, (_, i) => i),
    long: 'x'.repeat(2000),
  });
  assert.equal(value.cyclic.self, '[CIRCULAR]');
  assert.equal(value.deep.a.b.c.d, '[MAX_DEPTH]');
  assert.equal(value.many.length, 33);
  assert.match(value.long, /truncated/);
  const keyed = sanitize({ 'token=viewer-token-canary': 'value' });
  assert.equal(JSON.stringify(keyed).includes('viewer-token-canary'), false);
  const huge = Object.fromEntries(
    Array.from({ length: 5000 }, (_, i) => [`key_${i}`, i]));
  const bounded = sanitize(huge);
  assert.equal(bounded._truncated_keys, true);
  assert.ok(Object.keys(bounded).length <= 49);
  const throwing = {};
  Object.defineProperty(throwing, 'broken', {
    enumerable: true, get() { throw new Error('getter failed'); },
  });
  assert.equal(sanitize(throwing).broken, '[UNREADABLE]');
});

test('sanitizer normalizes camelCase sensitive keys', () => {
  const value = sanitize({
    accessToken: 'secret-token',
    requestBody: 'private request',
    safeCount: 4,
    apiKeyValue: 'credential-canary',
    passwordValue: 'password-canary',
    transcriptPreview: 'dictation-canary',
    outputTokens: 12,
    audioBytes: 2048,
    stdoutChars: 'not-a-real-measurement',
  });
  assert.equal(value.accessToken, '[REDACTED]');
  assert.equal(value.requestBody, '[REDACTED]');
  assert.equal(value.safeCount, 4);
  assert.equal(value.apiKeyValue, '[REDACTED]');
  assert.equal(value.passwordValue, '[REDACTED]');
  assert.equal(value.transcriptPreview, '[REDACTED]');
  assert.equal(value.outputTokens, 12);
  assert.equal(value.audioBytes, 2048);
  assert.equal(value.stdoutChars, '[REDACTED]');
});

test('sanitizer classifies sensitive suffixes before display-key truncation', () => {
  const secret = 'credential-canary-after-long-key';
  const privateBody = 'private-body-canary-after-long-key';
  const value = sanitize({
    [`${'x'.repeat(300)}_accessToken`]: secret,
    [`${'y'.repeat(300)}_requestBody`]: privateBody,
    [`${'z'.repeat(1025)}`]: 'oversized-key-canary',
  });
  const encoded = JSON.stringify(value);
  assert.equal(encoded.includes(secret), false);
  assert.equal(encoded.includes(privateBody), false);
  assert.equal(encoded.includes('oversized-key-canary'), false);
});

test('existing diagnostics directory permissions are preserved', (t) => {
  const parent = tempDir(t);
  const dir = path.join(parent, 'shared');
  fs.mkdirSync(dir, { mode: 0o755 });
  fs.chmodSync(dir, 0o755);
  const before = fs.statSync(dir).mode & 0o777;
  const diag = new Diagnostics({ dir });
  diag.close();
  assert.equal(fs.statSync(dir).mode & 0o777, before);
});

test('string redaction keeps ordinary operational messages useful', () => {
  assert.equal(redactString('viewer request failed with HTTP 503'),
    'viewer request failed with HTTP 503');
  assert.equal(redactString('Bearer abc123'), 'Bearer [REDACTED]');
  assert.equal(redactString('connect ws://127.0.0.1:123/?token=abc'),
    'connect [REDACTED_URL]');
});

test('error stacks keep frames but drop multiline message content', () => {
  const canary = 'private second line from an upstream response body';
  const err = new Error('first line');
  err.stack = `Error: first line\n${canary}\n    at worker (/Users/alice/private.js:4:2)`;

  const fields = errorFields(err);
  assert.equal(JSON.stringify(fields).includes(canary), false);
  assert.match(fields.stack, /at worker/);
  const direct = sanitize({
    stackTrace: `Error: headline\n${canary}\n    at handler (node:internal/test:1:2)`,
  });
  assert.equal(JSON.stringify(direct).includes(canary), false);
  assert.match(direct.stackTrace, /at handler/);
});

test('retention removes older runs beyond the configured run count', (t) => {
  const dir = tempDir(t);
  fs.writeFileSync(path.join(dir, 'run-python-component.jsonl'), '{}\n');
  for (let i = 0; i < 4; i++) {
    const name = `orb-run-2020-01-0${i + 1}T00-00-00-000Z-1-${String(i).padStart(8, '0')}.jsonl`;
    const file = path.join(dir, name);
    fs.writeFileSync(file, '{}\n');
    const when = new Date(Date.now() - (10 - i) * 1000);
    fs.utimesSync(file, when, when);
  }
  const diag = new Diagnostics({ dir, maxRuns: 2, maxAgeDays: 365 });
  diag.close();
  const runs = fs.readdirSync(dir).filter((n) => n.startsWith('orb-run-') && n.endsWith('.jsonl'));
  assert.equal(runs.length, 2); // current run + newest pre-existing run
  assert.equal(fs.existsSync(path.join(dir, 'run-python-component.jsonl')), true);
});

test('failed historical retention disables diagnostics before adding a run', (t) => {
  const dir = tempDir(t);
  const old = path.join(dir, 'orb-run-2020-01-01T00-00-00-000Z-1-deadbeef.jsonl');
  fs.writeFileSync(old, '{}\n');
  const originalUnlink = fs.unlinkSync;
  fs.unlinkSync = (target) => {
    if (target === old) {
      const err = new Error('retention denied');
      err.code = 'EACCES';
      throw err;
    }
    return originalUnlink(target);
  };
  let diag;
  try {
    diag = new Diagnostics({ dir, maxRuns: 1, maxAgeDays: 1 });
  } finally {
    fs.unlinkSync = originalUnlink;
  }

  assert.equal(diag.enabled, false);
  assert.equal(fs.existsSync(old), true);
  assert.deepEqual(fs.readdirSync(dir).filter((name) =>
    name.startsWith('orb-run-')), [path.basename(old)]);
});

test('retention preserves unrelated files which only resemble Orb logs', (t) => {
  const dir = tempDir(t);
  const unrelated = [
    'orb-run-personal-not-owned.jsonl',
    'orb-run-2020-01-01T00-00-00-000Z-1-not-hex.jsonl',
    'orb-run-2020-01-01T00-00-00-000Z-1-deadbeef.backup.jsonl',
    'orb-run-2020-01-01T00-00-00-000Z-1-DEADBEEF.jsonl',
  ];
  for (const name of unrelated) {
    const file = path.join(dir, name);
    fs.writeFileSync(file, 'important operator data\n');
    fs.utimesSync(file, new Date(0), new Date(0));
  }
  const owned = path.join(
    dir, 'orb-run-2020-01-01T00-00-00-000Z-1-cafebabe.jsonl');
  fs.writeFileSync(owned, '{}\n');
  fs.utimesSync(owned, new Date(0), new Date(0));

  const diag = new Diagnostics({ dir, maxRuns: 1, maxAgeDays: 1 });
  diag.close();

  assert.equal(fs.existsSync(owned), false);
  for (const name of unrelated) {
    assert.equal(fs.readFileSync(path.join(dir, name), 'utf8'),
      'important operator data\n');
  }
});

test('bounded fingerprints retain original length and ignore oversized suffixes', () => {
  const prefix = 'x'.repeat(16);
  const first = boundedFingerprint(prefix + 'first private suffix', 16);
  const second = boundedFingerprint(prefix + 'other private suffix', 16);

  assert.equal(first.truncated, true);
  assert.equal(first.length, (prefix + 'first private suffix').length);
  assert.equal(first.fingerprint, second.fingerprint);
  assert.match(first.fingerprint, /^[a-f0-9]{16}$/);
});

test('global environment opt-out creates no diagnostics files', (t) => {
  const parent = tempDir(t);
  const dir = path.join(parent, 'disabled');
  const prior = process.env.ECHOECHO_DIAGNOSTICS;
  t.after(() => {
    if (prior === undefined) delete process.env.ECHOECHO_DIAGNOSTICS;
    else process.env.ECHOECHO_DIAGNOSTICS = prior;
  });
  for (const value of ['0', 'false', 'NO', 'off']) assert.equal(diagnosticsEnabled(value), false);
  process.env.ECHOECHO_DIAGNOSTICS = 'off';
  const diag = new Diagnostics({ dir });
  diag.error('should.not.write', { safe: true });
  diag.close();
  assert.equal(fs.existsSync(dir), false);
});

test('an unwritable diagnostics target never prevents app startup', (t) => {
  const dir = tempDir(t);
  const notDirectory = path.join(dir, 'regular-file');
  fs.writeFileSync(notDirectory, 'occupied');
  const diag = new Diagnostics({ dir: notDirectory });
  assert.equal(diag.enabled, false);
  assert.doesNotThrow(() => diag.error('ignored.event', { value: 1 }));
});

test('initialization failure remains safe when stderr is unavailable', (t) => {
  const dir = tempDir(t);
  const notDirectory = path.join(dir, 'regular-file');
  fs.writeFileSync(notDirectory, 'occupied');
  const original = process.stderr.write;
  process.stderr.write = () => { throw new Error('closed stderr'); };
  try {
    assert.doesNotThrow(() => new Diagnostics({ dir: notDirectory }));
  } finally {
    process.stderr.write = original;
  }
});

test('hostile fields and rotation failures never escape diagnostics', (t) => {
  const dir = tempDir(t);
  const diag = new Diagnostics({ dir });
  const hostile = new Proxy({}, {
    ownKeys() { throw new Error('hostile getter'); },
  });
  assert.doesNotThrow(() => {
    assert.ok(diag.info('test.hostile', hostile));
  });
  diag._rotate = () => { throw new Error('rotation failed'); };
  assert.doesNotThrow(() => diag.info('test.rotation_failure', { count: 1 }));
  assert.doesNotThrow(() => diag.close());
});

test('rotation keeps a bounded recent ring and updates the pointer', (t) => {
  const dir = tempDir(t);
  const diag = new Diagnostics({
    dir, runId: 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee',
    maxBytes: 64 * 1024, maxParts: 2,
  });
  const large = Object.fromEntries(
    Array.from({ length: 48 }, (_, i) => [`field_${i}`, 'x'.repeat(1000)]));
  for (let i = 0; i < 40; i++) diag.info('rotation.sample', { i, large });
  diag.close();

  const latest = JSON.parse(fs.readFileSync(path.join(dir, 'latest-orb.json'), 'utf8'));
  const files = fs.readdirSync(dir).filter((name) =>
    name.startsWith('orb-run-') && name.endsWith('.jsonl'));
  assert.equal(files.length, 2);
  assert.equal(latest.files.length, 2);
  assert.deepEqual(new Set(files), new Set(latest.files));
  const seqs = latest.files.flatMap((name) => fs.readFileSync(path.join(dir, name), 'utf8')
    .trim().split('\n').filter(Boolean).map((line) => JSON.parse(line).seq));
  assert.deepEqual(seqs, [...seqs].sort((a, b) => a - b));
});

test('oversized events are replaced before they can exceed a part limit', (t) => {
  const dir = tempDir(t);
  const maxBytes = 64 * 1024;
  const diag = new Diagnostics({
    dir,
    runId: 'r'.repeat(10000),
    maxBytes,
    build: { channel: 'test' },
  });
  const huge = Object.fromEntries(Array.from({ length: 48 }, (_, group) => [
    `group_${group}`,
    Object.fromEntries(Array.from({ length: 16 }, (_, field) => [
      `field_${field}`, 'x'.repeat(1000),
    ])),
  ]));

  diag.info('oversized.sample', { huge });
  diag.close();

  assert.equal(diag.runId.length, 128);
  const files = fs.readdirSync(dir).filter((name) =>
    name.startsWith('orb-run-') && name.endsWith('.jsonl'));
  assert.ok(files.length >= 1);
  assert.ok(files.every((name) => fs.statSync(path.join(dir, name)).size <= maxBytes));
  const sample = records(dir).find((record) => record.event === 'oversized.sample');
  assert.ok(sample);
  assert.equal(sample.run_id.length, 128);
  assert.equal(sample.truncated, true);
  assert.equal(sample.fields._truncated, true);
  assert.ok(sample.fields.original_bytes > maxBytes);
  assert.deepEqual(sample.build, { _truncated: true });
});

test('rotation stops growing after an old part cannot be removed', (t) => {
  const dir = tempDir(t);
  const diag = new Diagnostics({
    dir, runId: 'bbbbbbbb-cccc-4ddd-8eee-ffffffffffff',
    maxBytes: 64 * 1024, maxParts: 1,
  });
  const base = diag.file;
  const originalUnlink = fs.unlinkSync;
  fs.unlinkSync = (target) => {
    if (target === base) {
      const err = new Error('retention denied');
      err.code = 'EACCES';
      throw err;
    }
    return originalUnlink(target);
  };
  try {
    const large = Object.fromEntries(
      Array.from({ length: 48 }, (_, i) => [`field_${i}`, 'x'.repeat(1000)]));
    for (let i = 0; i < 120; i++) diag.info('rotation.sample', { i, large });
  } finally {
    fs.unlinkSync = originalUnlink;
  }
  diag.close();

  const files = fs.readdirSync(dir).filter((name) =>
    name.startsWith(diag.baseName) && name.endsWith('.jsonl'));
  assert.equal(diag.rotationRetentionFailed, true);
  assert.equal(diag.part, 1);
  assert.equal(files.length, 2);
  assert.ok(files.every((name) => fs.statSync(path.join(dir, name)).size <= diag.maxBytes));
});

test('pointer writes and rotated parts never follow predictable symlinks', (t) => {
  const dir = tempDir(t);
  const victim = path.join(dir, 'victim.txt');
  fs.writeFileSync(victim, 'unchanged');
  const runId = '12345678-abcd-4abc-8abc-1234567890ab';
  fs.symlinkSync(victim, path.join(dir, `.latest-${process.pid}-12345678.tmp`));
  const diag = new Diagnostics({ dir, runId, maxBytes: 64 * 1024 });
  const firstPart = `${diag.baseName}.1.jsonl`;
  const partVictim = path.join(dir, 'part-victim.txt');
  fs.writeFileSync(partVictim, 'part-unchanged');
  fs.symlinkSync(partVictim, path.join(dir, firstPart));
  const large = Object.fromEntries(
    Array.from({ length: 48 }, (_, i) => [`field_${i}`, 'x'.repeat(1000)]));
  for (let i = 0; i < 8; i++) diag.info('rotation.symlink_probe', { i, large });
  diag.close();

  assert.equal(fs.readFileSync(victim, 'utf8'), 'unchanged');
  assert.equal(fs.readFileSync(partVictim, 'utf8'), 'part-unchanged');
});
