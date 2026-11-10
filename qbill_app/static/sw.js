/* QBill service worker — caches ONLY the app shell so QBill opens offline.
   API requests (/api/...) are never intercepted and never cached: user
   data must always come from the server (or the app's own offline queue),
   never from a stale worker cache. */
var CACHE = "qbill-shell-v17";
var SHELL = [
  "/",
  "/static/app.js",
  "/static/styles.css",
  "/static/manifest.webmanifest",
  "/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/logo-full.png"
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE).then(function (cache) {
      return cache.addAll(SHELL);
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (k) {
        return k === CACHE ? null : caches.delete(k);
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  var url = new URL(event.request.url);
  if (url.pathname.indexOf("/api/") === 0 || url.pathname === "/healthz") {
    return; // never cache API or health responses
  }
  if (event.request.method !== "GET") return;
  event.respondWith(
    caches.match(event.request, { ignoreSearch: true }).then(function (hit) {
      if (hit) return hit;
      return fetch(event.request).then(function (resp) {
        var copy = resp.clone();
        caches.open(CACHE).then(function (cache) { cache.put(event.request, copy); });
        return resp;
      }).catch(function () {
        if (event.request.mode === "navigate") return caches.match("/");
        throw new Error("offline and not cached");
      });
    })
  );
});
