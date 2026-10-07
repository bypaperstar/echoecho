// WebSocket <-> raw TCP bridge for echoecho's Mac (main process only).
//
// The renderer can't open raw TCP and the VM's VNC server speaks plain RFB,
// so main runs a tiny ws server on 127.0.0.1 (ephemeral port) and pipes each
// WebSocket client into a fresh TCP connection to the VNC host. The password
// parsed from the vnc:// URL is returned to the caller (it rides to the
// renderer over IPC) and is never logged.
'use strict';

const crypto = require('crypto');
const net = require('net');
const { WebSocketServer } = require('ws');

// Above this much unsent WS data the TCP side pauses (framebuffer bursts can
// outrun a slow renderer; RFB has no flow control of its own).
const HIGH_WATER = 1 << 20;

let state = null; // { key, info, wss, conns: Set<{ws, tcp}> }
let warnedNoPassword = false;
let diagnostics = null;

function log(level, event, fields) {
  if (diagnostics && typeof diagnostics[level] === 'function') diagnostics[level](event, fields);
}

function errorMeta(err) {
  return diagnostics && diagnostics.errorFields ? diagnostics.errorFields(err) : {
    name: String((err && err.name) || 'Error'), code: err && err.code,
  };
}

// start/stop both mutate the single module-level `state`; interleaved calls
// (rapid open/close from the renderer) would race — e.g. a stop() landing
// mid-start leaks the half-built wss — so every operation runs through this
// promise chain, one at a time, in call order.
let op = Promise.resolve();
function serialize(fn) {
  const run = op.then(fn);
  op = run.then(() => undefined, () => undefined); // keep the chain alive
  return run;
}

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

function bridge(ws, target, conns) {
  const tcp = net.connect({ host: target.host, port: target.port });
  tcp.setNoDelay(true);
  const started = Date.now();
  const metrics = { wsBytes: 0, tcpBytes: 0, wsPauses: 0, tcpPauses: 0 };
  let finished = false;
  let dropped = false;
  let entry;
  const finish = (reason, err) => {
    if (finished) return;
    finished = true;
    log(err ? 'warn' : 'info', 'vnc_proxy.connection_closed', {
      reason, duration_ms: Date.now() - started,
      renderer_to_target_bytes: metrics.wsBytes,
      target_to_renderer_bytes: metrics.tcpBytes,
      renderer_backpressure: metrics.wsPauses,
      target_backpressure: metrics.tcpPauses,
      error: err ? errorMeta(err) : null,
    });
  };
  const drop = (reason, err) => {
    if (dropped) return;
    dropped = true;
    finish(reason, err);
    conns.delete(entry);
    // Let the RFB server observe EOF while we drain its pending framebuffer.
    // Resetting a socket with unread data can terminate Lume's guest runner.
    if (!tcp.destroyed) {
      const timer = setTimeout(() => tcp.destroy(), 4000);
      timer.unref();
      tcp.once('close', () => clearTimeout(timer));
      tcp.resume();
      tcp.end();
    }
    try { ws.terminate(); } catch {}
  };
  entry = { ws, tcp, finish, drop };
  conns.add(entry);
  log('info', 'vnc_proxy.connection_opened', { connections: conns.size });

  // ws -> tcp (net queues writes until 'connect', no buffering needed here)
  ws.on('message', (data) => {
    metrics.wsBytes += data && (data.byteLength || data.length) || 0;
    if (!tcp.write(data)) {
      metrics.wsPauses++;
      ws.pause();
      tcp.once('drain', () => ws.resume());
    }
  });
  ws.on('close', () => drop('renderer-close'));
  ws.on('error', (err) => drop('renderer-error', err));

  // tcp -> ws, pausing the TCP side while the WS send queue is backed up
  tcp.on('data', (chunk) => {
    if (finished) { tcp.resume(); return; }
    metrics.tcpBytes += chunk.length;
    ws.send(chunk, () => {
      if (ws.bufferedAmount < HIGH_WATER) tcp.resume();
    });
    if (ws.bufferedAmount >= HIGH_WATER) {
      metrics.tcpPauses++;
      tcp.pause();
    }
  });
  tcp.on('error', (err) => {
    finish('target-error', err);
    conns.delete(entry);
    try { ws.close(1011, `vnc target unreachable (${err.code || 'error'})`); } catch {}
  });
  tcp.on('close', () => {
    finish('target-close');
    conns.delete(entry);
    try { ws.close(1000, 'vnc target closed'); } catch {}
  });
}

async function doStart(targetUrl) {
  const target = parseVncUrl(targetUrl);
  const key = `${target.host}:${target.port}#${target.password}`;
  if (state && state.key === key) {
    log('info', 'vnc_proxy.reused', { connections: state.conns.size });
    return state.info; // same target: reuse
  }
  await doStop(); // different target: tear the old bridge down first

  if (!target.password && !warnedNoPassword) {
    warnedNoPassword = true;
    console.warn('[vnc-proxy] VNC target has no password — the endpoint is unauthenticated');
  }

  // Connect only when the renderer is ready to negotiate RFB. A disposable
  // TCP probe closes the guest's first VNC connection before its handshake.

  // The bridge listens on loopback but any local process could reach it;
  // upgrades must present the per-bridge token (never logged) to ride it.
  const token = crypto.randomBytes(16).toString('hex');
  const wss = new WebSocketServer({
    host: '127.0.0.1',
    port: 0,
    verifyClient: ({ req }) => {
      try {
        return new URL(req.url, 'ws://127.0.0.1').searchParams.get('token') === token;
      } catch {
        return false;
      }
    },
  });
  await new Promise((resolve, reject) => {
    wss.once('listening', resolve);
    wss.once('error', reject);
  });
  const conns = new Set();
  const port = wss.address().port;
  const info = {
    wsUrl: `ws://127.0.0.1:${port}/?token=${token}`,
    password: target.password,
    host: target.host,
    port: target.port,
  };
  state = { key, info, wss, conns };
  wss.on('connection', (ws) => bridge(ws, target, conns));
  wss.on('error', (err) => {
    log('error', 'vnc_proxy.server_error', { error: errorMeta(err) });
    // EventEmitter error listeners suppress the default fatal throw. Make
    // that handling explicit and safe: invalidate/tear down this bridge so a
    // later start cannot reuse a broken WebSocketServer.
    serialize(() => (state && state.wss === wss ? doStop() : undefined))
      .catch((cleanupErr) => log('error', 'vnc_proxy.cleanup_failed', {
        error: errorMeta(cleanupErr),
      }));
  });
  log('info', 'vnc_proxy.ready', {});
  console.log(`[vnc-proxy] bridge ready on loopback port ${port}`);
  return info;
}

function doStop() {
  if (!state) return Promise.resolve();
  const { wss, conns } = state;
  state = null;
  const connectionCount = conns.size;
  for (const { drop } of conns) {
    drop('proxy-stop');
  }
  conns.clear();
  return new Promise((resolve) => wss.close(() => {
    log('info', 'vnc_proxy.stopped', { connections: connectionCount });
    resolve();
  }));
}

function start(targetUrl) {
  return serialize(() => doStart(targetUrl));
}

function stop() {
  return serialize(doStop);
}

function setDiagnostics(value) {
  diagnostics = value || null;
}

module.exports = { start, stop, parseVncUrl, setDiagnostics };
