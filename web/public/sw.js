/* Steward phone app service worker: shows push notifications and handles taps.
   It never caches your conversation; the app always loads live from your Mac. */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = { body: event.data && event.data.text() }; }
  const title = data.title || 'Steward';
  const opts = {
    body: data.body || '',
    tag: data.tag || 'steward',
    renotify: true,
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    data: { aid: data.aid || null, url: '/' },
    requireInteraction: !!data.approval,
  };
  // Android shows these as buttons; iPhone ignores them and opens the app on tap.
  if (data.approval) opts.actions = [{ action: 'approve', title: 'Approve' }, { action: 'deny', title: 'Deny' }];
  event.waitUntil(self.registration.showNotification(title, opts));
});

async function openApp() {
  const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  for (const w of wins) {
    if ('focus' in w) { w.postMessage({ type: 'open' }); return w.focus(); }
  }
  return self.clients.openWindow('/');
}

self.addEventListener('notificationclick', (event) => {
  const n = event.notification;
  n.close();
  const aid = n.data && n.data.aid;
  if (aid && (event.action === 'approve' || event.action === 'deny')) {
    event.waitUntil(
      fetch('/approve', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ aid, approved: event.action === 'approve' }),
      }).then((r) => r.json()).then((res) => {
        if (!res.ok) return self.registration.showNotification('Steward', { body: res.error || 'Already answered.', tag: 'approval-result' });
        return self.registration.showNotification('Steward', {
          body: event.action === 'approve' ? 'Approved.' : 'Denied.', tag: 'approval-result',
        });
      }).catch(() => openApp()),
    );
    return;
  }
  event.waitUntil(openApp());
});
