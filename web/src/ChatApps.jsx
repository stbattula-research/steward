import { useEffect, useState } from 'react';
import { Icon } from './icons.jsx';

const APPS = {
  whatsapp: { label: 'WhatsApp', color: '#25D366', letter: 'W', blurb: 'Official WhatsApp Cloud API. Free for messages you start.' },
  imessage: { label: 'iMessage', color: '#34C759', letter: 'i', blurb: 'Uses Messages on this Mac. No account or keys needed.' },
  telegram: { label: 'Telegram', color: '#2AABEE', letter: 'T', blurb: 'A Telegram bot. Set up in Configure Steward.' },
  discord: { label: 'Discord', color: '#5865F2', letter: 'D', blurb: 'A private Discord bot that only answers you.' },
  slack: { label: 'Slack', color: '#E01E5A', letter: 'S', blurb: 'A Slack app you DM. Works in any workspace you own.' },
};
const ORDER = ['whatsapp', 'imessage', 'telegram', 'discord', 'slack'];

function Copy({ text, label = 'Copy' }) {
  const [done, setDone] = useState(false);
  return (
    <button className="btn ghost sm" onClick={async () => {
      try { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1500); } catch { /* blocked */ }
    }}>{done ? 'Copied' : label}</button>
  );
}

function Value({ label, value }) {
  return (
    <div className="kv">
      <span className="muted small">{label}</span>
      <code>{value || '…'}</code>
      {value && <Copy text={value} />}
    </div>
  );
}

function Field({ label, name, form, setForm, placeholder, secret, hint }) {
  return (
    <label className="field">
      <span>{label}</span>
      <input type={secret ? 'password' : 'text'} autoComplete="off" spellCheck={false} placeholder={placeholder}
        value={form[name] || ''} onChange={(e) => setForm({ ...form, [name]: e.target.value })} />
      {hint && <small className="muted">{hint}</small>}
    </label>
  );
}

function Steps({ children }) { return <ol className="app-steps">{children}</ol>; }
const A = ({ href, children }) => <a href={href} target="_blank" rel="noreferrer">{children}</a>;

function Setup({ kind, item, form, setForm, phoneReady, send }) {
  const saved = item?.configured;
  const keep = saved ? 'Saved. Leave empty to keep it.' : undefined;
  if (kind === 'whatsapp') return (
    <>
      {!phoneReady && <p className="app-warn">First turn on <b>Phone app → steps 1 and 2</b> (Tailscale). WhatsApp needs it to deliver messages to this Mac.</p>}
      <Steps>
        <li>Open <A href="https://developers.facebook.com/apps">Meta for Developers</A> → <b>Create app</b> → choose <b>Business</b> type → add the <b>WhatsApp</b> product. Meta gives you a free test number.</li>
        <li>In <b>WhatsApp → API Setup</b>, add <b>your own WhatsApp number</b> as a recipient and verify it.</li>
        <li>Copy the <b>Phone number ID</b>. For an access token that doesn’t expire, go to <b>Business settings → System users</b>, add one, and generate a token with <i>whatsapp_business_messaging</i>. (The temporary token on the API Setup page works for 24 hours.)</li>
        <li>Copy the <b>App secret</b> from <b>App settings → Basic</b>.</li>
      </Steps>
      <Field label="Phone number ID" name="phone_number_id" form={form} setForm={setForm} placeholder={item?.settings?.phone_number_id || '1234567890'} />
      <Field label="Access token" name="access_token" form={form} setForm={setForm} secret placeholder="EAA…" hint={keep} />
      <Field label="App secret" name="app_secret" form={form} setForm={setForm} secret placeholder="32 characters" hint={keep} />
      {saved && (
        <>
          <p className="pane-intro">Then in <b>WhatsApp → Configuration → Webhook</b>, paste these, click <b>Verify and save</b>, and subscribe to <b>messages</b>:</p>
          <Value label="Callback URL" value={item.webhook_url} />
          <Value label="Verify token" value={item.verify_token} />
          {item.funnel?.link && <p className="app-warn">Tailscale needs you to allow public access for this one webhook: <A href={item.funnel.link}>Allow in Tailscale</A></p>}
          {item.funnel?.note && <p className="app-warn">{item.funnel.note}</p>}
          {item.window_note && <p className="app-warn">{item.window_note}</p>}
        </>
      )}
    </>
  );
  if (kind === 'imessage') return (
    <>
      <Steps>
        <li>Make sure <b>Messages</b> on this Mac is signed in with your Apple ID.</li>
        <li>Let Steward read new messages (macOS calls this <b>Full Disk Access</b>):
          {item?.db_ok ? <div className="ok-line">Allowed ✓</div> : (
            <div className="fda">
              <div className="row-btns">
                <button className="btn ghost sm" onClick={() => send({ type: 'conn_open_fda', kind: 'imessage' })}>1. Open Full Disk Access</button>
                <button className="btn ghost sm" onClick={() => send({ type: 'conn_reveal_python', kind: 'imessage' })}>2. Show Steward’s Python in Finder</button>
                <button className="btn ghost sm" onClick={() => send({ type: 'conn_status' })}>3. Check again</button>
              </div>
              <span className="muted small">Drag the highlighted <b>{item?.python_app ? 'Python' : 'python'}</b> from the Finder window into the Full Disk Access list and switch it on. If it still says not allowed, quit and reopen Steward (Start Steward.command).</span>
              <span className="muted small path">{item?.python_app || item?.python}</span>
            </div>
          )}
        </li>
        <li>Enter <b>your own</b> phone number or Apple ID email below and click Connect. The first reply asks to let Steward control Messages: click <b>OK</b>.</li>
        <li>On your iPhone, open Messages and <b>text yourself</b> (start a chat with your own number). Steward answers in that chat.</li>
      </Steps>
      <Field label="Your phone number or Apple ID email" name="handle" form={form} setForm={setForm} placeholder={item?.settings?.handle || '+1 555 123 4567'} />
    </>
  );
  if (kind === 'discord') return (
    <>
      <Steps>
        <li>Open the <A href="https://discord.com/developers/applications">Discord Developer Portal</A> → <b>New Application</b> → <b>Bot</b> → <b>Reset Token</b> → copy it.</li>
        <li>Paste it below and click Connect.</li>
        <li>Add the bot to a server you own (or create a private one: <b>+</b> → <b>Create My Own</b>){item?.invite_url ? <>: <A href={item.invite_url}>Invite your bot</A></> : ''}.</li>
        <li>In Discord, click the bot’s name → <b>Message</b>, and send the pairing code below.</li>
      </Steps>
      <Field label="Bot token" name="token" form={form} setForm={setForm} secret placeholder="MTA…" hint={keep} />
    </>
  );
  if (kind === 'slack') return (
    <>
      <Steps>
        <li>Open <A href="https://api.slack.com/apps?new_app=1">Slack apps</A> → <b>Create New App</b> → <b>From a manifest</b> → pick your workspace → paste this manifest → Create. <Copy text={item?.manifest || ''} label="Copy manifest" /></li>
        <li><b>Install to Workspace</b>, then copy the <b>Bot User OAuth Token</b> (starts with xoxb-).</li>
        <li><b>Basic Information → App-Level Tokens → Generate</b> with the scope <i>connections:write</i>, and copy it (starts with xapp-).</li>
        <li>Paste both below and click Connect. Then open the app in Slack’s sidebar → <b>Messages</b> and send the pairing code.</li>
      </Steps>
      <Field label="Bot token" name="bot_token" form={form} setForm={setForm} secret placeholder="xoxb-…" hint={keep} />
      <Field label="App-level token" name="app_token" form={form} setForm={setForm} secret placeholder="xapp-…" hint={keep} />
    </>
  );
  return null;
}

export default function ChatAppsPanel({ data, send, onClose, phoneReady }) {
  const [sel, setSel] = useState('whatsapp');
  const [form, setForm] = useState({});
  const [confirm, setConfirm] = useState('');
  const [showSetup, setShowSetup] = useState(false);
  useEffect(() => {
    send({ type: 'conn_status' });
    const esc = (e) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', esc);
    return () => window.removeEventListener('keydown', esc);
  }, [send, onClose]);
  useEffect(() => { setForm({}); setConfirm(''); setShowSetup(false); }, [sel]);
  const items = Object.fromEntries((data?.items || []).map((i) => [i.kind, i]));
  const item = items[sel];
  const stateOf = (k) => {
    if (k === 'telegram') return data?.telegram?.connected ? ['on', 'Connected'] : ['off', 'Not set up'];
    const i = items[k];
    if (!i || !i.enabled) return ['off', i?.configured ? 'Off' : 'Not set up'];
    if (i.state === 'error') return ['err', 'Needs attention'];
    if (i.state === 'connecting') return ['busy', 'Connecting…'];
    if (i.state === 'on' && !i.paired) return ['busy', 'Waiting for you to pair'];
    return i.state === 'on' ? ['on', 'Connected'] : ['off', 'Off'];
  };

  return (
    <div className="modal-scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-label="Chat apps">
        <div className="modal-head">
          <div>
            <div className="modal-title">Chat apps</div>
            <div className="muted small">Talk to Steward from the messaging apps you already use. Only you can use it: each app is paired to you.</div>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="Close"><Icon name="x" /></button>
        </div>
        <div className="modal-scroll">
          <div className="tab-pane">
            <div className="app-tiles" role="tablist">
              {ORDER.map((k) => {
                const [st, text] = stateOf(k);
                return (
                  <button key={k} role="tab" aria-selected={sel === k} className={`app-tile ${sel === k ? 'on' : ''}`} onClick={() => setSel(k)}>
                    <span className="app-logo" style={{ background: APPS[k].color }}>{APPS[k].letter}</span>
                    <span className="app-tile-text"><b>{APPS[k].label}</b><span className="muted small"><span className={`dot ${st}`} /> {text}</span></span>
                  </button>
                );
              })}
            </div>

            <section className="app-detail">
              <div className="app-detail-head">
                <span className="app-logo lg" style={{ background: APPS[sel].color }}>{APPS[sel].letter}</span>
                <div><div className="modal-title">{APPS[sel].label}</div><div className="muted small">{APPS[sel].blurb}</div></div>
              </div>

              {sel === 'telegram' ? (
                <div className="pane-intro">
                  {data?.telegram?.connected ? 'Telegram is connected. ' : ''}
                  To set up or change the Telegram bot, double-click <b>Configure Steward.command</b> in the Steward folder. It walks you through BotFather in a minute.
                </div>
              ) : (
                <>
                  {item?.error && <p className="app-err">{item.error}</p>}
                  {item?.enabled && item.state === 'on' && !item.paired && item.claim && (
                    <div className="pair-box">
                      <span className="muted small">Send this pairing code to Steward in {APPS[sel].label}:</span>
                      <span className="pair-code">{item.claim}</span>
                      <span className="muted small">Valid for 30 minutes. Anyone else who messages it is ignored.</span>
                    </div>
                  )}
                  {item?.enabled && item.state === 'on' && item.paired && (
                    <p className="ok-line pane-intro">Connected and paired to you. Try sending “/help”.</p>
                  )}
                  {item?.enabled && item.paired && item.state === 'on' && !showSetup
                    ? <button className="link-btn" style={{ alignSelf: 'flex-start' }} onClick={() => setShowSetup(true)}>Show setup details</button>
                    : <Setup kind={sel} item={item} form={form} setForm={setForm} phoneReady={phoneReady} send={send} />}
                  <div className="mem-actions app-actions">
                    {(!item?.paired || showSetup || !item?.enabled || item.state !== 'on') &&
                    <button className="btn primary" onClick={() => send({ type: 'conn_save', kind: sel, fields: form })}>
                      {item?.enabled ? 'Save and reconnect' : 'Connect'}
                    </button>}
                    {item?.enabled && <button className="btn ghost" onClick={() => send({ type: 'conn_disable', kind: sel })}>Turn off</button>}
                    {item?.paired && item.state === 'on' && <button className="btn ghost" onClick={() => send({ type: 'conn_test', kind: sel })}>Send a test</button>}
                    {sel === 'whatsapp' && item?.configured && !item?.funnel?.on && phoneReady &&
                      <button className="btn ghost" onClick={() => send({ type: 'conn_funnel', kind: sel })}>Retry webhook setup</button>}
                  </div>
                  {item?.configured && (
                    <div className="app-foot">
                      <label className="check">
                        <input type="checkbox" checked={item.alerts !== false} onChange={(e) => send({ type: 'conn_alerts', kind: sel, on: e.target.checked })} />
                        Send scheduled tasks and heads-ups here too
                      </label>
                      <div className="row-btns">
                        {item.paired && sel !== 'imessage' && <button className="link-btn" onClick={() => send({ type: 'conn_unpair', kind: sel })}>Pair a different account</button>}
                        <button className={`link-btn ${confirm === sel ? 'danger' : ''}`}
                          onClick={() => (confirm === sel ? send({ type: 'conn_remove', kind: sel }) : setConfirm(sel))}>
                          {confirm === sel ? 'Click again to remove it and forget its keys' : 'Remove'}
                        </button>
                      </div>
                    </div>
                  )}
                </>
              )}
            </section>
            <p className="muted small">In every app you can send /stop, /new, /status, /tasks or /help, send voice notes, and reply YES or NO to approvals. Keys are kept in your Mac’s Keychain.</p>
          </div>
        </div>
      </div>
    </div>
  );
}
