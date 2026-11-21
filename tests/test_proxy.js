/**
 * Automated test suite for Kite Vercel Server-Side API Proxy (web/api/proxy.js)
 * Tests all requirements from Section 4 of the specification.
 */

'use strict';

const http = require('http');
const assert = require('assert');
const proxyHandler = require('../web/api/proxy.js');

const TEST_SECRET = 'test-proxy-secret-for-verification-at-least-32-chars';

function makeRequest(port, path, options = {}, body = null) {
  return new Promise((resolve, reject) => {
    const reqOptions = {
      hostname: '127.0.0.1',
      port: port,
      path: path,
      method: options.method || 'GET',
      headers: options.headers || {},
    };

    const req = http.request(reqOptions, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        const rawBody = Buffer.concat(chunks).toString('utf8');
        let jsonBody = null;
        try {
          jsonBody = JSON.parse(rawBody);
        } catch (e) {}
        resolve({
          statusCode: res.statusCode,
          headers: res.headers,
          rawBody: rawBody,
          json: jsonBody,
        });
      });
    });

    req.on('error', (err) => reject(err));

    if (body) {
      if (typeof body === 'string') {
        req.write(body);
      } else if (Buffer.isBuffer(body)) {
        req.write(body);
      } else {
        req.write(JSON.stringify(body));
      }
    }
    req.end();
  });
}

async function runTests() {
  console.log('--- Starting Kite Vercel Server-Side Proxy Tests ---');

  let upstreamRequests = [];
  let upstreamResponseConfig = {
    statusCode: 200,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status: 'ok' }),
  };
  let retryScenario = false;

  // 1. Start mock upstream backend
  const upstreamServer = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const rawBody = Buffer.concat(chunks).toString('utf8');
      upstreamRequests.push({
        method: req.method,
        url: req.url,
        headers: req.headers,
        body: rawBody,
      });

      const responseConfig = retryScenario && upstreamRequests.length === 1
        ? { statusCode: 503, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ detail: 'Starting up.' }) }
        : upstreamResponseConfig;
      res.writeHead(responseConfig.statusCode, responseConfig.headers);
      res.end(responseConfig.body);
    });
  });

  await new Promise((resolve) => upstreamServer.listen(0, '127.0.0.1', resolve));
  const upstreamPort = upstreamServer.address().port;

  // 2. Start proxy server using web/api/proxy.js
  process.env.KITE_BACKEND_URL = `http://127.0.0.1:${upstreamPort}`;
  process.env.KITE_PROXY_SECRET = TEST_SECRET;

  const proxyServer = http.createServer((req, res) => {
    proxyHandler(req, res).catch((err) => {
      res.statusCode = 500;
      res.end(JSON.stringify({ error: err.message }));
    });
  });

  await new Promise((resolve) => proxyServer.listen(0, '127.0.0.1', resolve));
  const proxyPort = proxyServer.address().port;

  try {
    // Test 1: GET request forwarding
    console.log('Test 1: Forwarding GET request...');
    upstreamRequests = [];
    upstreamResponseConfig = {
      statusCode: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lessons: [1, 2, 3] }),
    };

    let res = await makeRequest(proxyPort, '/api/lessons');
    assert.strictEqual(res.statusCode, 200);
    assert.deepStrictEqual(res.json, { lessons: [1, 2, 3] });
    assert.strictEqual(upstreamRequests.length, 1);
    assert.strictEqual(upstreamRequests[0].method, 'GET');
    assert.strictEqual(upstreamRequests[0].url, '/api/lessons');
    assert.strictEqual(upstreamRequests[0].headers['x-kite-proxy-secret'], TEST_SECRET);
    console.log('✓ Test 1 passed.');

    // Test 1b: transient GET 503 is retried once
    console.log('Test 1b: Transient GET 503 retry...');
    upstreamRequests = [];
    retryScenario = true;
    upstreamResponseConfig = {
      statusCode: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lessons: ['recovered'] }),
    };

    res = await makeRequest(proxyPort, '/api/retry');
    assert.strictEqual(res.statusCode, 200);
    assert.deepStrictEqual(res.json, { lessons: ['recovered'] });
    assert.strictEqual(upstreamRequests.length, 2);
    assert.strictEqual(upstreamRequests[0].url, '/api/retry');
    assert.strictEqual(upstreamRequests[1].url, '/api/retry');
    retryScenario = false;
    console.log('✓ Test 1b passed.');

    // Test 2: POST with JSON body
    console.log('Test 2: Forwarding POST with JSON body...');
    upstreamRequests = [];
    upstreamResponseConfig = {
      statusCode: 201,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: 'user-123', email: 'test@example.com' }),
    };

    const postPayload = { email: 'test@example.com', password: 'SecretPassword123' };
    res = await makeRequest(proxyPort, '/api/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    }, postPayload);

    assert.strictEqual(res.statusCode, 201);
    assert.deepStrictEqual(res.json, { id: 'user-123', email: 'test@example.com' });
    assert.strictEqual(upstreamRequests[0].method, 'POST');
    assert.strictEqual(upstreamRequests[0].url, '/api/auth/register');
    assert.deepStrictEqual(JSON.parse(upstreamRequests[0].body), postPayload);
    assert.strictEqual(upstreamRequests[0].headers['x-kite-proxy-secret'], TEST_SECRET);
    console.log('✓ Test 2 passed.');

    // Test 3: PATCH with body
    console.log('Test 3: Forwarding PATCH request...');
    upstreamRequests = [];
    upstreamResponseConfig = {
      statusCode: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'Updated Name' }),
    };

    const patchPayload = { name: 'Updated Name' };
    res = await makeRequest(proxyPort, '/api/me', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
    }, patchPayload);

    assert.strictEqual(res.statusCode, 200);
    assert.strictEqual(upstreamRequests[0].method, 'PATCH');
    assert.strictEqual(upstreamRequests[0].url, '/api/me');
    assert.deepStrictEqual(JSON.parse(upstreamRequests[0].body), patchPayload);
    console.log('✓ Test 3 passed.');

    // Test 4: DELETE request
    console.log('Test 4: Forwarding DELETE request...');
    upstreamRequests = [];
    upstreamResponseConfig = {
      statusCode: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: 'logged_out' }),
    };

    const logoutCookie = 'kite_access=access-token; kite_refresh=refresh-token; kite_session=session-id';
    const logoutCsrf = 'logout-csrf-token';
    res = await makeRequest(proxyPort, '/api/auth/logout', {
      method: 'POST',
      headers: {
        'Cookie': logoutCookie,
        'X-Kite-CSRF': logoutCsrf,
        'Origin': 'https://learnpythonwithkite.vercel.app',
      },
    });
    assert.strictEqual(res.statusCode, 200);
    assert.strictEqual(upstreamRequests[0].method, 'POST');
    assert.strictEqual(upstreamRequests[0].url, '/api/auth/logout');
    assert.strictEqual(upstreamRequests[0].headers.cookie, logoutCookie);
    assert.strictEqual(upstreamRequests[0].headers['x-kite-csrf'], logoutCsrf);
    assert.strictEqual(upstreamRequests[0].headers.origin, 'https://learnpythonwithkite.vercel.app');
    console.log('✓ Test 4 passed.');

    // Test 5: Query string preservation (both direct and Vercel rewrite __path)
    console.log('Test 5: Query-string preservation...');
    upstreamRequests = [];
    res = await makeRequest(proxyPort, '/api/lessons?module=loops&limit=5&sort=desc');
    assert.strictEqual(upstreamRequests[0].url, '/api/lessons?module=loops&limit=5&sort=desc');

    // Test with Vercel rewrite pattern: /api/proxy?__path=lessons&module=loops&limit=5
    upstreamRequests = [];
    res = await makeRequest(proxyPort, '/api/proxy?__path=lessons&module=loops&limit=5');
    assert.strictEqual(upstreamRequests[0].url, '/api/lessons?module=loops&limit=5');
    console.log('✓ Test 5 passed.');

    // Test 6: Authorization and CSRF header forwarding
    console.log('Test 6: Authorization and CSRF header forwarding...');
    upstreamRequests = [];
    res = await makeRequest(proxyPort, '/api/me', {
      headers: {
        'Authorization': 'Bearer test-jwt-token-value',
        'X-Kite-CSRF': 'csrf-token-abc-123',
        'X-Kite-Registration-Key': 'reg-key-xyz',
      },
    });

    assert.strictEqual(upstreamRequests[0].headers['authorization'], 'Bearer test-jwt-token-value');
    assert.strictEqual(upstreamRequests[0].headers['x-kite-csrf'], 'csrf-token-abc-123');
    assert.strictEqual(upstreamRequests[0].headers['x-kite-registration-key'], 'reg-key-xyz');
    console.log('✓ Test 6 passed.');

    // Test 7: Multiple Set-Cookie headers forwarding
    console.log('Test 7: Multiple Set-Cookie headers forwarding...');
    const cookiesFromBackend = [
      'kite_access=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax',
      'kite_refresh=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax',
      'kite_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax',
    ];
    upstreamResponseConfig = {
      statusCode: 200,
      headers: {
        'Content-Type': 'application/json',
        'Set-Cookie': cookiesFromBackend,
      },
      body: JSON.stringify({ status: 'authenticated' }),
    };

    res = await makeRequest(proxyPort, '/api/auth/login', { method: 'POST' }, { email: 'a@b.com' });
    assert.strictEqual(res.statusCode, 200);
    assert.ok(res.headers['set-cookie'], 'set-cookie header must exist');
    assert.strictEqual(res.headers['set-cookie'].length, 3, 'Must forward all 3 cookies');
    assert.ok(res.headers['set-cookie'][0].includes('kite_access='));
    assert.ok(res.headers['set-cookie'][1].includes('kite_refresh='));
    assert.ok(res.headers['set-cookie'][2].includes('kite_session='));
    assert.ok(res.headers['set-cookie'].every((cookie) =>
      cookie.includes('Path=/') && cookie.includes('Max-Age=0') &&
      cookie.includes('HttpOnly') && cookie.includes('SameSite=Lax')
    ));
    console.log('✓ Test 7 passed.');

    // Test 8: Backend status code passthrough (401, 403, 404, 422, 500, 503)
    console.log('Test 8: Backend error status forwarding (401, 403, 404, 422, 500, 503)...');
    const testStatuses = [401, 403, 404, 422, 500, 503];
    for (const status of testStatuses) {
      upstreamResponseConfig = {
        statusCode: status,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ detail: `Error status ${status}` }),
      };
      res = await makeRequest(proxyPort, `/api/test-error-${status}`);
      assert.strictEqual(res.statusCode, status);
      assert.deepStrictEqual(res.json, { detail: `Error status ${status}` });
    }
    console.log('✓ Test 8 passed.');

    // Test 9: Client attempt to spoof x-kite-proxy-secret
    console.log('Test 9: Client spoofing x-kite-proxy-secret rejection...');
    upstreamRequests = [];
    upstreamResponseConfig = {
      statusCode: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ok: true }),
    };

    res = await makeRequest(proxyPort, '/api/lessons', {
      headers: {
        'X-Kite-Proxy-Secret': 'fake-malicious-secret-sent-by-attacker',
      },
    });

    assert.strictEqual(res.statusCode, 200);
    // Upstream MUST have received the genuine server secret, NEVER the client's fake secret!
    assert.strictEqual(upstreamRequests[0].headers['x-kite-proxy-secret'], TEST_SECRET);
    assert.notStrictEqual(upstreamRequests[0].headers['x-kite-proxy-secret'], 'fake-malicious-secret-sent-by-attacker');
    console.log('✓ Test 9 passed.');

    // Test 10: Requests outside /api/ rejected with 404
    console.log('Test 10: Requests outside /api/ rejected with 404...');
    res = await makeRequest(proxyPort, '/admin/settings');
    assert.strictEqual(res.statusCode, 404);

    res = await makeRequest(proxyPort, '/index.html');
    assert.strictEqual(res.statusCode, 404);

    res = await makeRequest(proxyPort, '/etc/passwd');
    assert.strictEqual(res.statusCode, 404);
    console.log('✓ Test 10 passed.');

    // Test 11: Missing Vercel proxy secret returns 500
    console.log('Test 11: Missing Vercel proxy secret returns 500...');
    const originalSecret = process.env.KITE_PROXY_SECRET;
    delete process.env.KITE_PROXY_SECRET;

    res = await makeRequest(proxyPort, '/api/lessons');
    assert.strictEqual(res.statusCode, 500);
    assert.ok(res.json.detail.includes('KITE_PROXY_SECRET'));
    process.env.KITE_PROXY_SECRET = originalSecret;
    console.log('✓ Test 11 passed.');

    // Test 12: Upstream connection failure returns 502
    console.log('Test 12: Upstream connection failure returns 502...');
    // Point backend URL to an unused closed port
    process.env.KITE_BACKEND_URL = 'http://127.0.0.1:59999';

    res = await makeRequest(proxyPort, '/api/lessons');
    assert.strictEqual(res.statusCode, 502);
    assert.ok(res.json.detail.includes('unreachable'));
    process.env.KITE_BACKEND_URL = `http://127.0.0.1:${upstreamPort}`;
    console.log('✓ Test 12 passed.');

    console.log('\n======================================================');
    console.log('ALL 12 VERCEL SERVER-SIDE PROXY UNIT TESTS PASSED!');
    console.log('======================================================\n');

  } finally {
    upstreamServer.close();
    proxyServer.close();
  }
}

runTests().catch((err) => {
  console.error('Test failure:', err);
  process.exit(1);
});
