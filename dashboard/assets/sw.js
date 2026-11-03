// Treasury Flow Radar service worker: exists only so Android Chrome offers "Install".
// It caches nothing; every request goes straight to the network, so the page is never stale.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => event.respondWith(fetch(event.request)));
