// WhatsApp AI Companion — Service Worker
const CACHE_NAME = 'wa-companion-v1';
const STATIC_ASSETS = [
  '/',
  '/manifest.webmanifest',
  '/manifest.json',
  '/app-icon.svg',
  '/app-icon-192.png',
  '/app-icon-512.png',
  '/apple-touch-icon.png',
  '/favicon.svg'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(STATIC_ASSETS).catch((err) => {
        console.warn('[SW] Cache addAll warning:', err);
      });
    }).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            return caches.delete(key);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Bypass caching for APIs, SSE, or POST/DELETE/PATCH mutations
  if (
    event.request.method !== 'GET' ||
    url.pathname.startsWith('/api/') ||
    url.pathname === '/status' ||
    url.pathname === '/health' ||
    url.pathname === '/qr' ||
    url.pathname === '/reset'
  ) {
    return; // Pass directly to network
  }

  // Network-first for HTML, Stale-while-revalidate for static assets
  event.respondWith(
    fetch(event.request)
      .then((networkResponse) => {
        if (networkResponse && networkResponse.status === 200) {
          const responseToCache = networkResponse.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(event.request, responseToCache);
          });
        }
        return networkResponse;
      })
      .catch(() => {
        return caches.match(event.request).then((cachedResponse) => {
          if (cachedResponse) return cachedResponse;
          if (event.request.headers.get('accept')?.includes('text/html')) {
            return caches.match('/');
          }
        });
      })
  );
});
