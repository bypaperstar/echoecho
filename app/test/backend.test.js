'use strict';

const assert = require('node:assert/strict');
const http = require('node:http');
const test = require('node:test');

const { ViewerClient } = require('../lib/backend');

class CaptureDiagnostics {
  constructor() { this.records = []; }
  _push(level, event, fields) { this.records.push({ level, event, fields }); }
  debug(event, fields) { this._push('debug', event, fields); }
  info(event, fields) { this._push('info', event, fields); }
  warn(event, fields) { this._push('warn', event, fields); }
  error(event, fields) { this._push('error', event, fields); }
  errorFields(err) { return { name: err.name, code: err.code || null }; }
}

async function withServer(handler, fn) {
  const server = http.createServer(handler);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  try {
    return await fn(`http://127.0.0.1:${address.port}`);
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
}

test('ViewerClient records endpoint metadata without document paths', async () => {
  await withServer((req, res) => {
    if (req.url.startsWith('/doc?')) {
      res.writeHead(200, { 'Content-Type': 'text/plain' });
      res.end('private document contents');
      return;
    }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end('[]');
  }, async (base) => {
    const diagnostics = new CaptureDiagnostics();
    const client = new ViewerClient(base, { diagnostics, fetchTimeoutMs: 250 });
    assert.equal(await client.doc('private/client-name.md'), 'private document contents');
    assert.deepEqual(await client.transcript(), []);
    const raw = JSON.stringify(diagnostics.records);
    assert.equal(raw.includes('client-name'), false);
    assert.equal(raw.includes('private document contents'), false);
    assert.deepEqual(diagnostics.records.filter((r) => r.event === 'viewer.http_ready')
      .map((r) => r.fields.endpoint).sort(), ['document', 'transcript']);
  });
});

test('ViewerClient bounds stalled fetches and classifies the timeout', async () => {
  await withServer((_req, _res) => { /* deliberately never respond */ }, async (base) => {
    const diagnostics = new CaptureDiagnostics();
    const client = new ViewerClient(base, { diagnostics, fetchTimeoutMs: 40 });
    await assert.rejects(client.transcript(), (err) => err.code === 'ETIMEDOUT');
    const failure = diagnostics.records.find((r) => r.event === 'viewer.http_failed');
    assert.ok(failure);
    assert.equal(failure.fields.endpoint, 'transcript');
    assert.equal(failure.fields.error.code, 'ETIMEDOUT');
    assert.equal(failure.fields.consecutive, 1);
  });
});

test('ViewerClient gives vnc discovery its server-side lookup budget', async () => {
  await withServer((_req, res) => {
    setTimeout(() => {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"url":"vnc://example"}');
    }, 80);
  }, async (base) => {
    const client = new ViewerClient(base, {
      diagnostics: new CaptureDiagnostics(), fetchTimeoutMs: 30,
      vncTimeoutMs: 200,
    });
    assert.deepEqual(await client.json('/vnc-info'), { url: 'vnc://example' });
  });
});

test('ViewerClient coalesces repeated poll failures and reports recovery', async () => {
  let fail = true;
  await withServer((_req, res) => {
    if (fail) {
      res.writeHead(503);
      res.end('down');
    } else {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('[]');
    }
  }, async (base) => {
    const diagnostics = new CaptureDiagnostics();
    const client = new ViewerClient(base, { diagnostics, fetchTimeoutMs: 250 });
    for (let i = 0; i < 3; i++) await assert.rejects(client.transcript());
    assert.equal(diagnostics.records.filter((r) => r.event === 'viewer.http_failed').length, 2);
    fail = false;
    assert.deepEqual(await client.transcript(), []);
    const recovered = diagnostics.records.find((r) => r.event === 'viewer.http_recovered');
    assert.ok(recovered);
    assert.equal(recovered.fields.failed_attempts, 3);
  });
});

test('ViewerClient serializes and coalesces overlapping reload triggers', async () => {
  const diagnostics = new CaptureDiagnostics();
  const client = new ViewerClient('http://127.0.0.1:1', { diagnostics });
  let releaseFirst;
  let calls = 0;
  client.transcript = () => {
    calls++;
    if (calls === 1) {
      return new Promise((resolve) => { releaseFirst = () => resolve([]); });
    }
    return Promise.resolve([]);
  };

  client._onReload();
  for (let i = 0; i < 9; i++) client._onReload();
  assert.equal(calls, 1);
  assert.equal(client.reloadInFlight, 1);

  releaseFirst();
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(calls, 2);
  assert.equal(client.reloadInFlight, 0);
  const coalesced = diagnostics.records.find(
    (record) => record.event === 'viewer.reload_coalesced');
  assert.ok(coalesced);
  assert.equal(coalesced.fields.overlapping_triggers, 9);
  assert.equal(coalesced.fields.reruns, 1);
});

test('ViewerClient records unexpected reload processing errors without rejection', async () => {
  const diagnostics = new CaptureDiagnostics();
  const client = new ViewerClient('http://127.0.0.1:1', { diagnostics });
  client.primed = true;
  client.transcript = async () => [{ ts: 1, type: 'state' }];
  client.on('events', () => { throw new Error('listener failed'); });

  client._onReload();
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(client.reloadInFlight, 0);
  assert.ok(diagnostics.records.some(
    (record) => record.event === 'viewer.reload_processing_failed'));
});

test('ViewerClient sequence watermark preserves events with equal timestamps', async () => {
  const diagnostics = new CaptureDiagnostics();
  const client = new ViewerClient('http://127.0.0.1:1', { diagnostics });
  const batches = [];
  let response = [
    { ts: 100, run_id: 'run-equal', seq: 1, type: 'run' },
  ];
  client.transcript = async () => response;
  client.on('events', (events) => batches.push(events));

  await client._reloadOnce();
  response = [
    ...response,
    { ts: 100, run_id: 'run-equal', seq: 2, type: 'state' },
  ];
  await client._reloadOnce();

  assert.deepEqual(batches.map((batch) => batch.map((event) => event.seq)), [[2]]);
  assert.equal(client.lastSeq, 2);
  assert.equal(diagnostics.records.find(
    (record) => record.event === 'viewer.reload_events').fields.watermark, 'sequence');
});

test('ViewerClient sequence watermark survives a backward wall clock', async () => {
  const client = new ViewerClient('http://127.0.0.1:1', {
    diagnostics: new CaptureDiagnostics(),
  });
  const batches = [];
  let response = [
    { ts: 500, run_id: 'run-clock', seq: 1, type: 'run' },
  ];
  client.transcript = async () => response;
  client.on('events', (events) => batches.push(events));

  await client._reloadOnce();
  response = [
    ...response,
    { ts: 400, run_id: 'run-clock', seq: 2, type: 'assistant_text' },
  ];
  await client._reloadOnce();

  assert.deepEqual(batches.map((batch) => batch.map((event) => event.seq)), [[2]]);
});

test('ViewerClient resets sequence watermark on run changes without logging run ids', async () => {
  const diagnostics = new CaptureDiagnostics();
  const client = new ViewerClient('http://127.0.0.1:1', { diagnostics });
  const batches = [];
  let response = [
    { ts: 500, run_id: 'private-old-run', seq: 9, type: 'run' },
  ];
  client.transcript = async () => response;
  client.on('events', (events) => batches.push(events));

  await client._reloadOnce();
  response = [
    { ts: 500, run_id: 'private-old-run', seq: 9, type: 'run' },
    { ts: 900, type: 'assistant_text' },
    { ts: 100, run_id: 'private-new-run', seq: 1, type: 'run' },
    { ts: 90, run_id: 'private-new-run', seq: 2, type: 'wake' },
  ];
  await client._reloadOnce();

  assert.deepEqual(batches.map((batch) => batch.map((event) => event.seq)), [[1, 2]]);
  assert.equal(client.lastRunId, 'private-new-run');
  assert.equal(client.lastSeq, 2);
  const reload = diagnostics.records.find(
    (record) => record.event === 'viewer.reload_events');
  assert.equal(reload.fields.run_changed, true);
  assert.equal(JSON.stringify(diagnostics.records).includes('private-new-run'), false);
  assert.equal(JSON.stringify(diagnostics.records).includes('private-old-run'), false);
});

test('ViewerClient retains timestamp fallback for legacy transcript records', async () => {
  const client = new ViewerClient('http://127.0.0.1:1', {
    diagnostics: new CaptureDiagnostics(),
  });
  const batches = [];
  let response = [{ ts: 100, type: 'run' }];
  client.transcript = async () => response;
  client.on('events', (events) => batches.push(events));

  await client._reloadOnce();
  response = [
    ...response,
    { ts: 100, type: 'state' },
    { ts: 101, type: 'assistant_text' },
  ];
  await client._reloadOnce();

  assert.deepEqual(batches.map((batch) => batch.map((event) => event.ts)), [[101]]);
  assert.equal(client.lastTs, 101);
});
