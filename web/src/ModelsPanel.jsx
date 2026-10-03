import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Icon } from './icons.jsx';

const GROUPS = ['On this Mac', 'Claude-compatible', 'OpenAI-compatible', 'Other'];
const EMPTY = { id: '', provider: 'ollama', label: '', model: '', base_url: '', context_tokens: '', api_key: '' };

export function useAgentEvent(name, fn) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    const h = (e) => ref.current(e.detail);
    window.addEventListener(name, h);
    return () => window.removeEventListener(name, h);
  }, [name]);
}

/** Small model chip + menu in the chat box: pick which model to chat with. */
export function ModelPicker({ models, busy, send, onManage }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState(null);
  const box = useRef(null);
  const menu = useRef(null);
  const active = models.items.find((m) => m.id === models.active);

  // The chat box clips its contents, so the menu is rendered at the top level and
  // positioned just above the chip.
  function toggle() {
    if (!open) {
      const r = box.current.getBoundingClientRect();
      setPos({ left: Math.max(8, Math.min(r.left, window.innerWidth - 308)), bottom: window.innerHeight - r.top + 8 });
    }
    setOpen(!open);
  }

  useEffect(() => {
    if (!open) return;
    const close = (e) => { if (!box.current?.contains(e.target) && !menu.current?.contains(e.target)) setOpen(false); };
    const esc = (e) => e.key === 'Escape' && setOpen(false);
    window.addEventListener('pointerdown', close);
    window.addEventListener('keydown', esc);
    return () => { window.removeEventListener('pointerdown', close); window.removeEventListener('keydown', esc); };
  }, [open]);

  if (models.items.length === 0) {
    return (
      <div className="model-picker">
        <button className="model-chip setup" onClick={onManage || undefined} disabled={!onManage}
          title={onManage ? '' : 'Set up a model in the Steward app on your Mac'}>
          <Icon name="plus" size={12} /><span className="model-chip-label">{onManage ? 'Set up a model' : 'No model yet (set up on Mac)'}</span>
        </button>
      </div>
    );
  }

  return (
    <div className="model-picker" ref={box}>
      <button className="model-chip" onClick={toggle} aria-haspopup="menu" aria-expanded={open}
        title={busy ? 'Finish or stop the current task to switch models' : 'Choose the model for this chat'}>
        <span className={`model-dot ${active?.local ? 'local' : 'cloud'}`} />
        <span className="model-chip-label">{active?.label || 'Model'}</span>
        <Icon name="chevron" size={12} className="chev-down" />
      </button>
      {open && pos && createPortal(
        <div className="model-menu" role="menu" ref={menu} style={{ left: pos.left, bottom: pos.bottom }}>
          <div className="model-menu-title">Chat with</div>
          {models.items.map((m) => (
            <button key={m.id} role="menuitemradio" aria-checked={m.id === models.active}
              className={`model-item ${m.id === models.active ? 'on' : ''}`} disabled={busy && m.id !== models.active}
              onClick={() => { if (m.id !== models.active) send({ type: 'model_select', id: m.id }); setOpen(false); }}>
              <span className={`model-dot ${m.local ? 'local' : 'cloud'}`} />
              <span className="model-item-text">
                <span className="model-item-label">{m.label}</span>
                <span className="model-item-sub">{m.provider_label}{m.model && m.model !== m.label ? ` · ${m.model}` : ''}</span>
              </span>
              {m.id === models.active && <Icon name="check" size={14} />}
            </button>
          ))}
          {busy && <div className="model-menu-note">Finish or stop the current task to switch.</div>}
          {onManage && <>
          <div className="model-menu-sep" />
          <button className="model-item manage" onClick={() => { setOpen(false); onManage(); }}>
            <Icon name="plus" size={14} /> <span>Add or manage models…</span>
          </button>
          </>}
        </div>, document.body,
      )}
    </div>
  );
}

/** "My models" tab: edit, test and remove models (advanced). */
function MyModels({ models, send, onClose, startNew }) {
  const [selected, setSelected] = useState(startNew ? 'new' : (models.active || 'new'));
  const [form, setForm] = useState(EMPTY);
  const [background, setBackground] = useState(false);
  const [test, setTest] = useState(null);          // {state: 'running'|'ok'|'fail', text}
  const [tags, setTags] = useState([]);
  const [confirmDel, setConfirmDel] = useState(false);
  const providers = models.providers || {};
  const prov = providers[form.provider] || {};
  const isNew = !form.id;

  // load the selected model into the form
  useEffect(() => {
    const m = models.items.find((x) => x.id === selected);
    if (m) {
      setForm({ ...EMPTY, ...m, context_tokens: m.context_tokens || '', api_key: '' });
      setBackground(models.background === m.id);
    } else {
      setForm(EMPTY);
      setBackground(false);
    }
    setTest(null);
    setConfirmDel(false);
  }, [selected, models]);

  useEffect(() => { if (form.provider === 'ollama') send({ type: 'ollama_tags' }); }, [form.provider, send]);
  useAgentEvent('agent-ollama_tags', (ev) => setTags(ev.names || []));
  useAgentEvent('agent-model_test_result', (ev) => setTest({ state: ev.ok ? 'ok' : 'fail', text: ev.text }));
  useAgentEvent('agent-model_saved', (ev) => setSelected(ev.id));

  const grouped = useMemo(() => GROUPS.map((g) => [g, Object.entries(providers).filter(([, p]) => p.group === g)]), [providers]);
  const suggestions = form.provider === 'ollama' ? tags : (prov.examples || []);
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  const payload = () => ({ ...form, context_tokens: Number(form.context_tokens) || 0 });

  return (
        <div className="modal-body">
          <nav className="mem-nav">
            {models.items.map((m) => (
              <button key={m.id} className={`mem-item model-nav ${m.id === selected ? 'active' : ''}`} onClick={() => setSelected(m.id)}>
                <span className={`model-dot ${m.local ? 'local' : 'cloud'}`} />
                <span className="model-nav-text">
                  <span>{m.label}</span>
                  <span className="model-item-sub">
                    {m.id === models.active ? 'Chatting' : ''}{m.id === models.active && m.id === models.background ? ' · ' : ''}{m.id === models.background ? 'Scheduled tasks' : ''}
                    {m.id !== models.active && m.id !== models.background ? m.provider_label : ''}
                  </span>
                </span>
              </button>
            ))}
            <button className={`mem-new add-model ${selected === 'new' ? 'active' : ''}`} onClick={() => setSelected('new')}>
              <Icon name="plus" size={16} /> Add a model
            </button>
          </nav>

          <section className="mem-editor model-form">
            <label className="field">
              <span>Provider</span>
              <select value={form.provider} onChange={(e) => setForm({ ...form, provider: e.target.value, base_url: '' })}>
                {grouped.map(([g, list]) => list.length > 0 && (
                  <optgroup key={g} label={g}>
                    {list.map(([id, p]) => <option key={id} value={id}>{p.label}</option>)}
                  </optgroup>
                ))}
              </select>
              {prov.help && <small className="muted">{prov.help}</small>}
            </label>

            <div className="field-row">
              <label className="field">
                <span>Model ID{form.provider === 'anthropic' ? ' (optional)' : ''}</span>
                <input list="model-suggestions" value={form.model} onChange={set('model')} spellCheck={false}
                  placeholder={form.provider === 'ollama' ? (tags[0] || 'e.g. a model from `ollama list`') : (prov.examples?.[0] || 'Copy it from the provider’s model list')} />
                <datalist id="model-suggestions">{suggestions.map((t) => <option key={t} value={t} />)}</datalist>
                {prov.models_url && <small><a href={prov.models_url} target="_blank" rel="noreferrer">See available models ↗</a></small>}
              </label>
              <label className="field">
                <span>Display name</span>
                <input value={form.label} onChange={set('label')} placeholder={form.model || prov.label || 'My model'} />
              </label>
            </div>

            {(form.provider.startsWith('custom') || form.base_url) && (
              <label className="field">
                <span>Base URL</span>
                <input value={form.base_url} onChange={set('base_url')} spellCheck={false}
                  placeholder={form.provider === 'custom-openai' ? 'https://api.example.com/v1' : 'https://api.example.com'} />
              </label>
            )}

            {prov.key && (
              <label className="field">
                <span>API key</span>
                <input type="password" autoComplete="off" value={form.api_key} onChange={set('api_key')}
                  placeholder={!isNew && models.items.find((m) => m.id === form.id)?.has_key ? '•••••••• saved in Keychain (type to replace)' : 'Paste your API key'} />
                {prov.keys_url && <small><a href={prov.keys_url} target="_blank" rel="noreferrer">Get a key ↗</a></small>}
              </label>
            )}

            <details className="advanced">
              <summary>Advanced</summary>
              <label className="field">
                <span>Context size (tokens)</span>
                <input type="number" min="0" step="1024" value={form.context_tokens} onChange={set('context_tokens')}
                  placeholder={prov.ctx ? String(prov.ctx) : 'Leave blank unless long tasks run out of memory'} />
                <small className="muted">How much the model can hold at once. Lets Steward summarise long tasks before they overflow.</small>
              </label>
            </details>

            <label className="check">
              <input type="checkbox" checked={background} onChange={(e) => setBackground(e.target.checked)} />
              <span>Use for scheduled tasks and heads-ups</span>
            </label>

            {test && (
              <div className={`test-result ${test.state}`}>
                {test.state === 'running' ? 'Testing…' : test.text}
              </div>
            )}

            <div className="mem-actions">
              {!isNew && models.items.length > 1 && (
                <button className={`btn ghost sm ${confirmDel ? 'danger' : ''}`}
                  onClick={() => (confirmDel ? (send({ type: 'model_delete', id: form.id }), setSelected(models.active)) : setConfirmDel(true))}>
                  {confirmDel ? 'Click again to remove' : 'Remove'}
                </button>
              )}
              <span className="grow-text" />
              <button className="btn ghost sm" onClick={() => { setTest({ state: 'running' }); send({ type: 'model_test', model: payload() }); }}>
                Test connection
              </button>
              <button className="btn ghost sm" onClick={() => send({ type: 'model_save', model: payload(), background })}>
                {isNew ? 'Add model' : 'Save'}
              </button>
              {!isNew && form.id !== models.active && (
                <button className="btn primary sm" onClick={() => { send({ type: 'model_select', id: form.id }); onClose(); }}>
                  Chat with this model
                </button>
              )}
            </div>
          </section>
        </div>
  );
}


/* ------------------------------------------------------------------------ */
/* Connect a provider: pick a card, paste a key, choose from the model list.  */
/* ------------------------------------------------------------------------ */
const CARD_ORDER = ['anthropic', 'openai', 'gemini', 'openrouter', 'deepseek', 'groq', 'mistral', 'nvidia', 'ollama-cloud', 'custom-anthropic', 'custom-openai'];
const CARD_BLURB = {
  anthropic: 'Claude. Best at long, multi-step tasks.',
  openai: 'GPT models from OpenAI.',
  gemini: 'Google’s models. Has a free tier.',
  openrouter: 'One key for hundreds of models.',
  deepseek: 'Capable and low-cost.',
  groq: 'Very fast open models.',
  mistral: 'European models.',
  nvidia: 'Open models on NVIDIA’s cloud.',
  'ollama-cloud': 'Big open models, hosted.',
  'custom-anthropic': 'Any Claude-format endpoint.',
  'custom-openai': 'Any OpenAI-format endpoint.',
};

function ConnectProvider({ models, send, onDone }) {
  const providers = models.providers || {};
  const [pid, setPid] = useState(null);
  const [key, setKey] = useState('');
  const [base, setBase] = useState('');
  const [phase, setPhase] = useState('key');        // key | loading | pick
  const [error, setError] = useState('');
  const [list, setList] = useState([]);
  const [query, setQuery] = useState('');
  const [chosen, setChosen] = useState('');
  const [manual, setManual] = useState(false);
  const [background, setBackground] = useState(false);
  const p = providers[pid] || {};
  const custom = pid?.startsWith('custom');

  useAgentEvent('agent-provider_models', (ev) => {
    if (ev.provider !== pid) return;
    if (ev.ok) { setList(ev.items); setPhase('pick'); setError(''); setManual(false); }
    else { setError(ev.error); setPhase('key'); }
  });
  useAgentEvent('agent-model_saved', () => onDone());

  function choose(id) { setPid(id); setKey(''); setBase(''); setPhase('key'); setError(''); setList([]); setQuery(''); setChosen(''); setManual(false); }
  function check() {
    if (!key.trim()) { setError('Paste your API key first.'); return; }
    if (custom && !/^https?:\/\//.test(base)) { setError('Enter the base URL, starting with https://'); return; }
    setPhase('loading'); setError('');
    send({ type: 'provider_models', provider: pid, api_key: key.trim(), base_url: base.trim() });
  }
  function save() {
    if (!chosen.trim()) { setError('Choose a model.'); return; }
    send({ type: 'model_save', use_now: true, background,
           model: { provider: pid, model: chosen.trim(), label: '', base_url: base.trim(), api_key: key.trim() } });
  }

  const filtered = list.filter((m) => m.id.toLowerCase().includes(query.toLowerCase())).slice(0, 200);

  if (!pid) {
    return (
      <div className="tab-pane">
        <p className="pane-intro">Pick the AI service you have an account with. You’ll paste an API key, then choose a model from its list.</p>
        <div className="provider-grid">
          {CARD_ORDER.filter((id) => providers[id]).map((id) => (
            <button key={id} className="provider-card" onClick={() => choose(id)}>
              <span className="provider-name">{providers[id].label}</span>
              <span className="provider-blurb">{CARD_BLURB[id]}</span>
            </button>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="tab-pane narrow">
      <button className="back-link" onClick={() => setPid(null)}><Icon name="chevron" size={12} className="chev-back" /> All providers</button>
      <h3 className="pane-title">Connect {p.label}</h3>
      {p.help && <p className="muted small">{p.help}</p>}

      <ol className="steps-guide">
        {p.keys_url && (
          <li>
            <span className="step-n">1</span>
            <div><div>Get an API key from {p.label}</div>
              <a className="btn ghost sm" href={p.keys_url} target="_blank" rel="noreferrer">Open {p.label} ↗</a>
            </div>
          </li>
        )}
        <li>
          <span className="step-n">{p.keys_url ? 2 : 1}</span>
          <div className="grow">
            {custom && (
              <label className="field"><span>Base URL</span>
                <input value={base} onChange={(e) => setBase(e.target.value)} placeholder={pid === 'custom-openai' ? 'https://api.example.com/v1' : 'https://api.example.com'} spellCheck={false} />
              </label>
            )}
            <label className="field"><span>Paste your API key</span>
              <input type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && check()} placeholder="It’s stored in your Mac’s Keychain" />
            </label>
            {phase !== 'pick' && (
              <button className="btn primary sm" onClick={check} disabled={phase === 'loading'}>
                {phase === 'loading' ? 'Checking…' : 'Continue'}
              </button>
            )}
          </div>
        </li>
        {(phase === 'pick' || manual) && (
          <li>
            <span className="step-n">{p.keys_url ? 3 : 2}</span>
            <div className="grow">
              <div>Choose a model</div>
              {!manual ? (
                <>
                  <input className="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={`Search ${list.length} models`} />
                  <div className="model-list" role="listbox">
                    {filtered.map((m) => (
                      <button key={m.id} role="option" aria-selected={chosen === m.id} className={`model-row ${chosen === m.id ? 'on' : ''}`} onClick={() => setChosen(m.id)}>
                        <span>{m.id}</span>{m.note && <span className={`note ${m.note === 'free' ? 'free' : ''}`}>{m.note}</span>}
                      </button>
                    ))}
                  </div>
                </>
              ) : (
                <label className="field"><span>Model ID</span>
                  <input value={chosen} onChange={(e) => setChosen(e.target.value)} spellCheck={false}
                    placeholder={(p.examples || [])[0] || 'From the provider’s model list'} />
                </label>
              )}
              <label className="check"><input type="checkbox" checked={background} onChange={(e) => setBackground(e.target.checked)} />
                <span>Also use it for scheduled tasks and heads-ups</span></label>
              <button className="btn primary" onClick={save}>Add and start chatting</button>
            </div>
          </li>
        )}
      </ol>
      {error && (
        <div className="test-result fail">
          {error}
          {!manual && <> <button className="link-btn" onClick={() => { setManual(true); setError(''); }}>Type a model ID instead</button></>}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Local models: install Ollama, download recommended models, remove them.    */
/* ------------------------------------------------------------------------ */
function LocalModels({ models, send }) {
  const [st, setSt] = useState(null);
  const [progress, setProgress] = useState({});
  const [log, setLog] = useState([]);
  const [custom, setCustom] = useState('');
  const [confirm, setConfirm] = useState(null);

  useEffect(() => { send({ type: 'local_status' }); }, [send]);
  useAgentEvent('agent-local_status', (ev) => setSt(ev));
  useAgentEvent('agent-local_log', (ev) => setLog((l) => [...l.slice(-6), ev.text]));
  useAgentEvent('agent-local_progress', (ev) => setProgress((p) => ({ ...p, [ev.name]: ev })));

  if (!st) return <div className="tab-pane muted">Checking this Mac…</div>;
  const have = new Set(st.models.map((m) => m.name));
  const inList = (name) => models.items.find((m) => m.provider === 'ollama' && m.model === name);

  function Bar({ name }) {
    const pr = progress[name];
    if (!pr || ['success', 'cancelled'].includes(pr.status)) return null;
    if (pr.status === 'error') return <div className="test-result fail">{pr.error}</div>;
    return (
      <div className="dl">
        <div className="dl-bar"><div style={{ width: `${pr.pct ?? 2}%` }} /></div>
        <div className="dl-meta">
          <span>{pr.pct != null ? `${pr.pct}%` : pr.status}{pr.total_gb ? ` · ${pr.done_gb} of ${pr.total_gb} GB` : ''}</span>
          <button className="link-btn" onClick={() => send({ type: 'local_cancel', name })}>Cancel</button>
        </div>
      </div>
    );
  }

  return (
    <div className="tab-pane">
      <div className={`ollama-status ${st.running ? 'ok' : ''}`}>
        <span className={`dot ${st.running ? 'on' : 'off'}`} />
        <div className="grow">
          {st.running ? <><b>Ollama is running</b><span className="muted"> · free local models · v{st.version}</span></>
            : st.installed ? <><b>Ollama is installed but not running</b></>
            : <><b>Ollama isn’t installed yet</b><div className="muted small">Ollama runs AI models on your Mac for free. Nothing you send leaves your computer.</div></>}
        </div>
        {!st.installed && st.can_install && (
          <button className="btn primary sm" disabled={st.installing} onClick={() => { setLog([]); send({ type: 'local_install' }); }}>
            {st.installing ? 'Installing…' : 'Install Ollama'}
          </button>
        )}
        {st.installed && !st.running && <button className="btn primary sm" onClick={() => send({ type: 'local_start' })}>Start Ollama</button>}
      </div>
      {st.installing && log.length > 0 && <pre className="install-log">{log.join('\n')}</pre>}

      <div className="section-title pane-sub">Recommended for your Mac · {st.ram_gb} GB memory</div>
      <div className="local-grid">
        {st.recommended.map((r) => {
          const downloaded = have.has(r.name);
          const tooBig = r.min_ram > st.ram_gb;
          const busy = st.pulling.includes(r.name);
          return (
            <div key={r.name} className={`local-card ${r.name === st.best ? 'best' : ''}`}>
              <div className="local-head">
                <b>{r.title}</b>
                {r.name === st.best && <span className="badge">Best fit</span>}
              </div>
              <div className="muted small">{r.about}</div>
              <div className="local-meta"><code>{r.name}</code> · {r.size}</div>
              {tooBig && !downloaded && r.name !== st.best && <div className="warn small">Needs about {r.min_ram} GB of memory; it would be very slow here.</div>}
              <Bar name={r.name} />
              <div className="local-actions">
                {downloaded ? (
                  inList(r.name) && inList(r.name).id === models.active
                    ? <span className="chip ok">In use</span>
                    : <button className="btn ghost sm" onClick={() => inList(r.name) ? send({ type: 'model_select', id: inList(r.name).id }) : send({ type: 'local_pull', name: r.name })}>Chat with it</button>
                ) : (
                  <button className="btn primary sm" disabled={!st.running || busy} onClick={() => send({ type: 'local_pull', name: r.name })}>
                    {busy ? 'Downloading…' : 'Download'}
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {st.models.length > 0 && (
        <>
          <div className="section-title pane-sub">On this Mac</div>
          <ul className="local-list">
            {st.models.map((m) => (
              <li key={m.name}>
                <code>{m.name}</code><span className="muted small">{m.size_gb} GB</span>
                <span className="grow" />
                {inList(m.name)?.id === models.active ? <span className="chip ok">In use</span>
                  : <button className="btn ghost sm" onClick={() => inList(m.name) ? send({ type: 'model_select', id: inList(m.name).id }) : send({ type: 'local_pull', name: m.name })}>Use</button>}
                <button className={`btn ghost sm ${confirm === m.name ? 'danger' : ''}`}
                  onClick={() => (confirm === m.name ? (send({ type: 'local_delete', name: m.name }), setConfirm(null)) : setConfirm(m.name))}>
                  {confirm === m.name ? `Free ${m.size_gb} GB?` : 'Remove'}
                </button>
              </li>
            ))}
          </ul>
        </>
      )}

      {st.running && (
        <div className="custom-pull">
          <input value={custom} onChange={(e) => setCustom(e.target.value)} placeholder="Another model from ollama.com, e.g. qwen3:8b" spellCheck={false}
            onKeyDown={(e) => e.key === 'Enter' && custom.trim() && send({ type: 'local_pull', name: custom.trim() })} />
          <button className="btn ghost sm" disabled={!custom.trim()} onClick={() => send({ type: 'local_pull', name: custom.trim() })}>Download</button>
          <a className="small" href="https://ollama.com/search?c=tools" target="_blank" rel="noreferrer">Browse models ↗</a>
        </div>
      )}
      {Object.entries(progress).filter(([n]) => !st.recommended.some((r) => r.name === n)).map(([n]) => (
        <div key={n} className="custom-progress"><code>{n}</code><Bar name={n} /></div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* The panel shell with tabs.                                                 */
/* ------------------------------------------------------------------------ */
export default function ModelsPanel({ models, send, onClose, initialTab }) {
  const [tab, setTab] = useState(initialTab || (models.items.length ? 'mine' : 'local'));
  useEffect(() => {
    const esc = (e) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', esc);
    return () => window.removeEventListener('keydown', esc);
  }, [onClose]);
  const tabs = [['connect', 'Connect a provider'], ['local', 'Local models'], ['mine', `My models · ${models.items.length}`]];
  return (
    <div className="modal-scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-label="Models">
        <div className="modal-head">
          <div>
            <div className="modal-title">Models</div>
            <div className="muted small">Choose what powers Steward. Add as many as you like and switch from the chat box.</div>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="Close"><Icon name="x" /></button>
        </div>
        <div className="tabs" role="tablist">
          {tabs.map(([id, label]) => (
            <button key={id} role="tab" aria-selected={tab === id} className={tab === id ? 'on' : ''} onClick={() => setTab(id)}>{label}</button>
          ))}
        </div>
        {tab === 'connect' && <div className="modal-scroll"><ConnectProvider models={models} send={send} onDone={() => setTab('mine')} /></div>}
        {tab === 'local' && <div className="modal-scroll"><LocalModels models={models} send={send} /></div>}
        {tab === 'mine' && (models.items.length
          ? <MyModels models={models} send={send} onClose={onClose} />
          : <div className="tab-pane muted">No models yet. Download a free one under <b>Local models</b>, or <b>Connect a provider</b>.</div>)}
      </div>
    </div>
  );
}
