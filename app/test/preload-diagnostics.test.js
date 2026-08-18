'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function loadPreloadDiagnostics() {
  const sent = [];
  const exposed = {};
  const listeners = {};
  const electron = {
    contextBridge: {
      exposeInMainWorld(name, value) { exposed[name] = value; },
    },
    ipcRenderer: {
      send(channel, payload) { sent.push({ channel, payload }); },
      invoke() { return Promise.resolve(); },
      on() {},
    },
  };
  const sandbox = {
    require(name) {
      if (name === 'electron') return electron;
      throw new Error(`unexpected preload require: ${name}`);
    },
    window: {
      addEventListener(name, callback) { listeners[name] = callback; },
    },
  };
  const source = fs.readFileSync(path.join(__dirname, '..', 'preload.js'), 'utf8');
  vm.runInNewContext(source, sandbox, { filename: 'preload.js' });
  return { report: exposed.echoDiagnostics.report, sent, listeners };
}

test('preload fingerprints bounded prefixes before renderer diagnostics cross IPC', () => {
  const { report, sent } = loadPreloadDiagnostics();
  const privateSuffix = 'PRIVATE-SUFFIX-MUST-NOT-CROSS-IPC';
  const message = 'm'.repeat(100000) + privateSuffix;
  const stack = 's'.repeat(100000) + privateSuffix;

  assert.equal(report('client.error', {
    error_name: 'Error', message, stack,
  }), true);

  assert.equal(sent.length, 1);
  assert.equal(sent[0].channel, 'diagnostics:event');
  const payload = sent[0].payload;
  assert.equal(payload.fields.message_length, message.length);
  assert.equal(payload.fields.stack_length, stack.length);
  assert.match(payload.fields.message_fingerprint, /^[a-f0-9]{16}$/);
  assert.match(payload.fields.stack_fingerprint, /^[a-f0-9]{16}$/);
  assert.equal(JSON.stringify(payload).includes(privateSuffix), false);
  assert.ok(JSON.stringify(payload).length < 1000);
});

test('preload caps primitive fields and replaces oversized token strings', () => {
  const { report, sent } = loadPreloadDiagnostics();
  const input = {};
  const allowed = [
    'line', 'column', 'error_name', 'error_code', 'consecutive', 'duration_ms',
    'failed_attempts', 'downtime_ms', 'daemon', 'vm', 'orb_visible', 'action',
    'ok', 'detached', 'enabled', 'operation', 'transition', 'reason', 'connected',
    'status', 'running', 'stage', 'outcome', 'attempt', 'clean', 'was_connected',
    'connected_ms', 'has_password', 'has_reason',
  ];
  let reads = 0;
  for (const key of allowed) {
    Object.defineProperty(input, key, {
      enumerable: true,
      get() {
        reads++;
        return key === 'action' ? 'private-action-'.repeat(10000) : 1;
      },
    });
  }

  assert.equal(report('control.action_start', input), true);

  assert.ok(reads <= 24);
  assert.ok(Object.keys(sent[0].payload.fields).length <= 24);
  assert.ok(JSON.stringify(sent[0].payload).length < 3000);
  assert.equal(JSON.stringify(sent[0].payload).includes('private-action-private-action'), false);
});

test('preload rejects oversized event names without sending IPC', () => {
  const { report, sent } = loadPreloadDiagnostics();
  assert.equal(report('x'.repeat(100000), { message: 'private' }), false);
  assert.equal(sent.length, 0);
});

