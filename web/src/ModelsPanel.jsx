import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Icon } from './icons.jsx';

const GROUPS = ['On this Mac', 'Claude-compatible', 'OpenAI-compatible', 'Other'];
const EMPTY = { id: '', provider: 'ollama', label: '', model: '', base_url: '', context_tokens: '', api_key: '' };

function useAgentEvent(name, fn) {
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
    window.addEventListener('mousedown', close);
    window.addEventListener('keydown', esc);
    return () => { window.removeEventListener('mousedown', close); window.removeEventListener('keydown', esc); };
  }, [open]);

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
          <div className="model-menu-sep" />
          <button className="model-item manage" onClick={() => { setOpen(false); onManage(); }}>
            <Icon name="plus" size={14} /> <span>Add or manage models…</span>
          </button>
        </div>, document.body,
      )}
    </div>
  );
}

/** Full panel: add, edit, test and remove models. */
export default function ModelsPanel({ models, send, onClose }) {
  const [selected, setSelected] = useState(models.active || 'new');
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

  useEffect(() => {
    const esc = (e) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', esc);
    return () => window.removeEventListener('keydown', esc);
  }, [onClose]);

  const grouped = useMemo(() => GROUPS.map((g) => [g, Object.entries(providers).filter(([, p]) => p.group === g)]), [providers]);
  const suggestions = form.provider === 'ollama' ? tags : (prov.examples || []);
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  const payload = () => ({ ...form, context_tokens: Number(form.context_tokens) || 0 });

  return (
    <div className="modal-scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-label="Models">
        <div className="modal-head">
          <div>
            <div className="modal-title">Models</div>
            <div className="muted small">Add any model you have access to, then pick one for each chat. API keys are stored in your Mac's Keychain.</div>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="Close"><Icon name="x" /></button>
        </div>
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
      </div>
    </div>
  );
}
