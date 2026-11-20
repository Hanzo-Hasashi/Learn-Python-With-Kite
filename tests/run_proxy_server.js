'use strict';

const http = require('http');
const proxyHandler = require('../web/api/proxy.js');

const port = parseInt(process.argv[2] || process.env.PORT || '8992', 10);

const server = http.createServer((req, res) => {
  proxyHandler(req, res).catch((err) => {
    if (!res.headersSent) {
      res.statusCode = 500;
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ error: err.message }));
    }
  });
});

server.listen(port, '127.0.0.1', () => {
  console.log(`KITE_PROXY_LISTENING:${server.address().port}`);
});
