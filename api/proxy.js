'use strict';

/**
 * Root-level Vercel Serverless Function entry point.
 * Delegates to web/api/proxy.js if Vercel is configured with Root Directory = .
 */
module.exports = require('../web/api/proxy.js');
