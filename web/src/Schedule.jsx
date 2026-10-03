import { useEffect, useMemo, useState } from 'react';
import { Icon } from './icons.jsx';
import { useAgentEvent } from './ModelsPanel.jsx';

const DAYS = [['1', 'Mon'], ['2', 'Tue'], ['3', 'Wed'], ['4', 'Thu'], ['5', 'Fri'], ['6', 'Sat'], ['0', 'Sun']];
const IDEAS = [
  { name: 'Morning brief', instructions: 'Give me a short brief: my calendar for today, important unread emails, and the weather.', repeat: 'weekdays', time: '08:00' },
  { name: 'Clean Downloads', instructions: 'Move files older than 30 days in ~/Downloads into ~/Downloads/Old, grouped by type. Tell me what you moved.', repeat: 'weekly', days: ['0'], time: '18:00' },
  { name: 'Price watch', instructions: 'Check the price of the item in my watchlist and tell me only if it dropped below my target.', repeat: 'hours', every: 6, mode: 'watch' },
];

function pad(n) { return String(n).padStart(2, '0'); }
function today() { const d = new Date(Date.now() + 3600e3); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }

/** Turn the friendly form into a cron line (standard crontab: 0 = Sunday) or a one-time date. */
function toSchedule(f) {
  const [h, m] = (f.time || '09:00').split(':').map(Number);
  if (f.repeat === 'once') return { run_at: `${f.date}T${f.time}` };
  if (f.repeat === 'daily') return { cron: `${m} ${h} * * *` };
  if (f.repeat === 'weekdays') return { cron: `${m} ${h} * * 1-5` };
  if (f.repeat === 'weekly') return { cron: `${m} ${h} * * ${(f.days.length ? f.days : ['1']).join(',')}` };
  if (f.repeat === 'monthly') return { cron: `${m} ${h} ${f.dom || 1} * *` };
  if (f.repeat === 'hours') return { cron: `0 */${Math.max(1, Math.min(23, Number(f.every) || 1))} * * *` };
  return { cron: f.cron.trim() };
}

function describe(t) {
  if (!t.cron) return `Once · ${new Date(t.run_at).toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}`;
  const [m, h, dom, , dow] = t.cron.split(' ');
  const at = /^\d+$/.test(h) && /^\d+$/.test(m) ? new Date(2000, 0, 1, +h, +m).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }) : '';
  if (h.startsWith('*/')) return `Every ${h.slice(2)} hours`;
  if (dow === '1-5' && dom === '*') return `Weekdays at ${at}`;
  if (dow === '*' && dom === '*') return `Every day at ${at}`;
  if (dom === '*' && /^[\d,]+$/.test(dow)) return `${dow.split(',').map((d) => DAYS.find(([k]) => k === String(+d % 7))?.[1]).join(', ')} at ${at}`;
  if (/^\d+$/.test(dom)) return `Monthly on day ${dom} at ${at}`;
  return `Custom: ${t.cron}`;
}

const EMPTY = { name: '', instructions: '', mode: 'task', repeat: 'daily', time: '09:00', date: today(), days: ['1'], dom: 1, every: 6, cron: '' };

export default function SchedulePanel({ tasks, send, onClose }) {
  const [f, setF] = useState(EMPTY);
  const [err, setErr] = useState('');
  const [confirm, setConfirm] = useState(null);
  const [creating, setCreating] = useState(tasks.length === 0);
  const set = (k, v) => setF((x) => ({ ...x, [k]: v }));
  useAgentEvent('agent-task_saved', () => { setF(EMPTY); setErr(''); setCreating(false); });
  useAgentEvent('agent-task_error', (e) => setErr(e.error));
  useEffect(() => {
    const esc = (e) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', esc);
    return () => window.removeEventListener('keydown', esc);
  }, [onClose]);
  const preview = useMemo(() => { try { return describe({ ...toSchedule(f) }); } catch { return ''; } }, [f]);

  function save() {
    setErr('');
    if (!f.instructions.trim()) { setErr('Say what Steward should do.'); return; }
    send({ type: 'task_add', name: f.name.trim() || f.instructions.trim().split(/[.\n]/)[0].slice(0, 50), instructions: f.instructions,
      mode: f.mode, ...toSchedule(f) });
  }

  return (
    <div className="modal-scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-label="Scheduled tasks">
        <div className="modal-head">
          <div>
            <div className="modal-title">Scheduled tasks</div>
            <div className="muted small">Things Steward does on its own, on a schedule. Results come to the app, your phone and your chat apps.</div>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="Close"><Icon name="x" /></button>
        </div>
        <div className="modal-scroll">
          <div className="tab-pane narrow">
            {!creating && (
              <>
                <ul className="sched-list">
                  {tasks.map((t) => (
                    <li key={t.id} className="sched-item">
                      <div className="sched-text">
                        <div className="sched-top"><span className={`mode ${t.mode}`}>{t.mode === 'watch' ? 'Watch' : 'Task'}</span><b>{t.name}</b></div>
                        <div className="muted small">{describe(t)}{t.next_run ? ` · next ${new Date(t.next_run).toLocaleString(undefined, { weekday: 'short', hour: 'numeric', minute: '2-digit' })}` : ''}</div>
                        <div className="sched-instr">{t.instructions}</div>
                      </div>
                      <div className="sched-actions">
                        <button className="btn ghost sm" onClick={() => send({ type: 'task_run', id: t.id })}><Icon name="play" size={12} /> Run now</button>
                        <button className={`btn ghost sm ${confirm === t.id ? 'danger' : ''}`}
                          onClick={() => (confirm === t.id ? (send({ type: 'cancel_task', id: t.id }), setConfirm(null)) : setConfirm(t.id))}
                          onMouseLeave={() => confirm === t.id && setConfirm(null)}>{confirm === t.id ? 'Delete?' : 'Delete'}</button>
                      </div>
                    </li>
                  ))}
                </ul>
                <button className="btn primary" onClick={() => setCreating(true)}><Icon name="plus" size={14} /> New scheduled task</button>
                <p className="muted small">You can also just ask in chat: “every weekday at 8am send me my calendar”.</p>
              </>
            )}
            {creating && (
              <div className="sched-form">
                {tasks.length === 0 && (
                  <div className="sched-ideas">
                    <span className="muted small">Start from an idea:</span>
                    {IDEAS.map((i) => (
                      <button key={i.name} className="chip-btn" onClick={() => setF({ ...EMPTY, ...i })}>{i.name}</button>
                    ))}
                  </div>
                )}
                <label className="field">
                  <span>What should Steward do?</span>
                  <textarea rows={3} value={f.instructions} onChange={(e) => set('instructions', e.target.value)}
                    placeholder="e.g. Check my inbox for anything from my professor and summarise it" />
                </label>
                <div className="field-row">
                  <label className="field"><span>Name</span>
                    <input value={f.name} onChange={(e) => set('name', e.target.value)} placeholder="Optional" /></label>
                  <label className="field"><span>Type</span>
                    <select value={f.mode} onChange={(e) => set('mode', e.target.value)}>
                      <option value="task">Do it and tell me the result</option>
                      <option value="watch">Keep an eye out, only tell me if something needs me</option>
                    </select></label>
                </div>
                <div className="field-row">
                  <label className="field"><span>When</span>
                    <select value={f.repeat} onChange={(e) => set('repeat', e.target.value)}>
                      <option value="once">Once</option>
                      <option value="daily">Every day</option>
                      <option value="weekdays">Every weekday (Mon–Fri)</option>
                      <option value="weekly">Every week on…</option>
                      <option value="monthly">Every month</option>
                      <option value="hours">Every few hours</option>
                      <option value="custom">Custom (cron)</option>
                    </select></label>
                  {f.repeat === 'once' && <label className="field"><span>Date</span><input type="date" value={f.date} onChange={(e) => set('date', e.target.value)} /></label>}
                  {f.repeat === 'monthly' && <label className="field"><span>Day of month</span><input type="number" min={1} max={28} value={f.dom} onChange={(e) => set('dom', e.target.value)} /></label>}
                  {f.repeat === 'hours' && <label className="field"><span>Every</span>
                    <select value={f.every} onChange={(e) => set('every', e.target.value)}>{[1, 2, 3, 4, 6, 8, 12].map((n) => <option key={n} value={n}>{n} hour{n > 1 ? 's' : ''}</option>)}</select></label>}
                  {f.repeat === 'custom' && <label className="field"><span>Cron</span><input value={f.cron} onChange={(e) => set('cron', e.target.value)} placeholder="0 8 * * 1-5" /></label>}
                  {!['hours', 'custom'].includes(f.repeat) && <label className="field"><span>Time</span><input type="time" value={f.time} onChange={(e) => set('time', e.target.value)} /></label>}
                </div>
                {f.repeat === 'weekly' && (
                  <div className="day-picks" role="group" aria-label="Days">
                    {DAYS.map(([k, label]) => (
                      <button key={k} className={f.days.includes(k) ? 'on' : ''} aria-pressed={f.days.includes(k)}
                        onClick={() => set('days', f.days.includes(k) ? f.days.filter((d) => d !== k) : [...f.days, k])}>{label}</button>
                    ))}
                  </div>
                )}
                {preview && <p className="muted small">{preview}{f.mode === 'watch' ? ' · quiet hours are skipped' : ''}</p>}
                {err && <p className="composer-error">{err}</p>}
                <div className="mem-actions">
                  {tasks.length > 0 && <button className="btn ghost" onClick={() => { setCreating(false); setErr(''); }}>Back</button>}
                  <button className="btn primary" onClick={save}>Schedule it</button>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
