"use strict";
// Service worker of AI Hub: what makes the app installable on the phone.
//
// Only the page's shell is cached, and only as a fallback: the network is
// always tried first, so an update of the app is seen at once. Nothing under
// /api is ever cached (conversations stay on the PC, behind the password).

const CACHE = "aihub-v2";
const SHELL = ["/", "/static/style.css", "/static/app.js", "/static/icon-192.png", "/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((names) => Promise.all(names.filter((n) => n !== CACHE).map((n) => caches.delete(n))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) {
    return;  // straight to the network
  }
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response.ok && SHELL.includes(url.pathname)) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        }
        return response;
      })
      .catch(() => caches.match(event.request).then((hit) => hit || caches.match("/")))
  );
});

// Phone alerts (Web Push). The server encrypts them for this device; the
// browser decrypts them before this code sees them.
self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (err) { data = {}; }
  const title = data.title || "AI Hub";
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
    // The app is in front: it already shows the card or the toast. A test always shows.
    const watching = windows.some((w) => w.focused && w.visibilityState === "visible");
    if (watching && !data.test) return undefined;
    return self.registration.showNotification(title, {
      body: data.body || "", tag: data.tag || "aihub", renotify: data.event === "permission",
      requireInteraction: data.event === "permission", icon: "/static/icon-192.png",
      badge: "/static/icon-192.png", data: { conversation_id: data.conversation_id || null },
    });
  }));
});

// A tap opens the app on the conversation (or brings an open window forward).
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const cid = (event.notification.data || {}).conversation_id;
  const target = cid ? `/?conversation=${encodeURIComponent(cid)}` : "/";
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
    const open = windows.find((w) => new URL(w.url).origin === self.location.origin);
    if (open) {
      open.postMessage({ type: "open-conversation", conversation_id: cid });
      return open.focus();
    }
    return self.clients.openWindow(target);
  }));
});
