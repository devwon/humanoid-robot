const CACHE_NAME = 'remote-cli-v17';
const STATIC_ASSETS = [
    '/',
    '/dashboard',
    '/static/index.html',
    '/static/dashboard.html',
    '/static/dashboard.js',
    '/static/app.js',
    '/static/speech.js',
    '/static/style.css',
    '/static/manifest.json',
    '/static/icon.svg',
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => cache.addAll(STATIC_ASSETS))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys()
            .then(keys => Promise.all(
                keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
            ))
            .then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    // Don't cache WebSocket or API requests
    if (event.request.url.includes('/ws') || event.request.url.includes('/api/')) {
        return;
    }

    event.respondWith(
        fetch(event.request)
            .then(response => {
                const clone = response.clone();
                caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
                return response;
            })
            .catch(() => caches.match(event.request))
    );
});
