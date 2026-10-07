'use strict';

const assert = require('node:assert/strict');
const { once } = require('node:events');
const net = require('node:net');
const test = require('node:test');
const { WebSocket } = require('ws');
const proxy = require('../vnc-proxy');

test('a cold display opens exactly one TCP session for the real renderer', async () => {
  const sockets = new Set();
  let connections = 0;
  let remoteEnd;
  const server = net.createServer(socket => {
    connections++;
    remoteEnd = once(socket, 'end');
    sockets.add(socket);
    socket.on('close', () => sockets.delete(socket));
    socket.on('error', () => {});
    socket.write('RFB 003.008\n');
    socket.on('data', data => socket.write(data));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  let ws;
  try {
    const info = await proxy.start(`vnc://:testpass@127.0.0.1:${server.address().port}`);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(connections, 0, 'preparation must not open a throwaway probe');
    ws = new WebSocket(info.wsUrl);
    const banner = once(ws, 'message');
    await once(ws, 'open');
    assert.equal(String((await banner)[0]), 'RFB 003.008\n');
    const response = once(ws, 'message');
    ws.send('RFB 003.008\n');
    assert.equal(String((await response)[0]), 'RFB 003.008\n');
    assert.equal(connections, 1);
    await proxy.stop();
    await remoteEnd; // The guest receives EOF rather than an abrupt reset.
  } finally {
    if (ws) ws.terminate();
    await proxy.stop();
    for (const socket of sockets) socket.destroy();
    await new Promise(resolve => server.close(resolve));
  }
});
