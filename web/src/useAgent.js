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
  const ws = useRef(null);
  const retry = useRef(0);
  const alive = useRef(true);

  const connect = useCallback(() => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const sock = new WebSocket(`${proto}://${location.host}/ws`);
    ws.current = sock;

    sock.onopen = () => { setConnected(true); retry.current = 0; };
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
      if (document.hasFocus()) send({ type: 'presence' });
    }, 60000);
    return () => { alive.current = false; clearInterval(t); ws.current?.close(); };
  }, [connect]);

  const send = useCallback((msg) => {
    if (ws.current?.readyState === WebSocket.OPEN) ws.current.send(JSON.stringify(msg));
  }, []);

  return { events, status, tasks, info, connected, lastTool, memory, prefs, toast, setToast, send };
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
