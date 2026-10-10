/* Offline shell so the app opens instantly and survives a dropped connection.
   Calls still need the network; this only keeps the UI alive. */
const CACHE = "helix-call-v1";
const SHELL = ["/", "/app/app.js", "/app/manifest.webmanifest", "/app/icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // never cache signalling or transcripts
  if (url.pathname.startsWith("/ws") || url.pathname.startsWith("/call") ||
      url.pathname.startsWith("/turn") || url.pathname.startsWith("/calls")) return;
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});