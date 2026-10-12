// Minimal no-op Service Worker for the service-worker.html fixture.
// It has NO fetch handler, so it never intercepts or alters requests — its only
// observable effect is being returned by navigator.serviceWorker.getRegistrations(),
// which is exactly what the navigate() SW-capture test asserts on. skipWaiting +
// clients.claim make it activate promptly so the test doesn't wait on a reload.
self.addEventListener('install', function () { self.skipWaiting(); });
self.addEventListener('activate', function (event) { event.waitUntil(self.clients.claim()); });
