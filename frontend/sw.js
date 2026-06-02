// RayNews Service Worker
// Cache key includes VERSION — auto-busted on each release
const CACHE = 'raynews-v{{VERSION}}';
const API_CACHE = 'raynews-api-v{{VERSION}}';

// Files to pre-cache on install
const PRECACHE = [
  '/',
  '/index.html',
  '/manifest.json',
  '/favicon.ico?v=2',
  '/favicon-32x32.png?v=2',
  '/favicon-16x16.png?v=2',
  '/apple-touch-icon.png?v=2',
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE).then(cache => {
      // Pre-cache app shell (ignore failures for optional assets)
      return Promise.allSettled(
        PRECACHE.map(url => cache.add(url).catch(() => {}))
      );
    }).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => {
      return Promise.all(
        keys.map(key => {
          // Remove old cache versions
          if (key !== CACHE && key !== API_CACHE) {
            return caches.delete(key);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // ── AI endpoints: bypass SW to avoid timeout issues with long-running AI calls ──
  if (url.pathname.startsWith('/ai/')) {
    return;
  }

  // ── API list: network-first + background cache (no cold-start delay) ──
  if (url.pathname.startsWith('/api/')) {
    // Article detail: stale-while-revalidate (fast re-opens via SW cache)
    if (/^\/api\/news\/\d+$/.test(url.pathname)) {
      event.respondWith(
        caches.open(API_CACHE).then(cache => {
          return cache.match(event.request).then(cached => {
            const fetchPromise = fetch(event.request).then(network => {
              if (network.ok) {
                cache.put(event.request, network.clone());
              }
              return network;
            }).catch(() => cached);
            return cached || fetchPromise;
          });
        })
      );
      return;
    }
    // List / other API: network-first (avoids cold-start cache delay)
    event.respondWith(
      fetch(event.request).then(network => {
        if (network.ok) {
          caches.open(API_CACHE).then(cache => cache.put(event.request, network.clone()));
        }
        return network;
      }).catch(() => {
        return caches.open(API_CACHE).then(cache => cache.match(event.request));
      })
    );
    return;
  }

  // ── Static assets: cache-first ──
  if (url.pathname.match(/\.(js|css|png|jpg|jpeg|gif|ico|svg|woff2?)$/)) {
    event.respondWith(
      caches.open(CACHE).then(cache => {
        return cache.match(event.request).then(cached => {
          return cached || fetch(event.request).then(network => {
            cache.put(event.request, network.clone());
            return network;
          });
        });
      })
    );
    return;
  }

  // ── Navigation / HTML: network-first, fallback to cache ──
  if (event.request.mode === 'navigate' || url.pathname === '/' || url.pathname === '/index.html') {
    event.respondWith(
      fetch(event.request).then(network => {
        return caches.open(CACHE).then(cache => {
          cache.put(event.request, network.clone());
          return network;
        });
      }).catch(() => {
        return caches.match(event.request).then(cached => cached || caches.match('/index.html'));
      })
    );
    return;
  }

  // ── Everything else: network-first ──
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
