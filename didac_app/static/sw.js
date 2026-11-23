// Makes Footfall installable on a phone. Pages always come live from the server, so an update to the site is an update to the app.
// Offline, a page shows a short message instead of the browser's error.
self.addEventListener('install', function () { self.skipWaiting(); });
self.addEventListener('activate', function (e) { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', function (e) {
  if (e.request.mode !== 'navigate') return;
  e.respondWith(fetch(e.request).catch(function () {
    return new Response('<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><body style="font-family:system-ui;padding:32px;text-align:center"><h2>No connection</h2><p>Footfall needs the internet. Check your connection and try again.</p>', { headers: { 'Content-Type': 'text/html' } });
  }));
});
