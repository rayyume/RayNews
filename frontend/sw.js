// RayNews Service Worker
// Cache key includes VERSION + COMMIT_SHA — busted on every build
const CACHE = 'raynews-v{{VERSION}}-{{COMMIT_SHA}}';
const API_CACHE = 'raynews-api-v{{VERSION}}-{{COMMIT_SHA}}';

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

function normalizedApiRequest(request) {
  const url = new URL(request.url);
  url.searchParams.delete('t');
  return new Request(url.toString(), {
    method: 'GET',
    headers: request.headers,
    mode: request.mode,
    credentials: request.credentials,
    redirect: request.redirect,
  });
}

// Marks a cached response served in place of a failed network request. Without
// this, a network failure at the wrong moment (e.g. iOS PWA foreground resume,
// before the network stack is back) is indistinguishable from a real 200 —
// callers like loadSince() would read it as "no new articles" and stay quiet
// instead of retrying. See index.html's isSwFallbackResponse() callers.
function withSwFallbackMarker(cached) {
  const headers = new Headers(cached.headers);
  headers.set('X-SW-Fallback', '1');
  return new Response(cached.body, {
    status: cached.status,
    statusText: cached.statusText,
    headers,
  });
}

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

  // Auth/session mutations must always hit the network.
  if (url.pathname.startsWith('/auth/')) {
    return;
  }

  // ── API list: network-first + background cache (no cold-start delay) ──
  if (url.pathname.startsWith('/api/')) {
    const cacheRequest = normalizedApiRequest(event.request);
    // Article detail: stale-while-revalidate (fast re-opens via SW cache)
    if (/^\/api\/news\/\d+$/.test(url.pathname)) {
      event.respondWith(
        caches.open(API_CACHE).then(cache => {
          return cache.match(cacheRequest).then(cached => {
            const fetchPromise = fetch(event.request).then(network => {
              if (network.ok) {
                const cloned = network.clone();
                cache.put(cacheRequest, cloned).catch(err => {
                  console.warn('SW: article cache.put failed', err);
                });
              }
              return network;
            }).catch(error => { if (cached) return cached; throw error; });
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
          const cloned = network.clone();
          caches.open(API_CACHE).then(cache => {
            cache.put(cacheRequest, cloned).catch(err => {
              console.warn('SW: cache.put failed', err);
            });
          });
        }
        return network;
      }).catch(error => {
        return caches.open(API_CACHE).then(cache => cache.match(cacheRequest)).then(cached => {
          if (cached) return withSwFallbackMarker(cached);
          throw error;
        });
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
            const cloned = network.clone();
            cache.put(event.request, cloned).catch(err => {
              console.warn('SW: asset cache.put failed', err);
            });
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
        const cloned = network.clone();
        return caches.open(CACHE).then(cache => {
          cache.put(event.request, cloned).catch(err => {
            console.warn('SW: nav cache.put failed', err);
          });
          return network;
        });
      }).catch(error => {
        return caches.match(event.request).then(cached => cached || caches.match('/index.html')).then(fallback => {
          if (fallback) return fallback;
          throw error;
        });
      })
    );
    return;
  }

  // ── Everything else: network-first ──
  event.respondWith(
    fetch(event.request).catch(error => caches.match(event.request).then(cached => {
      if (cached) return cached;
      throw error;
    }))
  );
});
