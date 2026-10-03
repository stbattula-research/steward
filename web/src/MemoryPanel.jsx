import { useEffect, useMemo, useRef, useState } from 'react';
import { Icon } from './icons.jsx';

const LABELS = {
  'about_me.md': { title: 'About me', hint: 'Facts the agent reads at the start of every conversation.' },
  'watchlist.md': { title: 'Watchlist', hint: 'Checked every hour. Lines starting with "-" are active; it only pings you when something needs you.' },
  'learned.md': { title: 'Learned', hint: 'Things the agent saved when you told it how you like things done.' },
};

function label(name) {
  if (LABELS[name]) return LABELS[name].title;
  return name.replace(/^playbooks\//, '').replace(/\.md$/, '').replace(/[-_]/g, ' ');
}

export default function MemoryPanel({ memory, send, onClose }) {
  const [active, setActive] = useState('about_me.md');
  const [drafts, setDrafts] = useState({});
  const [newName, setNewName] = useState('');
  const [confirmDel, setConfirmDel] = useState(false);
  const editor = useRef(null);

  const files = memory.files;
  const file = files.find((f) => f.name === active) || files[0];
  const value = file ? (drafts[file.name] ?? file.content) : '';
  const dirty = file && drafts[file.name] !== undefined && drafts[file.name] !== file.content;
  const playbooks = useMemo(() => files.filter((f) => f.name.startsWith('playbooks/')), [files]);
  const core = files.filter((f) => !f.name.startsWith('playbooks/'));

  // When the saved version arrives from the agent, drop the matching draft.
  useEffect(() => {
    setDrafts((d) => {
      const next = { ...d };
      for (const f of files) if (next[f.name] === f.content) delete next[f.name];
      return next;
    });
  }, [files]);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') onClose();
      if ((e.metaKey || e.ctrlKey) && e.key === 's') { e.preventDefault(); save(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  useEffect(() => { setConfirmDel(false); }, [active]);

  function save() {
    if (file && dirty) send({ type: 'memory_save', name: file.name, content: value });
  }

  function createPlaybook(e) {
    e.preventDefault();
    const slug = newName.trim().toLowerCase().replace(/[^a-z0-9 _-]/g, '').replace(/\s+/g, '-').slice(0, 60);
    if (!slug) return;
    const name = `playbooks/${slug}.md`;
    send({
      type: 'memory_save', name,
      content: `# Playbook: ${newName.trim()}\n\nWrite the steps the way you'd explain them to an assistant sitting next to you.\n\n1. \n2. \n3. Before anything that pays, sends or submits: ask me to approve.\n`,
    });
    setNewName('');
    setActive(name);
    setTimeout(() => editor.current?.focus(), 150);
  }

  const Item = ({ f }) => (
    <button className={`mem-item ${f.name === file?.name ? 'active' : ''}`} onClick={() => setActive(f.name)}>
      <span>{label(f.name)}</span>
      {drafts[f.name] !== undefined && drafts[f.name] !== f.content && <span className="unsaved" title="Unsaved changes" />}
    </button>
  );

  return (
    <div className="modal-scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-label="Memory">
        <div className="modal-head">
          <div>
            <div className="modal-title">Memory &amp; playbooks</div>
            <div className="muted small">What your agent knows about you and how you like things done.</div>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="Close"><Icon name="x" /></button>
        </div>
        <div className="modal-body">
          <nav className="mem-nav">
            {core.map((f) => <Item key={f.name} f={f} />)}
            <div className="mem-group">Playbooks</div>
            {playbooks.length === 0 && <div className="muted small mem-empty">None yet</div>}
            {playbooks.map((f) => <Item key={f.name} f={f} />)}
            <form className="mem-new" onSubmit={createPlaybook}>
              <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="New playbook…" maxLength={60} />
              <button className="icon-btn" disabled={!newName.trim()} aria-label="Create playbook"><Icon name="plus" size={16} /></button>
            </form>
          </nav>
          <section className="mem-editor">
            {file ? (
              <>
                <div className="mem-hint">
                  {LABELS[file.name]?.hint || 'Step-by-step instructions. The agent reads this before doing this kind of task.'}
                </div>
                <textarea
                  ref={editor}
                  value={value}
                  spellCheck={false}
                  onChange={(e) => setDrafts({ ...drafts, [file.name]: e.target.value })}
                />
                <div className="mem-actions">
                  {file.name.startsWith('playbooks/') && (
                    <button
                      className={`btn ghost sm ${confirmDel ? 'danger' : ''}`}
                      onClick={() => (confirmDel ? (send({ type: 'memory_delete', name: file.name }), setActive('about_me.md')) : setConfirmDel(true))}
                    >
                      {confirmDel ? 'Click again to remove' : 'Remove'}
                    </button>
                  )}
                  <span className="muted small grow-text">{memory.dir && `${memory.dir}/${file.name}`}</span>
                  <button className="btn ghost sm" disabled={!dirty} onClick={() => setDrafts({ ...drafts, [file.name]: undefined })}>Discard</button>
                  <button className="btn primary sm" disabled={!dirty} onClick={save}>Save <kbd>⌘S</kbd></button>
                </div>
              </>
            ) : <div className="muted">Loading…</div>}
          </section>
        </div>
      </div>
    </div>
  );
}
