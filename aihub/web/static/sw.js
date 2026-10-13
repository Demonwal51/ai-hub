"use strict";
// Service worker of AI Hub: what makes the app installable on the phone.
//
// Only the page's shell is cached, and only as a fallback: the network is
// always tried first, so an update of the app is seen at once. Nothing under
// /api is cached (conversations stay on the PC, behind the password) unless
// this device chose « Hors connexion » (Menu): then the conversation list and
// the conversations opened are kept in a cache of their own, served only when
// the PC does not answer (the page reads it, never writes), and dropped when
// the choice is undone or at the logout.

const CACHE = "aihub-v12";
const OFFLINE = "aihub-hors-ligne";
const SHELL = ["/", "/static/style.css", "/static/markdown.js", "/static/app.js", "/static/icon-192.png", "/manifest.webmanifest"];
// What may be read without the PC: the lists, a conversation, the state the page starts with.
const READABLE = [/^\/api\/auth\/session$/, /^\/api\/options$/, /^\/api\/conversations$/, /^\/api\/projects$/,
  /^\/api\/conversations\/\d+$/, /^\/api\/agents$/, /^\/api\/quota$/, /^\/api\/status$/];
const ON = "/__aihub-hors-ligne-active";
const PERSON = "/__aihub-hors-ligne-compte";

async function offlineOn() {
  const cache = await caches.open(OFFLINE);
  const marker = await cache.match(ON);
  return marker ? await marker.text() : null;
}

self.addEventListener("message", (event) => {
  const kind = event.data && event.data.type;
  if (kind === "offline-on") {
    event.waitUntil(caches.open(OFFLINE).then((cache) => cache.put(ON, new Response(crypto.randomUUID()))));
  } else if (kind === "offline-off") {
    event.waitUntil(caches.delete(OFFLINE));  // the choice undone, or the logout: nothing stays
  }
});

async function readable(request, url) {
  // Bind the request to the person/epoch present when it was sent, not
  // whichever person happens to be signed in when its response arrives.
  const requestedEpoch = await offlineOn();
  try {
    const response = await fetch(request);
    const samePerson = requestedEpoch !== null && await offlineOn() === requestedEpoch;
    if (samePerson && response.ok && url.pathname === "/api/auth/session") {
      const session = await response.clone().json();
      if (!session.logged_in) {
        await caches.delete(OFFLINE);
      } else if (await offlineOn()) {
        const cache = await caches.open(OFFLINE);
        const identity = JSON.stringify([session.required, session.user]);
        const previous = await cache.match(PERSON);
        if (previous && await previous.text() !== identity) {
          await caches.delete(OFFLINE);
        } else {
          await cache.put(PERSON, new Response(identity));
        }
      }
    }
    if (samePerson && response.status === 401) {
      await caches.delete(OFFLINE);
    } else if (samePerson && (response.status === 403 || response.status === 404) && /^\/api\/conversations\/\d+$/.test(url.pathname)) {
      const cache = await caches.open(OFFLINE);
      for (const stored of await cache.keys()) {
        if (new URL(stored.url).pathname === url.pathname) await cache.delete(stored);
      }
    }
    const epoch = await offlineOn();
    if (response.ok && epoch && epoch === requestedEpoch) {
      const copy = response.clone();
      const cache = await caches.open(OFFLINE);
      if (await offlineOn() === epoch) await cache.put(request, copy);
    }
    return response;
  } catch (err) {
    const hit = await offlineOn() ? await (await caches.open(OFFLINE)).match(request) : null;
    if (!hit) {
      return new Response(JSON.stringify({ detail: "hors connexion : le PC ne répond pas", code: "offline" }),
        { status: 503, headers: { "Content-Type": "application/json", "X-AIHub-Hors-Ligne": "1" } });
    }
    // Said to the page: what it shows comes from this device, not from the PC.
    const headers = new Headers(hit.headers);
    headers.set("X-AIHub-Hors-Ligne", "1");
    return new Response(await hit.blob(), { status: hit.status, headers });
  }
}

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((names) => Promise.all(names.filter((n) => n !== CACHE && n !== OFFLINE).map((n) => caches.delete(n))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;  // straight to the network
  if (url.pathname.startsWith("/api/")) {
    if (READABLE.some((pattern) => pattern.test(url.pathname))) event.respondWith(readable(event.request, url));
    return;  // anything else of the API: the network only
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
