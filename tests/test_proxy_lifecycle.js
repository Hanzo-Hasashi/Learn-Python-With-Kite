'use strict';

const assert = require('node:assert/strict');
const http = require('node:http');
const { test } = require('node:test');
const proxyHandler = require('../web/api/proxy.js');

const PROXY_SECRET = 'test-proxy-secret-with-more-than-32-characters';

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => resolve(server.address().port));
  });
}

function close(server) {
  return new Promise((resolve) => {
    if (!server.listening) return resolve();
    server.close(() => resolve());
    if (server.closeAllConnections) server.closeAllConnections();
  });
}

function startProxy(t, upstreamPort) {
  const previousBackend = process.env.KITE_BACKEND_URL;
  const previousSecret = process.env.KITE_PROXY_SECRET;
  process.env.KITE_BACKEND_URL = `http://127.0.0.1:${upstreamPort}`;
  process.env.KITE_PROXY_SECRET = PROXY_SECRET;

  const proxy = http.createServer((req, res) => {
    proxyHandler(req, res).catch(() => {
      if (!res.headersSent) res.writeHead(500).end();
    });
  });
  const ready = listen(proxy);
  t.after(async () => {
    await close(proxy);
    if (previousBackend === undefined) delete process.env.KITE_BACKEND_URL;
    else process.env.KITE_BACKEND_URL = previousBackend;
    if (previousSecret === undefined) delete process.env.KITE_PROXY_SECRET;
    else process.env.KITE_PROXY_SECRET = previousSecret;
  });
  return ready;
}

function get(port) {
  return new Promise((resolve, reject) => {
    http.get(`http://127.0.0.1:${port}/api/health`, (response) => {
      const chunks = [];
      response.on('data', (chunk) => chunks.push(chunk));
      response.on('end', () => resolve({
        status: response.statusCode,
        headers: response.headers,
        body: Buffer.concat(chunks).toString('utf8'),
      }));
    }).on('error', reject);
  });
}

test('retries one transient GET failure and keeps the response private', async (t) => {
  let requests = 0;
  const upstream = http.createServer((req, res) => {
    requests += 1;
    assert.equal(req.headers['x-kite-proxy-secret'], PROXY_SECRET);
    if (requests === 1) {
      res.writeHead(503).end('temporary');
      return;
    }
    res.writeHead(200).end('ready');
  });
  const upstreamPort = await listen(upstream);
  t.after(() => close(upstream));
  const proxyPort = await startProxy(t, upstreamPort);

  const response = await get(proxyPort);
  assert.equal(response.status, 200);
  assert.equal(response.body, 'ready');
  assert.equal(response.headers['cache-control'], 'private, no-store');
  assert.equal(requests, 2);
});

test('cancels the upstream request when the browser disconnects', async (t) => {
  let requests = 0;
  let resolveUpstreamStarted;
  let resolveUpstreamClosed;
  const upstreamStarted = new Promise((resolve) => { resolveUpstreamStarted = resolve; });
  const upstreamClosed = new Promise((resolve) => { resolveUpstreamClosed = resolve; });
  const upstream = http.createServer((req, res) => {
    requests += 1;
    res.once('close', resolveUpstreamClosed);
    resolveUpstreamStarted();
  });
  const upstreamPort = await listen(upstream);
  t.after(() => close(upstream));
  const proxyPort = await startProxy(t, upstreamPort);

  const clientRequest = http.get(`http://127.0.0.1:${proxyPort}/api/health`);
  clientRequest.on('error', () => {});
  await upstreamStarted;
  clientRequest.destroy();
  await Promise.race([
    upstreamClosed,
    new Promise((_, reject) => setTimeout(() => reject(new Error('upstream was not cancelled')), 1500)),
  ]);
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(requests, 1);
});
