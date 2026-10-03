import { useCallback, useEffect, useRef, useState } from 'react';

/** WebSocket connection to the agent running on this Mac. */
export function useAgent() {
  const [events, setEvents] = useState([]);
  const [status, setStatus] = useState({ busy: false, label: '' });
  const [tasks, setTasks] = useState([]);
  const [info, setInfo] = useState({ agent: 'Steward', avatar: 'droid', model: '', telegram: false });
  const [connected, setConnected] = useState(false);
  const [lastTool, setLastTool] = useState(null);
  const [memory, setMemory] = useState({ files: [], dir: '' });
  const [prefs, setPrefs] = useState({ phone_mode: 'auto' });
  const [toast, setToast] = useState(null);
  const [models, setModels] = useState({ items: [], active: '', background: '', providers: {} });
  const ws = useRef(null);
  const retry = useRef(0);
  const alive = useRef(true);

  const connect = useCallback(() => {
    const cur = ws.current;
    if (cur && (cur.readyState === WebSocket.OPEN || cur.readyState === WebSocket.CONNECTING)) return;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const sock = new WebSocket(`${proto}://${location.host}/ws`);
    ws.current = sock;

    sock.onopen = () => {
      setConnected(true); retry.current = 0;
      sock.send(JSON.stringify({ type: 'presence', visible: document.visibilityState === 'visible' }));
    };
    sock.onclose = () => {
      setConnected(false);
      if (!alive.current) return;
      const wait = Math.min(1000 * 2 ** retry.current++, 15000);
      setTimeout(connect, wait);
    };
    sock.onmessage = (e) => {
      const ev = JSON.parse(e.data);
      switch (ev.type) {
        case 'hello': setInfo(ev); if (ev.prefs) setPrefs(ev.prefs); break;
        case 'memory': setMemory({ files: ev.files, dir: ev.dir }); break;
        case 'prefs': setPrefs(ev.prefs); break;
        case 'toast': setToast({ ...ev, key: Date.now() }); break;
        case 'models': setModels(ev); break;
        case 'hello_update': setInfo((i) => ({ ...i, ...ev })); break;
        case 'device': setInfo((i) => ({ ...i, device: ev.device })); break;
        case 'model_test_result':
        case 'ollama_tags':
        case 'model_saved':
        case 'local_status':
        case 'local_progress':
        case 'local_log':
        case 'provider_models':
        case 'phone_status':
          window.dispatchEvent(new CustomEvent('agent-' + ev.type, { detail: ev })); break;
        case 'history': setEvents(ev.events); break;
        case 'status':
          setStatus({ busy: ev.busy, label: ev.label });
          if (!ev.busy) setLastTool(null);
          break;
        case 'tasks': setTasks(ev.items); break;
        case 'approval_resolved':
          setEvents((prev) => prev.map((x) =>
            x.type === 'approval' && x.aid === ev.aid ? { ...x, approved: ev.approved } : x));
          break;
        default:
          if (ev.type === 'tool') setLastTool(ev);
          setEvents((prev) => [...prev, ev]);
          window.dispatchEvent(new CustomEvent('agent-event', { detail: ev }));
      }
    };
  }, []);

  useEffect(() => {
    alive.current = true;
    connect();
    // Tell the agent you're at the desk, so heads-ups don't also go to your phone.
    const t = setInterval(() => {
      if (document.hasFocus()) send({ type: 'presence', visible: true });
    }, 60000);
    // Phones suspend the connection in the background; reconnect as soon as the app is back,
    // and tell the agent whether you can see replies (otherwise it sends a notification).
    const onVis = () => {
      const visible = document.visibilityState === 'visible';
      if (visible && ws.current?.readyState !== WebSocket.OPEN && ws.current?.readyState !== WebSocket.CONNECTING) {
        retry.current = 0; connect();
      } else send({ type: 'presence', visible });
    };
    document.addEventListener('visibilitychange', onVis);
    return () => { alive.current = false; clearInterval(t); document.removeEventListener('visibilitychange', onVis); ws.current?.close(); };
  }, [connect]);

  const send = useCallback((msg) => {
    if (ws.current?.readyState === WebSocket.OPEN) ws.current.send(JSON.stringify(msg));
  }, []);

  return { events, status, tasks, info, connected, lastTool, memory, prefs, toast, setToast, models, send };
}

export async function uploadFile(file) {
  const fd = new FormData();
  fd.append('file', file, file.name);
  const r = await fetch('/upload', { method: 'POST', body: fd });
  if (!r.ok) throw new Error('Upload failed');
  return r.json();
}

export async function transcribe(blob) {
  const r = await fetch('/voice', { method: 'POST', body: blob });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || 'Transcription failed');
  return data.text;
}


/* ------------------------------------------------------------ phone push -- */
const b64ToBytes = (b64) => {
  const pad = '='.repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
};

export const isStandalone = () =>
  window.matchMedia?.('(display-mode: standalone)').matches || window.navigator.standalone === true;
export const isIOS = () => /iPhone|iPad|iPod/.test(navigator.userAgent) ||
  (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1 && !window.webkit?.messageHandlers?.steward);

/** Can this phone get notifications right now? 'ok' | 'install' (iPhone: add to Home Screen first) | 'unsupported' */
export function pushSupport() {
  if ('serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window) return 'ok';
  if (isIOS() && !isStandalone()) return 'install';
  return 'unsupported';
}

export async function enablePush(vapid, send) {
  const perm = await Notification.requestPermission();
  if (perm !== 'granted') throw new Error('Notifications are blocked. Allow them in Settings → Notifications.');
  const reg = await navigator.serviceWorker.ready;
  let sub = await reg.pushManager.getSubscription();
  if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(vapid) });
  send({ type: 'push_subscribe', subscription: sub.toJSON() });
}

export async function disablePush(send) {
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    await sub?.unsubscribe();
  } catch { /* nothing to undo */ }
  send({ type: 'push_subscribe', subscription: null });
}
