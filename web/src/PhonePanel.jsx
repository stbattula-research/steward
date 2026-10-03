import { useEffect, useState } from 'react';
import { Icon } from './icons.jsx';
import { useAgentEvent } from './ModelsPanel.jsx';

const ago = (s) => {
  if (!s) return 'never';
  const m = Math.round((Date.now() / 1000 - s) / 60);
  if (m < 2) return 'just now';
  if (m < 60) return `${m} min ago`;
  if (m < 60 * 24) return `${Math.round(m / 60)} h ago`;
  return new Date(s * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
};

function Step({ n, title, done, children, disabled }) {
  return (
    <section className={`pstep ${done ? 'done' : ''} ${disabled ? 'disabled' : ''}`}>
      <div className="pstep-num">{done ? <Icon name="check" size={14} /> : n}</div>
      <div className="pstep-body">
        <div className="pstep-title">{title}</div>
        {children}
      </div>
    </section>
  );
}

/** Mac-side setup for the phone app: Tailscale, phone access, pairing, paired phones. */
export default function PhonePanel({ send, onClose, agent }) {
  const [st, setSt] = useState(null);
  const [confirm, setConfirm] = useState(null);
  const [now, setNow] = useState(Date.now());
  useAgentEvent('agent-phone_status', setSt);

  useEffect(() => {
    send({ type: 'phone_status' });
    const poll = setInterval(() => send({ type: 'phone_status' }), 5000);   // pick up Tailscale sign-in
    const tick = setInterval(() => setNow(Date.now()), 1000);
    const esc = (e) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', esc);
    return () => { clearInterval(poll); clearInterval(tick); window.removeEventListener('keydown', esc); };
  }, [send, onClose]);

  const left = st?.code_expires ? Math.max(0, st.code_expires - Math.floor(now / 1000)) : 0;
  useEffect(() => { if (st?.serving && st?.code && left === 0) send({ type: 'phone_new_code' }); }, [left, st, send]);

  const tsReady = st?.installed && st?.signed_in;

  return (
    <div className="modal-scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal phone-modal" role="dialog" aria-label="Phone app">
        <div className="modal-head">
          <div>
            <div className="modal-title">Phone app</div>
            <div className="muted small">Chat with {agent} from your iPhone or Android phone, wherever you are. Free, private, no app store.</div>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="Close"><Icon name="x" /></button>
        </div>
        <div className="modal-scroll">
          {!st ? <div className="tab-pane muted">Checking…</div> : (
            <div className="tab-pane narrow phone-pane">
              <Step n={1} title="Connect this Mac to Tailscale" done={tsReady}>
                {!st.installed ? (
                  <>
                    <p className="pane-intro">Tailscale is a free app that links your phone and this Mac privately. Nothing is opened to the internet.</p>
                    <div className="row-btns">
                      <a className="btn primary" href={st.download} target="_blank" rel="noreferrer">Get Tailscale for Mac</a>
                      <button className="btn ghost" onClick={() => send({ type: 'phone_status' })}>I’ve installed it</button>
                    </div>
                  </>
                ) : !st.signed_in ? (
                  <>
                    <p className="pane-intro">Open Tailscale and sign in (Google, Apple or Microsoft account is fine).</p>
                    <div className="row-btns">
                      <button className="btn primary" onClick={() => send({ type: 'phone_open_tailscale' })}>Open Tailscale</button>
                      <span className="muted small">This updates by itself once you’re signed in.</span>
                    </div>
                  </>
                ) : <p className="pane-intro ok-line">Connected · <b>{st.host || "this Mac"}</b></p>}
              </Step>

              <Step n={2} title="Turn on phone access" done={st.serving} disabled={!tsReady}>
                {st.serving ? (
                  <div className="row-btns">
                    <span className="pane-intro ok-line">On · only your own devices can reach it</span>
                    <button className="btn ghost sm" onClick={() => send({ type: 'phone_disable' })}>Turn off</button>
                  </div>
                ) : st.enabling ? (
                  st.enable_link ? (
                    <>
                      <p className="pane-intro">Tailscale needs you to allow secure (HTTPS) links for your network. You only do this once.</p>
                      <div className="row-btns">
                        <a className="btn primary" href={st.enable_link} target="_blank" rel="noreferrer">Allow in Tailscale</a>
                        <span className="muted small">Waiting for you…</span>
                      </div>
                    </>
                  ) : <p className="pane-intro">Turning on…</p>
                ) : (
                  <>
                    <p className="pane-intro">Lets your phone reach {agent} through Tailscale.</p>
                    <div className="row-btns">
                      <button className="btn primary" disabled={!tsReady} onClick={() => send({ type: 'phone_enable' })}>Turn on</button>
                    </div>
                  </>
                )}
              </Step>

              <Step n={3} title="Pair your phone" done={false} disabled={!st.serving}>
                {st.serving && st.code ? (
                  <div className="pair-grid">
                    <ol className="pair-how">
                      <li>On your phone, install <b>Tailscale</b> from the App Store or Google Play and sign in with the <b>same account</b>.</li>
                      <li>Point your phone’s camera at this code and open the link.</li>
                      <li><b>iPhone:</b> tap <Icon name="share" size={13} /> Share → <b>Add to Home Screen</b>, then open {agent} from your Home Screen.<br /><b>Android:</b> tap <b>Install</b>.</li>
                      <li>Enter the pairing code:</li>
                    </ol>
                    <div className="pair-qr">
                      <div className="qr" dangerouslySetInnerHTML={{ __html: st.qr_svg || '' }} />
                      <div className="pair-code" aria-label="Pairing code">{st.code}</div>
                      <div className="muted small">
                        {left > 0 ? `Expires in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}` : 'Making a new code…'}
                        {' · '}<button className="link-btn" onClick={() => send({ type: 'phone_new_code' })}>New code</button>
                      </div>
                      <div className="muted small url-line">{st.url}</div>
                    </div>
                  </div>
                ) : <p className="pane-intro muted">Finish the steps above first.</p>}
              </Step>

              <section className="paired">
                <div className="section-title">Paired phones <span className="count">{st.devices.length}</span></div>
                {st.devices.length === 0 ? <p className="muted small">None yet.</p> : (
                  <ul className="device-list">
                    {st.devices.map((d) => (
                      <li key={d.id} className="device">
                        <Icon name="phone" size={16} />
                        <div className="device-text">
                          <b>{d.name}</b>
                          <span className="muted small">Last seen {ago(d.last_seen)} · {d.push ? 'notifications on' : 'notifications off'}</span>
                        </div>
                        <button className={`btn ghost sm ${confirm === d.id ? 'danger' : ''}`}
                          onClick={() => (confirm === d.id ? (send({ type: 'phone_remove', id: d.id }), setConfirm(null)) : setConfirm(d.id))}
                          onMouseLeave={() => confirm === d.id && setConfirm(null)}>
                          {confirm === d.id ? 'Remove?' : 'Remove'}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
                {st.devices.some((d) => d.push) && (
                  <button className="btn ghost sm" onClick={() => send({ type: 'phone_test' })}><Icon name="bell" size={13} /> Send a test notification</button>
                )}
                <p className="muted small">Lost a phone? Remove it here and it’s signed out straight away. Phones can chat and approve, but models, memory and pairing can only be changed on this Mac.</p>
              </section>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
