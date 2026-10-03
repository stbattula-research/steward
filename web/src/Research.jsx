import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Icon } from './icons.jsx';

/* --------------------------------------------------------------- citations -- */
// Turn [1] / [1, 3] / [1][2] in an answer into small links to its sources.
const CITE = /\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\]/g;
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

export function citeHtml(html, sources) {
  if (!sources?.length) return html;
  const byN = new Map(sources.map((s) => [s.n, s]));
  // leave code blocks, inline code and existing links alone
  return html.split(/(<pre[\s\S]*?<\/pre>|<code[\s\S]*?<\/code>|<a [\s\S]*?<\/a>)/g).map((part, i) => {
    if (i % 2) return part;
    return part.replace(CITE, (whole, nums) => {
      const links = nums.split(',').map((x) => byN.get(Number(x.trim()))).filter(Boolean)
        .map((s) => `<a class="cite" href="${esc(s.url)}" title="${esc(s.title || s.domain)}">${s.n}</a>`);
      return links.length ? `<span class="cites">${links.join('')}</span>` : whole;
    });
  }).join('');
}

export function citedNumbers(text) {
  const used = new Set();
  for (const m of (text || '').matchAll(CITE)) m[1].split(',').forEach((x) => used.add(Number(x.trim())));
  return used;
}

/* ----------------------------------------------------------- source cards -- */
export function SourceCards({ sources, text }) {
  const [all, setAll] = useState(false);
  if (!sources?.length) return null;
  const used = citedNumbers(text);
  const cited = sources.filter((s) => used.has(s.n));
  const rest = sources.filter((s) => !used.has(s.n));
  const shown = all ? [...cited, ...rest] : (cited.length ? cited : sources.slice(0, 6));
  const hidden = sources.length - shown.length;
  return (
    <div className="sources">
      <div className="sources-head"><Icon name="globe" size={13} /> Sources</div>
      <div className="source-row">
        {shown.map((s) => (
          <a key={s.n} className={`source ${used.has(s.n) ? '' : 'uncited'}`} href={s.url} target="_blank" rel="noreferrer" title={s.url}>
            <span className="source-top">
              <span className="source-fav">{(s.domain || '?')[0].toUpperCase()}</span>
              <span className="source-domain">{s.domain}</span>
              <span className="source-n">{s.n}</span>
            </span>
            <span className="source-title">{s.title || s.url}</span>
          </a>
        ))}
        {hidden > 0 && <button className="source more" onClick={() => setAll(true)}>+{hidden} more</button>}
      </div>
    </div>
  );
}

export function Related({ items, onPick }) {
  if (!items?.length) return null;
  return (
    <div className="related">
      <div className="sources-head"><Icon name="sparkle" size={13} /> Related</div>
      {items.map((q) => (
        <button key={q} className="related-q" onClick={() => onPick(q)}>
          <span>{q}</span><Icon name="plus" size={14} />
        </button>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------ team card -- */
const STAGE_LABEL = (st) => (st === 'research' ? 'Research' : st.startsWith('discuss') ? `Discussion${st.slice(7) > 1 ? ` · round ${st.slice(7)}` : ''}` : st);

export function CouncilCard({ group, live, busy, Markdown }) {
  const [tab, setTab] = useState(group.members[0]?.id);
  const [open, setOpen] = useState(true);
  const done = group.phase === 'final' || group.phase === 'failed';
  useEffect(() => { if (done) setOpen(false); }, [done]);
  const phases = [['research', 'Research'], ['discuss', 'Discuss'], ['final', 'Final answer']];
  const at = group.phase === 'failed' ? -1 : phases.findIndex(([p]) => p === group.phase);
  const member = group.members.find((m) => m.id === tab) || group.members[0];
  const entries = group.entries[member?.id] || [];
  const stage = group.phase === 'discuss' ? `discuss${group.round || 1}` : 'research';
  const stillWorking = (mid) => busy && !done && !(group.entries[mid] || []).some((e) => e.error || e.stage === stage);
  return (
    <div className={`council ${open ? 'open' : ''}`}>
      <button className="council-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <Icon name="users" size={16} />
        <span className="council-title">Team of {group.members.length}</span>
        <span className="council-names">{group.members.map((m) => m.label).join(' · ')}</span>
        <Icon name="chevron" size={14} className="chev" />
      </button>
      <ol className="council-phases">
        {phases.map(([p, label], i) => (
          <li key={p} className={i < at || (p !== 'final' && done && group.phase !== 'failed') || (p === 'final' && group.phase === 'final' && !busy) ? 'done' : i === at ? 'now' : ''}>
            {label}{p === 'discuss' && group.rounds > 1 ? ` (${group.rounds} rounds)` : ''}
          </li>
        ))}
      </ol>
      {open && (
        <>
          <div className="council-tabs" role="tablist">
            {group.members.map((m) => {
              const working = stillWorking(m.id);
              const err = (group.entries[m.id] || []).some((e) => e.error);
              return (
                <button key={m.id} role="tab" aria-selected={tab === m.id} className={tab === m.id ? 'on' : ''} onClick={() => setTab(m.id)}>
                  <span className={`dot ${err ? 'err' : working ? 'busy' : 'on'}`} />
                  {m.label}
                  {working && live?.[m.id]?.steps ? <span className="muted small"> · {live[m.id].steps}</span> : null}
                </button>
              );
            })}
          </div>
          <div className="council-body">
            {entries.length === 0 && (
              <p className="muted small">{stillWorking(member.id) ? (live?.[member.id]?.step || 'Thinking…') : 'Waiting…'}</p>
            )}
            {entries.map((e) => (
              <section key={e.stage} className="council-entry">
                <div className="council-stage">{STAGE_LABEL(e.stage)}{e.steps ? <span className="muted"> · {e.steps} step{e.steps === 1 ? '' : 's'}</span> : null}</div>
                {e.error ? <p className="council-err">{e.error}</p> : <Markdown text={e.text} sources={group.sources} />}
              </section>
            ))}
            {entries.length > 0 && stillWorking(member.id) && <p className="muted small">{live?.[member.id]?.step || 'Thinking…'}</p>}
          </div>
        </>
      )}
    </div>
  );
}

/* ---------------------------------------------------- composer controls -- */
function usePopover() {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState(null);
  const anchor = useRef(null);
  const menu = useRef(null);
  const toggle = () => {
    if (!open) {
      const r = anchor.current.getBoundingClientRect();
      setPos({ left: Math.max(8, Math.min(r.left, window.innerWidth - 316)), bottom: window.innerHeight - r.top + 8 });
    }
    setOpen(!open);
  };
  useEffect(() => {
    if (!open) return;
    const close = (e) => { if (!anchor.current?.contains(e.target) && !menu.current?.contains(e.target)) setOpen(false); };
    const esc = (e) => e.key === 'Escape' && setOpen(false);
    window.addEventListener('pointerdown', close);
    window.addEventListener('keydown', esc);
    return () => { window.removeEventListener('pointerdown', close); window.removeEventListener('keydown', esc); };
  }, [open]);
  return { open, setOpen, pos, anchor, menu, toggle };
}

export const MODES = [
  { id: 'auto', label: 'Auto', icon: 'auto', tip: 'Searches the web when it needs to, or works on your Mac' },
  { id: 'web', label: 'Web search', icon: 'globe', tip: 'Always searches the web and cites sources' },
  { id: 'academic', label: 'Academic', icon: 'book', tip: 'Papers, preprints and university sources' },
  { id: 'research', label: 'Deep research', icon: 'layers', tip: 'Many searches, a structured report with citations' },
];

export function ModeChip({ mode, setMode }) {
  const p = usePopover();
  const cur = MODES.find((m) => m.id === mode) || MODES[0];
  return (
    <div className="model-picker" ref={p.anchor}>
      <button className={`model-chip ${mode !== 'auto' ? 'active' : ''}`} onClick={p.toggle} aria-haspopup="menu" aria-expanded={p.open} title={cur.tip}>
        <Icon name={cur.icon} size={13} /><span className="model-chip-label chip-text">{cur.label}</span>
      </button>
      {p.open && p.pos && createPortal(
        <div className="model-menu" role="menu" ref={p.menu} style={{ left: p.pos.left, bottom: p.pos.bottom }}>
          <div className="model-menu-title">Search mode</div>
          {MODES.map((m) => (
            <button key={m.id} role="menuitemradio" aria-checked={m.id === mode} className={`model-item ${m.id === mode ? 'on' : ''}`}
              onClick={() => { setMode(m.id); p.setOpen(false); }}>
              <Icon name={m.icon} size={15} />
              <span className="model-item-text"><span className="model-item-label">{m.label}</span><span className="model-item-sub">{m.tip}</span></span>
              {m.id === mode && <Icon name="check" size={14} />}
            </button>
          ))}
        </div>, document.body,
      )}
    </div>
  );
}

export function TeamChip({ models, team, setTeam }) {
  const p = usePopover();
  const ids = (team.members || []).filter((id) => models.items.some((m) => m.id === id));
  const on = team.on && ids.length >= 2;
  if (models.items.length < 2) return null;
  const toggle = (id) => {
    const has = ids.includes(id);
    const next = has ? ids.filter((x) => x !== id) : ids.length >= 3 ? ids : [...ids, id];
    setTeam({ ...team, members: next, on: next.length >= 2 ? (team.on || !has) : false });
  };
  return (
    <div className="model-picker" ref={p.anchor}>
      <button className={`model-chip ${on ? 'active' : ''}`} onClick={p.toggle} aria-haspopup="dialog" aria-expanded={p.open}
        title="Give the task to a team of up to 3 models that discuss it">
        <Icon name="users" size={13} /><span className="model-chip-label chip-text">{on ? `Team · ${ids.length}` : 'Team'}</span>
      </button>
      {p.open && p.pos && createPortal(
        <div className="model-menu team-menu" role="dialog" aria-label="Team" ref={p.menu} style={{ left: p.pos.left, bottom: p.pos.bottom }}>
          <div className="team-top">
            <div>
              <div className="model-menu-title" style={{ padding: 0 }}>Team mode</div>
              <div className="model-item-sub">Up to 3 models research on their own, discuss, then the model you’re chatting with writes the final answer and does the work.</div>
            </div>
            <button role="switch" aria-checked={on} className={`switch ${on ? 'on' : ''}`} disabled={ids.length < 2}
              onClick={() => setTeam({ ...team, on: !on })} aria-label="Team mode"><span /></button>
          </div>
          <div className="model-menu-title">Teammates ({ids.length}/3)</div>
          {models.items.map((m) => {
            const checked = ids.includes(m.id);
            return (
              <button key={m.id} role="menuitemcheckbox" aria-checked={checked} disabled={!checked && ids.length >= 3}
                className={`model-item ${checked ? 'on' : ''}`} onClick={() => toggle(m.id)}>
                <span className={`check-box ${checked ? 'on' : ''}`}>{checked && <Icon name="check" size={12} />}</span>
                <span className={`model-dot ${m.local ? 'local' : 'cloud'}`} />
                <span className="model-item-text"><span className="model-item-label">{m.label}</span>
                  <span className="model-item-sub">{m.provider_label}{m.id === models.active ? ' · lead' : ''}</span></span>
              </button>
            );
          })}
          <div className="team-rounds">
            <span className="model-item-sub">Discussion rounds</span>
            <div className="seg sm">
              {[1, 2].map((r) => (
                <button key={r} className={(team.rounds || 1) === r ? 'on' : ''} onClick={() => setTeam({ ...team, rounds: r })}>{r}</button>
              ))}
            </div>
          </div>
          {ids.length < 2 && <div className="model-menu-note">Pick at least 2 models.</div>}
          {ids.filter((id) => models.items.find((m) => m.id === id)?.local).length > 1 &&
            <div className="model-menu-note">Local models take turns, so a team of them is slower.</div>}
        </div>, document.body,
      )}
    </div>
  );
}
