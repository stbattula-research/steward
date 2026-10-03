import { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { BorderBeam } from 'border-beam';
import { BotAvatar } from 'bot-avatars';
import { ThinkingOrb } from 'thinking-orbs';
import { VoiceBeam } from 'voice-glow';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import { disablePush, enablePush, pushSupport, transcribe, uploadFile, useAgent } from './useAgent.js';
import { Icon } from './icons.jsx';
import MemoryPanel from './MemoryPanel.jsx';
import ModelsPanel, { ModelPicker } from './ModelsPanel.jsx';
import PhonePanel from './PhonePanel.jsx';
import { CouncilCard, ModeChip, Related, SourceCards, TeamChip, citeHtml } from './Research.jsx';

marked.setOptions({ breaks: true, gfm: true });

/* ----------------------------------------------------------------- native -- */
// Inside the Steward Mac app, the page can ask the app for native notifications etc.
const native = typeof window !== 'undefined' && window.webkit?.messageHandlers?.steward;
const toNative = (msg) => { try { native?.postMessage(msg); } catch { /* not in the app */ } };

/* ------------------------------------------------------------------ theme -- */
// Appearance: 'auto' follows the Mac; 'light' / 'dark' pin it. The resolved value is
// passed to the libraries.dev components so their glows match the page.
const ThemeCtx = createContext('dark');
const useTheme = () => useContext(ThemeCtx);

function useResolvedTheme(pref) {
  const mq = typeof window !== 'undefined' && window.matchMedia('(prefers-color-scheme: light)');
  const [system, setSystem] = useState(mq && mq.matches ? 'light' : 'dark');
  useEffect(() => {
    if (!mq) return;
    const on = (e) => setSystem(e.matches ? 'light' : 'dark');
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, []);
  const resolved = pref === 'light' || pref === 'dark' ? pref : system;
  useEffect(() => {
    const root = document.documentElement;
    if (pref === 'light' || pref === 'dark') root.dataset.theme = pref; else delete root.dataset.theme;
    try { localStorage.setItem('steward-theme', pref || 'auto'); } catch { /* storage unavailable */ }
    document.querySelector('meta[name=theme-color]')?.setAttribute('content', resolved === 'light' ? '#f7f7f5' : '#121212');
    toNative({ type: 'theme', bg: resolved === 'light' ? '#f7f7f5' : '#121212', dark: resolved !== 'light' });
  }, [pref, resolved]);
  return resolved;
}
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') { node.setAttribute('target', '_blank'); node.setAttribute('rel', 'noreferrer'); }
});

/* ---------------------------------------------------------------- helpers -- */

// Which thinking-orb animation fits what the agent is doing right now.
function orbFor(tool) {
  const n = tool?.name || '';
  if (!n) return 'solving';
  if (n.includes('request_approval')) return 'breathing';
  if (n.includes('schedule') || n.startsWith('mcp__') && !n.startsWith('mcp__browser') && !n.startsWith('mcp__me')) return 'connecting';
  if (n.includes('browser_type') || n.includes('fill_form') || n.includes('press_key')) return 'weaving';
  if (n.includes('screenshot')) return 'shaping';
  if (n.startsWith('mcp__browser') || n.startsWith('mcp__web') || n.startsWith('Web') || ['Read', 'Glob', 'Grep'].includes(n)) return 'searching';
  if (['Write', 'Edit', 'MultiEdit'].includes(n) || n.includes('remember')) return 'composing';
  return 'working';
}

function activityLabel(tool, statusLabel) {
  const n = tool?.name || '';
  if (!n) return statusLabel && statusLabel !== 'Working' ? statusLabel : 'Thinking';
  if (n === 'Bash') return 'Running a command';
  if (n.startsWith('mcp__browser')) return 'Using the browser';
  if (n.startsWith('Web') || n === 'mcp__web__web_search') return 'Searching the web';
  if (n === 'mcp__web__fetch_page') return 'Reading a source';
  if (['Read', 'Glob', 'Grep'].includes(n)) return 'Reading files';
  if (['Write', 'Edit', 'MultiEdit'].includes(n)) return 'Writing';
  if (n.includes('screenshot')) return 'Looking at the screen';
  if (n.includes('request_approval')) return 'Waiting for your approval';
  if (n.includes('schedule')) return 'Scheduling';
  return 'Working';
}

function relTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const diff = (d - Date.now()) / 60000;
  const fmt = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });
  if (Math.abs(diff) < 60) return fmt.format(Math.round(diff), 'minute');
  if (Math.abs(diff) < 60 * 24) return fmt.format(Math.round(diff / 60), 'hour');
  return d.toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

const clock = (iso) => (iso ? new Date(iso).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }) : '');

/** Group consecutive tool calls into one collapsible "steps" row. */
function groupEvents(events) {
  const out = [];
  const councils = new Map();
  const lastAssistant = () => {
    for (let i = out.length - 1; i >= 0; i--) {
      if (out[i].type === 'message' && out[i].role === 'assistant') return i;
      if (out[i].type === 'message' && out[i].role === 'user') return -1;
    }
    return -1;
  };
  for (const ev of events) {
    if (ev.type === 'sources' || ev.type === 'related') {
      const i = lastAssistant();
      if (i >= 0) out[i] = { ...out[i], [ev.type === 'sources' ? 'sources' : 'related']: ev.items };
      const c = [...councils.values()].pop();
      if (ev.type === 'sources' && c && !c.sources) c.sources = ev.items;
      continue;
    }
    if (ev.type === 'council') {
      let c = councils.get(ev.cid);
      if (!c) {
        c = { type: 'council', id: `c-${ev.cid}`, cid: ev.cid, members: ev.members || [], rounds: ev.rounds || 1, entries: {}, phase: ev.phase };
        councils.set(ev.cid, c);
        out.push(c);
      }
      c.phase = ev.phase;
      if (ev.round) c.round = ev.round;
      continue;
    }
    if (ev.type === 'council_member') {
      const c = councils.get(ev.cid);
      if (c) (c.entries[ev.mid] = c.entries[ev.mid] || []).push(ev);
      continue;
    }
    if (ev.type === 'tool') {
      const last = out[out.length - 1];
      if (last?.type === 'steps') last.items.push(ev);
      else out.push({ type: 'steps', id: `s-${ev.id}`, items: [ev] });
    } else out.push(ev);
  }
  return out;
}

/* ------------------------------------------------------------- components -- */

function Markdown({ text, sources }) {
  const html = useMemo(() => DOMPurify.sanitize(citeHtml(marked.parse(text || ''), sources)), [text, sources]);
  return <div className="md" dangerouslySetInnerHTML={{ __html: html }} />;
}

function Steps({ items, live }) {
  const [open, setOpen] = useState(false);
  const last = items[items.length - 1];
  return (
    <div className={`steps ${open ? 'open' : ''}`}>
      <button className="steps-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        {!live && <Icon name="check" size={14} />}
        <span className="steps-count">{items.length} {items.length === 1 ? 'step' : 'steps'}</span>
        {!open && <span className="steps-last">{last.summary.replace(/\s*\n\s*/g, ' ')}</span>}
        <Icon name="chevron" size={14} className="chev" />
      </button>
      {open && (
        <ol className="steps-list">
          {items.map((s) => <li key={s.id}><code>{s.summary}</code></li>)}
        </ol>
      )}
    </div>
  );
}

function Approval({ ev, onAnswer }) {
  const theme = useTheme();
  const pending = ev.approved === undefined;
  const [title, ...rest] = (ev.summary || '').split('\n');
  return (
    <div className="approval-wrap">
      <BorderBeam size="pulse-outside" colorVariant="sunset" theme={theme} active={pending} strength={0.9}>
        <div className={`approval ${pending ? '' : 'done'}`}>
          <div className="approval-head">
            <Icon name="shield" size={16} />
            <span>Approval needed</span>
            <span className="approval-reason">· {title.replace(/^Needs approval:\s*/i, '')}</span>
          </div>
          {rest.join('\n').trim() && <pre className="approval-body">{rest.join('\n').trim()}</pre>}
          {pending ? (
            <div className="approval-actions">
              <button className="btn ghost" onClick={() => onAnswer(ev.aid, false)}>Deny</button>
              <button className="btn primary" onClick={() => onAnswer(ev.aid, true)}>Approve</button>
            </div>
          ) : (
            <div className={`chip ${ev.approved ? 'ok' : 'no'}`}>
              {ev.approved === true ? 'Approved' : ev.approved === false ? 'Denied' : 'Answered on your phone'}
            </div>
          )}
        </div>
      </BorderBeam>
    </div>
  );
}

function FileCard({ ev }) {
  const url = `/files/${ev.fid}`;
  if (ev.image) {
    return (
      <figure className="file-img">
        <a href={url} target="_blank" rel="noreferrer"><img src={url} alt={ev.caption || ev.name} loading="lazy" /></a>
        {ev.caption && <figcaption>{ev.caption}</figcaption>}
      </figure>
    );
  }
  return (
    <a className="file-chip" href={url} download={ev.name}>
      <Icon name="file" size={16} />
      <span>{ev.name}</span>
      {ev.caption && <span className="muted">· {ev.caption}</span>}
      <Icon name="download" size={14} />
    </a>
  );
}

const MODE_TAG = { web: 'Web search', academic: 'Academic', research: 'Deep research' };

function Message({ ev, onAsk }) {
  if (ev.role === 'user') {
    return (
      <div className="msg user">
        <div className="bubble"><Markdown text={ev.text} /></div>
        <div className="meta">
          {ev.team && <span className="tag"><Icon name="users" size={11} /> {ev.team.join(' · ')}</span>}
          {ev.mode && <span className="tag">{MODE_TAG[ev.mode] || ev.mode}</span>}
          {ev.source === 'telegram' && <span className="tag">via Telegram</span>}{ev.source === 'phone' && <span className="tag">from phone</span>}{clock(ev.ts)}
        </div>
      </div>
    );
  }
  if (ev.role === 'system') return <div className="msg system">{ev.text}</div>;
  return (
    <div className="msg assistant">
      <Markdown text={ev.text} sources={ev.sources} />
      <SourceCards sources={ev.sources} text={ev.text} />
      <Related items={ev.related} onPick={onAsk} />
    </div>
  );
}

function EmptyState({ info, onPick, phone }) {
  const theme = useTheme();
  const ideas = [
    "What's using the most space on my Mac?",
    'Take a screenshot and tell me what apps are open',
    'Every weekday at 8am, send me my calendar for the day',
    'What changed in the news about AI this week? Cite sources',
  ];
  return (
    <div className="empty">
      <BotAvatar type={info.avatar} state="default" face="mouth" size={112} theme={theme} />
      <h1>What can I do for you?</h1>
      <p className="muted">{phone ? "I'm on your Mac at home. Ask me to do something there, or try one of these."
        : "I'm running on your Mac. Ask me to do something, or try one of these."}</p>
      <div className="ideas">
        {ideas.map((t) => <button key={t} className="idea" onClick={() => onPick(t)}>{t}</button>)}
      </div>
    </div>
  );
}

function loadLocal(key, fallback) {
  try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch { return fallback; }
}
function saveLocal(key, v) { try { localStorage.setItem(key, JSON.stringify(v)); } catch { /* ignore */ } }

function Composer({ busy, connected, onSend, onStop, models, send, onManageModels, phone }) {
  const theme = useTheme();
  const [mode, setModeState] = useState(() => loadLocal('steward-mode', 'auto'));
  const [team, setTeamState] = useState(() => loadLocal('steward-team', { on: false, members: [], rounds: 1 }));
  const setMode = (m) => { setModeState(m); saveLocal('steward-mode', m); };
  const setTeam = (t) => { setTeamState(t); saveLocal('steward-team', t); };
  const teamIds = (team.members || []).filter((id) => models.items.some((m) => m.id === id));
  const teamOn = team.on && teamIds.length >= 2;
  const [text, setText] = useState('');
  const [files, setFiles] = useState([]);       // {name, path} | {name, uploading:true}
  const [voice, setVoice] = useState('idle');   // idle | recording | transcribing
  const [stream, setStream] = useState(null);
  const [error, setError] = useState('');
  const rec = useRef(null);
  const ta = useRef(null);
  const fileInput = useRef(null);

  useEffect(() => {                              // auto-grow textarea
    const el = ta.current; if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 220) + 'px';
  }, [text]);

  useEffect(() => {
    const onKey = (e) => { if (e.key === '/' && document.activeElement?.tagName !== 'TEXTAREA') { e.preventDefault(); ta.current?.focus(); } };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const ready = files.every((f) => f.path);
  const canSend = connected && ready && (text.trim() || files.length);

  function submit() {
    if (!canSend) return;
    const notes = files.map((f) => `[Attached file saved at: ${f.path}]`).join('\n');
    onSend([text.trim(), notes].filter(Boolean).join('\n\n'), { mode, team: teamOn ? { members: teamIds, rounds: team.rounds || 1 } : null });
    setText(''); setFiles([]); setError('');
  }

  async function addFiles(list) {
    for (const file of list) {
      setFiles((f) => [...f, { name: file.name, uploading: true }]);
      try {
        const res = await uploadFile(file);
        setFiles((f) => f.map((x) => (x.name === file.name && x.uploading ? { name: res.name, path: res.path } : x)));
      } catch {
        setFiles((f) => f.filter((x) => x.name !== file.name));
        setError(`Couldn't attach ${file.name}`);
      }
    }
  }

  async function toggleMic() {
    setError('');
    if (voice === 'recording') { rec.current?.stop(); return; }
    if (voice !== 'idle') return;
    try {
      const s = await navigator.mediaDevices.getUserMedia({ audio: true });
      const chunks = [];
      const r = new MediaRecorder(s);
      r.ondataavailable = (e) => e.data.size && chunks.push(e.data);
      r.onstop = async () => {
        s.getTracks().forEach((t) => t.stop());
        setStream(null);
        setVoice('transcribing');
        try {
          const said = await transcribe(new Blob(chunks, { type: r.mimeType }));
          setText((t) => (t ? `${t} ${said}` : said));
          ta.current?.focus();
        } catch (e) { setError(e.message); }
        setVoice('idle');
      };
      rec.current = r;
      r.start();
      setStream(s);
      setVoice('recording');
    } catch {
      setError(phone ? 'Microphone access was blocked. Allow it for this app in your phone’s Settings.'
        : 'Microphone access was blocked. Allow it in your browser settings.');
    }
  }

  return (
    <div
      className="composer-wrap"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => { e.preventDefault(); addFiles([...e.dataTransfer.files]); }}
    >
      {error && <div className="composer-error">{error}</div>}
      <VoiceBeam stream={stream} processing={voice === 'transcribing'} active={voice !== 'idle'} idle={0} theme={theme} colorVariant="colorful">
        <div className="composer">
          {files.length > 0 && (
            <div className="attachments">
              {files.map((f) => (
                <span key={f.name} className={`att ${f.uploading ? 'loading' : ''}`}>
                  {f.uploading ? <ThinkingOrb state="working" size={20} theme={theme} /> : <Icon name="file" size={14} />}
                  {f.name}
                  {!f.uploading && <button aria-label={`Remove ${f.name}`} onClick={() => setFiles(files.filter((x) => x !== f))}><Icon name="x" size={12} /></button>}
                </span>
              ))}
            </div>
          )}
          <textarea
            ref={ta}
            rows={1}
            value={text}
            placeholder={voice === 'recording' ? 'Listening… tap the mic again to finish' : voice === 'transcribing' ? 'Transcribing…'
              : teamOn ? `Ask the team (${teamIds.length} models)` : mode === 'research' ? 'What should I research?' : 'Ask anything, or ask your agent to do something'}
            onChange={(e) => setText(e.target.value)}
            enterKeyHint="send"
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && !phone) { e.preventDefault(); submit(); } }}
            onPaste={(e) => { const f = [...e.clipboardData.files]; if (f.length) { e.preventDefault(); addFiles(f); } }}
          />
          <div className="composer-bar">
            <div className="left">
              <button className="icon-btn" title="Attach files" onClick={() => fileInput.current.click()}><Icon name="paperclip" /></button>
              <input ref={fileInput} type="file" multiple hidden accept={phone ? 'image/*,video/*,application/pdf,*/*' : undefined} onChange={(e) => { addFiles([...e.target.files]); e.target.value = ''; }} />
              <button className={`icon-btn ${voice === 'recording' ? 'live' : ''}`} title={voice === 'recording' ? 'Stop recording' : 'Voice'} onClick={toggleMic} disabled={voice === 'transcribing'}>
                <Icon name={voice === 'recording' ? 'stop' : 'mic'} />
              </button>
              <ModelPicker models={models} busy={busy} send={send} onManage={onManageModels} />
              <ModeChip mode={mode} setMode={setMode} />
              <TeamChip models={models} team={team} setTeam={setTeam} />
            </div>
            <div className="right">
              {busy && <button className="btn ghost sm" onClick={onStop}><Icon name="stop" size={12} /> Stop</button>}
              <button className="send" disabled={!canSend} onClick={submit} aria-label="Send"><Icon name="arrowUp" /></button>
            </div>
          </div>
        </div>
      </VoiceBeam>
      {!phone && <div className="hint">Enter to send · Shift + Enter for a new line · drop files to attach</div>}
    </div>
  );
}

function Segmented({ label, icon, value, options, onChange }) {
  return (
    <div className="setting">
      <div className="setting-label"><Icon name={icon} size={14} />{label}</div>
      <div className="seg" role="radiogroup" aria-label={label}>
        {options.map((o) => (
          <button key={o.id} role="radio" aria-checked={value === o.id} title={o.tip}
            className={value === o.id ? 'on' : ''} onClick={() => onChange(o.id)}>
            {o.icon && <Icon name={o.icon} size={13} />}{o.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function AppearanceSwitch({ prefs, send }) {
  return (
    <Segmented label="Appearance" icon="contrast" value={prefs.theme || 'auto'}
      onChange={(v) => send({ type: 'set_pref', key: 'theme', value: v })}
      options={[
        { id: 'auto', label: 'Auto', icon: 'auto', tip: 'Follow your Mac (System Settings → Appearance)' },
        { id: 'light', label: 'Light', icon: 'sun', tip: 'Always light' },
        { id: 'dark', label: 'Dark', icon: 'moon', tip: 'Always dark' },
      ]} />
  );
}

function PhoneSwitch({ prefs, send }) {
  const modes = [
    { id: 'auto', label: 'Auto', tip: 'Phone gets heads-ups when you have been away from this app for 10 minutes' },
    { id: 'always', label: 'Always', tip: 'Everything also goes to your phone' },
    { id: 'off', label: 'Off', tip: 'Only replies to messages you send from your phone' },
  ];
  return (
    <div className="setting">
      <div className="setting-label"><Icon name="phone" size={14} />Phone alerts</div>
      <div className="seg" role="radiogroup" aria-label="Phone alerts">
        {modes.map((m) => (
          <button key={m.id} role="radio" aria-checked={prefs.phone_mode === m.id} title={m.tip}
            className={prefs.phone_mode === m.id ? 'on' : ''}
            onClick={() => send({ type: 'set_pref', key: 'phone_mode', value: m.id })}>
            {m.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function Sidebar({ info, status, tasks, connected, send, open, onClose, prefs, onMemory, memoryCount, onModels, modelCount, onPhone, phone }) {
  const theme = useTheme();
  const [confirm, setConfirm] = useState(null);
  const hour = new Date().getHours();
  const avatarState = status.busy ? 'working' : hour >= 23 || hour < 6 ? 'sleeping' : 'default';
  return (
    <aside className={`sidebar ${open ? 'open' : ''}`}>
      <button className="icon-btn close-side" onClick={onClose} aria-label="Close"><Icon name="x" /></button>
      <BorderBeam size="md" colorVariant="ocean" theme={theme} active={status.busy} strength={0.85}>
        <div className="agent-card">
          <BotAvatar type={info.avatar} state={avatarState} face="mouth" size={84} theme={theme} />
          <div className="agent-name">{info.agent}</div>
          <div className="agent-status">
            <span className={`dot ${!connected ? 'off' : status.busy ? 'busy' : 'on'}`} />
            {!connected ? 'Reconnecting…' : status.busy ? (status.label || 'Working') : 'Ready'}
          </div>
          {info.model && <div className="agent-model">{info.model}</div>}
        </div>
      </BorderBeam>

      <div className="section">
        <div className="section-title">Quick actions</div>
        <div className="actions">
          <button className="action" onClick={() => send({ type: 'screen' })}><Icon name="monitor" />Screenshot</button>
          <button className="action" onClick={() => send({ type: 'watch' })}><Icon name="eye" />Check watchlist</button>
          <button className="action" onClick={() => send({ type: 'new' })}><Icon name="plus" />New chat</button>
          <button className="action" onClick={() => send({ type: 'stop' })} disabled={!status.busy}><Icon name="stop" />Stop</button>
        </div>
        {phone ? <PhoneNotify info={info} send={send} /> : (<>
        <button className="memory-btn" onClick={onMemory}>
          <Icon name="book" />
          <span>Memory &amp; playbooks</span>
          <span className="count">{memoryCount}</span>
          <Icon name="chevron" size={14} />
        </button>
        <button className="memory-btn" onClick={onModels}>
          <Icon name="cpu" />
          <span>Models</span>
          <span className="count">{modelCount}</span>
          <Icon name="chevron" size={14} />
        </button>
        <button className="memory-btn" onClick={onPhone}>
          <Icon name="phone" />
          <span>Phone app</span>
          <span className="count">{info.phones || 0}</span>
          <Icon name="chevron" size={14} />
        </button>
        </>)}
      </div>

      <div className="section grow">
        <div className="section-title">Scheduled <span className="count">{tasks.length}</span></div>
        {tasks.length === 0 ? (
          <p className="muted small">Nothing scheduled. Try “remind me at 5pm to…” or “every morning…”.</p>
        ) : (
          <ul className="tasks">
            {tasks.map((t) => (
              <li key={t.id} className="task">
                <div className="task-top">
                  <span className={`mode ${t.mode}`}>{t.mode === 'watch' ? 'Watch' : 'Task'}</span>
                  <span className="task-name">{t.name}</span>
                </div>
                <div className="task-when">{t.next_run ? `Next ${relTime(t.next_run)}` : 'Paused'}{t.cron ? ' · repeats' : ''}</div>
                <button
                  className={`task-x ${confirm === t.id ? 'confirm' : ''}`}
                  onClick={() => (confirm === t.id ? (send({ type: 'cancel_task', id: t.id }), setConfirm(null)) : setConfirm(t.id))}
                  onMouseLeave={() => confirm === t.id && setConfirm(null)}
                  aria-label={`Cancel ${t.name}`}
                >
                  {confirm === t.id ? 'Cancel?' : <Icon name="x" size={12} />}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="side-foot">
        <AppearanceSwitch prefs={prefs} send={send} />
        {(info.telegram || info.phones > 0) && <PhoneSwitch prefs={prefs} send={send} />}
        {!phone && info.telegram && (
          <div className="tg-line"><span className="dot on" />Telegram connected</div>
        )}
      </div>
    </aside>
  );
}

/** Phone only: turn notifications on/off for this phone. */
function PhoneNotify({ info, send }) {
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  const support = pushSupport();
  const on = !!info.device?.push;
  async function toggle() {
    setErr(''); setBusy(true);
    try { if (on) await disablePush(send); else await enablePush(info.vapid, send); }
    catch (e) { setErr(e.message || 'Could not turn on notifications.'); }
    setBusy(false);
  }
  return (
    <div className="phone-notify">
      <div className="setting-label"><Icon name="bell" size={14} />Notifications on this phone</div>
      {support === 'ok' ? (
        <button className={`btn ${on ? 'ghost' : 'primary'} sm`} disabled={busy || !info.vapid} onClick={toggle}>
          {on ? 'Turn off' : 'Turn on'}
        </button>
      ) : support === 'install' ? (
        <p className="muted small">Add {info.agent} to your Home Screen (Share → Add to Home Screen) and open it from there to get notifications.</p>
      ) : <p className="muted small">This browser can’t show notifications. Try Chrome on Android or Safari on iPhone.</p>}
      {err && <p className="composer-error">{err}</p>}
      {info.device && <p className="muted small">Signed in as {info.device.name}. Remove this phone from the Mac app’s Phone app panel.</p>}
    </div>
  );
}

/** Phone only: a one-time nudge to turn on notifications. */
function NotifyBanner({ info, send }) {
  const [hidden, setHidden] = useState(() => { try { return localStorage.getItem('steward-notify-dismissed') === '1'; } catch { return false; } });
  const [err, setErr] = useState('');
  const support = pushSupport();
  if (hidden || info.device?.push || !info.vapid || support === 'unsupported') return null;
  const dismiss = () => { setHidden(true); try { localStorage.setItem('steward-notify-dismissed', '1'); } catch { /* ignore */ } };
  return (
    <div className="notify-banner" role="region" aria-label="Notifications">
      <Icon name="bell" size={16} />
      <div className="nb-text">
        {support === 'install'
          ? <>To get notifications, tap <Icon name="share" size={13} /> <b>Share → Add to Home Screen</b>, then open {info.agent} from there.</>
          : <>Get a notification when {info.agent} replies or needs your OK.</>}
        {err && <div className="composer-error">{err}</div>}
      </div>
      {support === 'ok' && <button className="btn primary sm" onClick={async () => {
        try { await enablePush(info.vapid, send); } catch (e) { setErr(e.message); }
      }}>Turn on</button>}
      <button className="icon-btn" onClick={dismiss} aria-label="Dismiss"><Icon name="x" size={14} /></button>
    </div>
  );
}

function Welcome({ info, models, onLocal, onConnect, onSkip }) {
  const theme = useTheme();
  return (
    <div className="modal-scrim">
      <div className="welcome" role="dialog" aria-label="Welcome">
        <BotAvatar type={info.avatar} state="default" face="mouth" size={96} theme={theme} />
        <h2>Welcome to {info.agent}</h2>
        <p className="muted">Your agent works on this Mac. First, choose what powers it. You can add more models any time.</p>
        <div className="welcome-options">
          <button className="welcome-card" onClick={onLocal}>
            <span className="welcome-tag">Free · private</span>
            <b>Run a model on this Mac</b>
            <span className="muted small">Nothing leaves your computer. Best for everyday tasks. One-click download.</span>
          </button>
          <button className="welcome-card" onClick={onConnect}>
            <span className="welcome-tag accent">Most capable</span>
            <b>Connect an AI provider</b>
            <span className="muted small">Claude, OpenAI, Gemini and more. Paste an API key and pick a model.</span>
          </button>
        </div>
        <button className="link-btn" onClick={onSkip}>{models.items.length ? 'Keep my current model' : 'I’ll do this later'}</button>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------- app -- */

export default function App() {
  const { events, status, tasks, info, connected, lastTool, memory, prefs, toast, setToast, models, send, councilLive } = useAgent();
  const [modelsOpen, setModelsOpen] = useState(false);
  const [modelsTab, setModelsTab] = useState(null);
  const openModels = (tab) => { setModelsTab(tab || null); setModelsOpen(true); };
  const phone = info.client === 'phone';
  const [phoneOpen, setPhoneOpen] = useState(false);
  const welcome = connected && !phone && prefs.onboarded === false && !modelsOpen;
  const finishWelcome = () => send({ type: 'set_pref', key: 'onboarded', value: true });
  const [sideOpen, setSideOpen] = useState(false);
  const [memOpen, setMemOpen] = useState(false);
  const theme = useResolvedTheme(prefs.theme);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 2600);
    return () => clearTimeout(t);
  }, [toast, setToast]);
  const thread = useRef(null);
  const stick = useRef(true);
  const items = useMemo(() => groupEvents(events), [events]);
  const lastSteps = [...items].reverse().find((x) => x.type === 'steps');

  useEffect(() => { document.title = status.busy ? `● ${info.agent}` : info.agent; }, [status.busy, info.agent]);

  useEffect(() => {                         // keep scrolled to bottom unless reading back
    const el = thread.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [items, status.busy]);

  useEffect(() => {                         // desktop notification when the window isn't focused
    const onEv = (e) => {
      const ev = e.detail;
      if (document.hasFocus() || phone) return;      // phones get push notifications instead
      const n = ev.type === 'approval' ? { title: `${info.agent} needs your approval`, body: ev.summary.slice(0, 140) }
        : ev.type === 'message' && ev.role === 'assistant' ? { title: info.agent, body: ev.text.slice(0, 140) } : null;
      if (!n) return;
      if (native) toNative({ type: 'notify', ...n });
      else if ('Notification' in window && Notification.permission === 'granted') new Notification(n.title, { body: n.body });
    };
    window.addEventListener('agent-event', onEv);
    return () => window.removeEventListener('agent-event', onEv);
  }, [info.agent, phone]);

  useEffect(() => { document.documentElement.classList.toggle('phone', phone); }, [phone]);

  useEffect(() => {                         // tapping a notification opens / focuses the app
    if (!('serviceWorker' in navigator)) return;
    const on = (e) => { if (e.data?.type === 'open') { stick.current = true; } };
    navigator.serviceWorker.addEventListener('message', on);
    return () => navigator.serviceWorker.removeEventListener('message', on);
  }, []);

  function sendText(text, opts = {}) {
    if (!native && !phone && 'Notification' in window && Notification.permission === 'default') Notification.requestPermission();
    stick.current = true;
    send({ type: 'send', text, mode: opts.mode || 'auto', team: opts.team || null });
  }

  return (
    <ThemeCtx.Provider value={theme}>
    <div className="app">
      <Sidebar info={info} status={status} tasks={tasks} connected={connected} send={send} open={sideOpen}
        onClose={() => setSideOpen(false)} prefs={prefs}
        onMemory={() => { setMemOpen(true); setSideOpen(false); send({ type: 'memory_get' }); }}
        memoryCount={memory.files.length}
        onModels={() => { openModels(); setSideOpen(false); }} modelCount={models.items.length}
        onPhone={() => { setPhoneOpen(true); setSideOpen(false); }} phone={phone} />
      {phoneOpen && <PhonePanel send={send} agent={info.agent} onClose={() => setPhoneOpen(false)} />}
      {modelsOpen && <ModelsPanel models={models} send={send} initialTab={modelsTab} onClose={() => setModelsOpen(false)} />}
      {welcome && <Welcome info={info} models={models}
        onLocal={() => { finishWelcome(); openModels('local'); }}
        onConnect={() => { finishWelcome(); openModels('connect'); }}
        onSkip={finishWelcome} />}
      {memOpen && <MemoryPanel memory={memory} send={send} onClose={() => setMemOpen(false)} />}
      {toast && <div key={toast.key} className={`toast ${toast.error ? 'err' : ''}`} role="status">{toast.text}</div>}
      {sideOpen && <div className="scrim" onClick={() => setSideOpen(false)} />}
      <main className="main">
        <header className="topbar">
          <button className="icon-btn menu" onClick={() => setSideOpen(true)} aria-label="Open sidebar"><Icon name="menu" /></button>
          <div className="topbar-title">
            <BotAvatar type={info.avatar} state={status.busy ? 'working' : 'default'} size={26} theme={theme} />
            <span>{info.agent}</span>
          </div>
          {!connected && <span className="chip warn">{phone ? 'Can’t reach your Mac…' : 'Reconnecting…'}</span>}
        </header>
        {phone && connected && <NotifyBanner info={info} send={send} />}
        {phone && !connected && <div className="offline-note">Make sure Tailscale is on (on this phone and your Mac) and your Mac is awake.</div>}

        <div className="thread" ref={thread} onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}>
          <div className="thread-inner">
            {items.length === 0 ? (
              <EmptyState info={info} onPick={sendText} phone={phone} />
            ) : (
              items.map((ev) => {
                if (ev.type === 'steps') return <Steps key={ev.id} items={ev.items} live={status.busy && ev === lastSteps && items[items.length - 1] === ev} />;
                if (ev.type === 'message') return <Message key={ev.id} ev={ev} onAsk={(q) => sendText(q)} />;
                if (ev.type === 'council') return <CouncilCard key={ev.id} group={ev} live={councilLive[ev.cid]} busy={status.busy} Markdown={Markdown} />;
                if (ev.type === 'approval') return <Approval key={ev.id} ev={ev} onAnswer={(aid, approved) => send({ type: 'approve', aid, approved })} />;
                if (ev.type === 'file') return <FileCard key={ev.id} ev={ev} />;
                if (ev.type === 'divider') return <div key={ev.id} className="divider"><span>{ev.text}</span></div>;
                return null;
              })
            )}
            {status.busy && (
              <div className="activity" aria-live="polite">
                <ThinkingOrb state={orbFor(lastTool)} size={64} theme={theme} />
                <div>
                  <div className="activity-label">{activityLabel(lastTool, status.label)}…</div>
                  {lastTool && <div className="activity-detail">{lastTool.summary.split('\n').slice(-1)[0].slice(0, 120)}</div>}
                </div>
              </div>
            )}
          </div>
        </div>

        <Composer busy={status.busy} connected={connected} onSend={sendText} onStop={() => send({ type: 'stop' })}
          models={models} send={send} phone={phone}
          onManageModels={phone ? null : () => openModels(models.items.length ? null : 'local')} />
      </main>
    </div>
    </ThemeCtx.Provider>
  );
}
