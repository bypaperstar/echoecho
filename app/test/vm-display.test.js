'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { displayTarget } = require('../lib/vm-display');

test('VM display discovers the same local guest while voice is offline', async () => {
  let calls = 0;
  const target = await displayTarget({
    viewerInfo: async () => { throw new Error('Voice is offline'); },
    localInfo: async () => {
      calls++;
      return { ok: true, output: JSON.stringify({ url: 'vnc://:pw@127.0.0.1:5901' }) };
    },
  });
  assert.equal(target, 'vnc://:pw@127.0.0.1:5901');
  assert.equal(calls, 1);
});

test('VM overrides and the live viewer keep their discovery priority', async () => {
  const unexpected = async () => { throw new Error('unexpected fallback'); };
  assert.equal(await displayTarget({ override: 'vnc://override', viewerInfo: unexpected, localInfo: unexpected }), 'vnc://override');
  assert.equal(await displayTarget({ viewerInfo: async () => ({ url: 'vnc://viewer' }), localInfo: unexpected }), 'vnc://viewer');
});

test('Failed local discovery does not expose private child output', async () => {
  for (const result of [{ ok: false, output: 'secret-password' }, { ok: true, output: 'secret-password' }, { ok: true, output: '{}' }]) {
    await assert.rejects(displayTarget({ viewerInfo: async () => ({}), localInfo: async () => result }), (err) => {
      assert.equal(err.message.includes('secret-password'), false);
      return /shared Mac is unavailable/.test(err.message);
    });
  }
});
