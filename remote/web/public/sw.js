const BUILD = "__CODEX_LITE_BUILD_ID__";
const CACHE = `codex-lite-remote-${BUILD}`;
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)));
    await self.clients.claim();
    const clients = await self.clients.matchAll({ type: "window" });
    await Promise.all(clients.map((client) => client.navigate(client.url)));
  })());
});
self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || url.pathname === "/firebase-config.json") return;
  const networkRequest = request.mode === "navigate" ? new Request(request, { cache: "no-store" }) : request;
  event.respondWith(fetch(networkRequest).then((response) => {
    if (response.ok) caches.open(CACHE).then((cache) => cache.put(request, response.clone()));
    return response;
  }).catch(async () => await caches.match(request) || await caches.match("/") || new Response("offline", { status: 503 })));
});
