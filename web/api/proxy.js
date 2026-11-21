/**
 * Kite Server-Side API Proxy for Vercel
 *
 * Securely proxies /api/* requests from the Vercel frontend to the Render
 * FastAPI backend, injecting the private server-side X-Kite-Proxy-Secret header.
 *
 * This allows all browser requests to remain same-origin on
 * https://learnpythonwithkite.vercel.app/api/..., keeping authentication cookies
 * first-party (Host-Only) and completely eliminating cross-site cookie restrictions,
 * without exposing the proxy secret to the client browser.
 */

'use strict';

const http = require('http');
const https = require('https');
const { URL } = require('url');

const DEFAULT_BACKEND_URL = 'https://kite-backend-958r.onrender.com';
const SUPPORTED_METHODS = new Set(['GET', 'HEAD', 'POST', 'PATCH', 'DELETE', 'OPTIONS']);

const HOP_BY_HOP_REQUEST = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailers',
  'transfer-encoding',
  'upgrade',
  'host',
  'x-kite-proxy-secret', // Strip any client-supplied secret to prevent spoofing
]);

const HOP_BY_HOP_RESPONSE = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailers',
  'transfer-encoding',
  'upgrade',
]);

/**
 * Collect raw body buffer from incoming request stream.
 */
function readRequestBody(req) {
  return new Promise((resolve, reject) => {
    // If Vercel or middleware already parsed the body
    if (req.body !== undefined) {
      if (Buffer.isBuffer(req.body)) {
        return resolve(req.body);
      }
      if (typeof req.body === 'string') {
        return resolve(Buffer.from(req.body, 'utf8'));
      }
      if (typeof req.body === 'object' && req.body !== null) {
        return resolve(Buffer.from(JSON.stringify(req.body), 'utf8'));
      }
    }

    const chunks = [];
    req.on('data', (chunk) => chunks.push(chunk));
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', (err) => reject(err));
  });
}

/**
 * Main Vercel serverless request handler.
 */
async function handler(req, res) {
  const method = (req.method || 'GET').toUpperCase();

  // Validate HTTP method
  if (!SUPPORTED_METHODS.has(method)) {
    res.statusCode = 405;
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Cache-Control', 'private, no-store');
    res.end(JSON.stringify({ detail: 'Method not allowed' }));
    return;
  }

  // Determine target path and query string
  const rawUrl = req.url || '/api';
  const parsedReqUrl = new URL(rawUrl, 'http://localhost');

  let targetPath = '';
  if (parsedReqUrl.pathname === '/api/proxy' || parsedReqUrl.pathname === '/api/proxy/') {
    // Rewritten request: extract subpath from __path parameter
    const subpath = parsedReqUrl.searchParams.get('__path') || '';
    parsedReqUrl.searchParams.delete('__path');
    const remainingQuery = parsedReqUrl.searchParams.toString();

    const normalizedSubpath = subpath ? (subpath.startsWith('/') ? subpath : '/' + subpath) : '';
    targetPath = '/api' + normalizedSubpath + (remainingQuery ? '?' + remainingQuery : '');
  } else {
    // Direct path access
    targetPath = parsedReqUrl.pathname + parsedReqUrl.search;
  }

  // Requirement: Accept ONLY /api/ requests
  if (targetPath !== '/api' && !targetPath.startsWith('/api/')) {
    res.statusCode = 404;
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Cache-Control', 'private, no-store');
    res.end(JSON.stringify({ detail: 'Not found' }));
    return;
  }

  // Verify server-side proxy secret is configured
  const proxySecret = (process.env.KITE_PROXY_SECRET || '').trim();
  if (!proxySecret || proxySecret.length < 32) {
    res.statusCode = 500;
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Cache-Control', 'private, no-store');
    res.end(JSON.stringify({
      detail: 'Proxy configuration error: KITE_PROXY_SECRET must be configured with at least 32 characters on Vercel.'
    }));
    return;
  }

  // Parse upstream backend base URL
  const backendBase = (process.env.KITE_BACKEND_URL || DEFAULT_BACKEND_URL).replace(/\/+$/, '');
  let backendUrl;
  try {
    backendUrl = new URL(targetPath, backendBase);
  } catch (err) {
    res.statusCode = 400;
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Cache-Control', 'private, no-store');
    res.end(JSON.stringify({ detail: 'Invalid request path' }));
    return;
  }

  // Read request body
  let bodyBuffer;
  try {
    bodyBuffer = await readRequestBody(req);
  } catch (err) {
    res.statusCode = 400;
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Cache-Control', 'private, no-store');
    res.end(JSON.stringify({ detail: 'Error reading request body' }));
    return;
  }

  // Build forwarded headers
  const forwardedHeaders = {};
  const clientHeaders = req.headers || {};

  for (const [key, value] of Object.entries(clientHeaders)) {
    const lowerKey = key.toLowerCase();
    if (!HOP_BY_HOP_REQUEST.has(lowerKey)) {
      forwardedHeaders[lowerKey] = value;
    }
  }

  // Inject required upstream headers
  forwardedHeaders['host'] = backendUrl.host;
  forwardedHeaders['x-kite-proxy-secret'] = proxySecret;

  // Forward client IP addresses for accurate backend rate limiting
  const remoteAddress = req.socket && req.socket.remoteAddress ? req.socket.remoteAddress : '';
  const existingXForwardedFor = clientHeaders['x-forwarded-for'];
  if (existingXForwardedFor) {
    forwardedHeaders['x-forwarded-for'] = existingXForwardedFor;
  } else if (remoteAddress) {
    forwardedHeaders['x-forwarded-for'] = remoteAddress;
  }

  if (clientHeaders['x-real-ip']) {
    forwardedHeaders['x-real-ip'] = clientHeaders['x-real-ip'];
  } else if (remoteAddress) {
    forwardedHeaders['x-real-ip'] = remoteAddress;
  }

  // Content-Length calculation
  if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS' && bodyBuffer.length > 0) {
    forwardedHeaders['content-length'] = bodyBuffer.length;
  } else if (method === 'GET' || method === 'HEAD' || method === 'OPTIONS') {
    delete forwardedHeaders['content-length'];
  }

  // Perform request to upstream backend
  const isHttps = backendUrl.protocol === 'https:';
  const transport = isHttps ? https : http;

  const requestOptions = {
    protocol: backendUrl.protocol,
    hostname: backendUrl.hostname,
    port: backendUrl.port || (isHttps ? 443 : 80),
    path: backendUrl.pathname + backendUrl.search,
    method: method,
    headers: forwardedHeaders,
    timeout: 30000, // 30-second upstream timeout
  };

  const upstreamReq = transport.request(requestOptions, (upstreamRes) => {
    // Forward status code
    res.statusCode = upstreamRes.statusCode || 502;

    // Forward response headers
    for (const [key, value] of Object.entries(upstreamRes.headers)) {
      const lowerKey = key.toLowerCase();
      if (HOP_BY_HOP_RESPONSE.has(lowerKey)) {
        continue;
      }
      if (lowerKey === 'set-cookie') {
        // Forward multiple Set-Cookie headers as an array
        res.setHeader('Set-Cookie', value);
      } else if (value !== undefined) {
        res.setHeader(key, value);
      }
    }

    // Always enforce private, no-store on all API proxy responses
    res.setHeader('Cache-Control', 'private, no-store');

    // Pipe upstream response body to client response
    upstreamRes.pipe(res);
  });

  upstreamReq.on('timeout', () => {
    upstreamReq.destroy();
    if (!res.headersSent) {
      res.statusCode = 504;
      res.setHeader('Content-Type', 'application/json');
      res.setHeader('Cache-Control', 'private, no-store');
      res.end(JSON.stringify({ detail: 'Kite backend request timed out.' }));
    }
  });

  upstreamReq.on('error', (err) => {
    if (!res.headersSent) {
      res.statusCode = 502;
      res.setHeader('Content-Type', 'application/json');
      res.setHeader('Cache-Control', 'private, no-store');
      res.end(JSON.stringify({ detail: 'Kite backend is currently unreachable. Please try again shortly.' }));
    }
  });

  // Send request body if applicable
  if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS' && bodyBuffer.length > 0) {
    upstreamReq.write(bodyBuffer);
  }
  upstreamReq.end();
}

module.exports = handler;
