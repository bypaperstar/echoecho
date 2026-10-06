'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { voiceState } = require('../renderer/control-state');
const { validate, write, read } = require('../lib/preferences');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

test('an HTTP server alone never claims a microphone is listening', () => {
  assert.equal(voiceState({ viewer: true }).healthy, false);
  assert.equal(voiceState({ viewer: true, voice: { captureActive: false } }).healthy, false);
  assert.equal(voiceState({ viewer: true, voice: { captureActive: true, captureAge: 10 } }).badge, 'Check microphone');
});
test('privacy permission, paused capture, connection and conversation states stay distinct', () => {
  const status = { viewer: true, voice: { captureActive: true, captureAge: .1, phase: 'ready', session: 'IDLE' } };
  assert.equal(voiceState(status).badge, 'Listening');
  assert.equal(voiceState({ ...status, microphonePermission: 'denied' }).badge, 'Needs permission');
  assert.equal(voiceState({ ...status, voice: { ...status.voice, phase: 'paused' } }).badge, 'Paused');
  assert.equal(voiceState({ ...status, voice: { ...status.voice, phase: 'connecting' } }).badge, 'Connecting');
  assert.equal(voiceState({ ...status, voice: { ...status.voice, session: 'ACTIVE' } }).badge, 'In conversation');
});
test('settings persist privately and never accept key material or arbitrary models', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'echo-prefs-'));
  const prior = process.env.ECHOECHO_PREFERENCES_FILE;
  process.env.ECHOECHO_PREFERENCES_FILE = path.join(root, 'prefs.json');
  try {
    write({ voiceModel: 'gpt-live-1', inputDevice: 'MacBook Pro Microphone' });
    write({ recordSessions: false });
    assert.equal(read().voiceModel, 'gpt-live-1');
    assert.equal(read().recordSessions, false);
    assert.equal(fs.statSync(process.env.ECHOECHO_PREFERENCES_FILE).mode & 0o777, 0o600);
    assert.throws(() => validate({ OPENAI_API_KEY: 'private' }));
    assert.throws(() => validate({ voiceModel: 'unknown' }));
    assert.throws(() => validate({ recordSessions: 'yes' }));
  } finally {
    if (prior === undefined) delete process.env.ECHOECHO_PREFERENCES_FILE;
    else process.env.ECHOECHO_PREFERENCES_FILE = prior;
    fs.rmSync(root, { recursive: true });
  }
});
