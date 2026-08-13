// WebSocket <-> raw TCP bridge for Echo's Mac (main process only).
//
// The renderer can't open raw TCP and the VM's VNC server speaks plain RFB,
// so main runs a tiny ws server on 127.0.0.1 (ephemeral port) and pipes each
// WebSocket client into a fresh TCP connection to the VNC host. The password
// parsed from the vnc:// URL is returned to the caller (it rides to the
// renderer over IPC) and is never logged.
'use strict';

const net = require('net');
const { WebSocketServer } = require('ws');

const PROBE_TIMEOUT_MS = 4000;
// Above this much unsent WS data the TCP side pauses (framebuffer bursts can
// outrun a slow renderer; RFB has no flow control of its own).
const HIGH_WATER = 1 << 20;

let state = null; // { key, info, wss, conns: Set<{ws, tcp}> }

// vnc://[user[:password]@]host[:port] — lume emits vnc://:pass@ip:port.
function parseVncUrl(raw) {
  let url;
  try {
    url = new URL(String(raw));
  } catch {
    throw new Error('invalid VNC URL');
  }
  if (url.protocol !== 'vnc:' || !url.hostname) {
    throw new Error('invalid VNC URL (expected vnc://[user[:password]@]host:port)');
  }
  return {
    host: url.hostname,
    port: url.port ? Number(url.port) : 5900,
    password: url.password ? decodeURIComponent(url.password) : '',
  };
}

// Fail fast while the VM is off, so vnc:connect rejects and the renderer can
// say "Echo's Mac is asleep" instead of a WS that opens then dies.
function probe(target) {
  return new Promise((resolve, reject) => {
    const sock = net.connect({ host: target.host, port: target.port });
    const fail = (why) => {
      sock.destroy();
      reject(new Error(`cannot reach VNC at ${target.host}:${target.port} (${why})`));
    };
    sock.setTimeout(PROBE_TIMEOUT_MS, () => fail('timeout'));
    sock.on('error', (err) => fail(err.code || err.message));
    sock.on('connect', () => {
      sock.destroy();
      resolve();
    });
  });
}

function bridge(ws, target, conns) {
  const tcp = net.connect({ host: target.host, port: target.port });
  tcp.setNoDelay(true);
  const entry = { ws, tcp };
  conns.add(entry);
  const drop = () => {
    conns.delete(entry);
    tcp.destroy();
    try { ws.terminate(); } catch {}
  };

  // ws -> tcp (net queues writes until 'connect', no buffering needed here)
  ws.on('message', (data) => {
    if (!tcp.write(data)) {
      ws.pause();
      tcp.once('drain', () => ws.resume());
    }
  });
  ws.on('close', drop);
  ws.on('error', drop);

  // tcp -> ws, pausing the TCP side while the WS send queue is backed up
  tcp.on('data', (chunk) => {
    ws.send(chunk, () => {
      if (ws.bufferedAmount < HIGH_WATER) tcp.resume();
    });
    if (ws.bufferedAmount >= HIGH_WATER) tcp.pause();
  });
  tcp.on('error', (err) => {
    conns.delete(entry);
    try { ws.close(1011, `vnc target unreachable (${err.code || 'error'})`); } catch {}
  });
  tcp.on('close', () => {
    conns.delete(entry);
    try { ws.close(1000, 'vnc target closed'); } catch {}
  });
}

async function start(targetUrl) {
  const target = parseVncUrl(targetUrl);
  const key = `${target.host}:${target.port}#${target.password}`;
  if (state && state.key === key) return state.info; // same target: reuse
  await stop(); // different target: tear the old bridge down first

  await probe(target);

  const wss = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await new Promise((resolve, reject) => {
    wss.once('listening', resolve);
    wss.once('error', reject);
  });
  const conns = new Set();
  wss.on('connection', (ws) => bridge(ws, target, conns));

  const info = {
    wsUrl: `ws://127.0.0.1:${wss.address().port}`,
    password: target.password,
    host: target.host,
    port: target.port,
  };
  state = { key, info, wss, conns };
  console.log(`[vnc-proxy] bridging ${info.wsUrl} -> ${target.host}:${target.port}`);
  return info;
}

function stop() {
  if (!state) return Promise.resolve();
  const { wss, conns } = state;
  state = null;
  for (const { ws, tcp } of conns) {
    try { ws.terminate(); } catch {}
    tcp.destroy();
  }
  conns.clear();
  return new Promise((resolve) => wss.close(() => resolve()));
}

module.exports = { start, stop, parseVncUrl };
