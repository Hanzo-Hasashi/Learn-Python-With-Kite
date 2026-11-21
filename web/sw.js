"use strict";

const CACHE_NAME = "kite-shell-v9";
const CACHE_PREFIX = "kite-shell-";
const APP_SHELL = [
  "/",
  "/manifest.webmanifest",
  "/assets/app.js?v=password-recovery-21",
  "/assets/config.js",
  "/assets/playground-shortcut.js",
  "/assets/playground-shortcut.css",
  "/assets/pwa.js?v=pwa-2",
  "/assets/pwa.css?v=pwa-2",
  "/assets/python_logo.webp",
  "/assets/icons/kite.svg",
  "/assets/icons/kite-192.png",
  "/assets/icons/kite-512.png",
  "/assets/icons/kite-512-maskable.png",
  "/assets/icons/apple-touch-icon.png"
];
const PUBLIC_ASSETS = new Set([
  "/manifest.webmanifest",
  "/assets/app.js?v=password-recovery-21",
  "/assets/config.js",
  "/assets/playground-shortcut.js",
  "/assets/playground-shortcut.css",
  "/assets/pwa.js?v=pwa-2",
  "/assets/pwa.css?v=pwa-2",
  "/assets/python_logo.webp",
  "/assets/icons/kite.svg",
  "/assets/icons/kite-192.png",
  "/assets/icons/kite-512.png",
  "/assets/icons/kite-512-maskable.png",
  "/assets/icons/apple-touch-icon.png"
]);

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(function (cache) { return cache.addAll(APP_SHELL); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (names) {
        return Promise.all(names.map(function (name) {
          if (name.indexOf(CACHE_PREFIX) === 0 && name !== CACHE_NAME) {
            return caches.delete(name);
          }
          return Promise.resolve(false);
        }));
      })
      .then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  const request = event.request;
  if (request.method !== "GET" || request.headers.has("Authorization")) return;

  const url = new URL(request.url);
  if (
    url.origin !== self.location.origin ||
    url.pathname === "/api" ||
    url.pathname.indexOf("/api/") === 0
  ) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(function () {
        return caches.match(request).then(function (cached) {
          return cached || caches.match("/");
        }).then(function (cached) {
          return cached || Response.error();
        });
      })
    );
    return;
  }

  if (!PUBLIC_ASSETS.has(url.pathname + url.search)) return;
  event.respondWith(
    fetch(request).then(function (response) {
      if (!response.ok || response.type !== "basic") return response;
      return caches.open(CACHE_NAME).then(function (cache) {
        return cache.put(request, response.clone());
      }).then(function () {
        return response;
      }).catch(function (error) {
        console.warn("Kite could not cache a public asset for offline use.", error);
        return response;
      });
    }).catch(function () {
      return caches.match(request).then(function (cached) {
        return cached || Response.error();
      });
    })
  );
});
