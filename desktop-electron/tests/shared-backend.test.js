'use strict';

const assert = require('node:assert/strict');
const http = require('node:http');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');

const { findExistingBackend, SHARED_BACKEND_PORT } = require('../runtime.js');

function serve(handler) {
  return new Promise((resolve) => {
    const server = http.createServer(handler);
    server.listen(0, '127.0.0.1', () => resolve(server));
  });
}

const portOf = (server) => server.address().port;

test('attaches to a healthy backend that is already running', async () => {
  const server = await serve((req, res) => {
    if (req.url === '/api/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, pid: 4242 }));
      return;
    }
    res.writeHead(404).end();
  });
  try {
    const found = await findExistingBackend({ port: portOf(server) });
    assert.equal(found.pid, 4242);
    assert.equal(found.owned, false, 'a shared server must not be owned by the shell');
    assert.match(found.url, /^http:\/\/127\.0\.0\.1:\d+$/);
  } finally {
    server.close();
  }
});

test('ignores a server whose health payload is not ours', async () => {
  // something else on 8767 must not be mistaken for Serena's backend
  const server = await serve((req, res) => {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ hello: 'not serena' }));
  });
  try {
    assert.equal(await findExistingBackend({ port: portOf(server) }), null);
  } finally {
    server.close();
  }
});

test('ignores a backend that answers unhealthy', async () => {
  const server = await serve((req, res) => {
    res.writeHead(503, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: false }));
  });
  try {
    assert.equal(await findExistingBackend({ port: portOf(server) }), null);
  } finally {
    server.close();
  }
});

test('returns null when nothing is listening, so the shell spawns its own', async () => {
  assert.equal(await findExistingBackend({ port: 9, timeoutMs: 250 }), null);
});

test('sharing can be turned off explicitly', async () => {
  const server = await serve((req, res) => {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, pid: 1 }));
  });
  try {
    const found = await findExistingBackend({ port: portOf(server), enabled: false });
    assert.equal(found, null);
  } finally {
    server.close();
  }
});

test('defaults to the mobile_host port', () => {
  assert.equal(SHARED_BACKEND_PORT, 8767);
});

test('quitting never terminates a backend the shell did not spawn', () => {
  // mobile_host belongs to systemd and to the phone. stopBackend() must only
  // reach for a process tree when this app actually owns one.
  const source = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  const stop = source.slice(source.indexOf('async function stopBackend()'));
  const body = stop.slice(0, stop.indexOf('\n}'));
  assert.match(
    body,
    /if \(child\) await terminateProcessTree\(child\)/,
    'stopBackend must guard the terminate call on owning the child',
  );
});

test('the shared path still creates a window and tray', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  const shared = source.slice(source.indexOf('SERENA_BACKEND_SHARED'));
  const block = shared.slice(0, shared.indexOf('const port = await findFreePort()'));
  assert.match(block, /createWindow\(shared\.url\)/);
  assert.match(block, /createTray\(\)/);
});
