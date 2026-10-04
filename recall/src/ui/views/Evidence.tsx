import { useMemo, useState } from 'react';
import { useRecall } from '../context';
import type { EventType } from '../../model/types';
import { eventTone, TYPE_LABEL } from '../format';
import { ProvenanceTag } from '../Pills';
import { ISearch } from '../icons';

const PAGE = 200;

export function Evidence() {
  const { source, ws, name, openRecord, input, cursor } = useRecall();
  const [type, setType] = useState<'all' | EventType>('all');
  const [agent, setAgent] = useState('all');
  const [q, setQ] = useState('');
  const [limit, setLimit] = useState(PAGE);
  const counts = useMemo(() => {
    const m = new Map<EventType, number>();
    ws.visible.forEach((e) => m.set(e.type, (m.get(e.type) ?? 0) + 1));
    return m;
  }, [ws]);
  // Withheld rows appear as placeholders (position only) so gaps are visible but content is not.
  const rows = useMemo(() => {
    const withheld = (source?.events ?? []).filter((e) => input.withheld.has(e.id) && e.sequence <= cursor);
    const all = [...ws.visible, ...withheld].sort((a, b) => b.sequence - a.sequence);
    return all.filter((e) => input.withheld.has(e.id)
      ? type === 'all' && agent === 'all' && !q
      : (type === 'all' || e.type === type) && (agent === 'all' || e.agentId === agent) &&
        (!q || `${e.id} ${e.text} ${name(e.agentId)} ${e.taskId ?? ''}`.toLowerCase().includes(q.toLowerCase())));
  }, [ws, source, input, cursor, type, agent, q, name]);

  return (
    <div className="page-wide">
      <div className="hero">
        <div>
          <h1>Every record, as recorded.</h1>
          <p className="sub">The exact source records RECALL reasons over, up to #{ws.cursor}. Click any row to open it with its references and the records that cite it.</p>
        </div>
      </div>
      <section className="card">
        <div className="card-body stack" style={{ paddingBottom: 14 }}>
          <div className="row" style={{ gap: 12 }}>
            <label className="input"><ISearch size={18} /><input placeholder="Search text, ids, agents, tasks…" value={q} onChange={(e) => { setQ(e.target.value); setLimit(PAGE); }} /></label>
            <select className="select" value={agent} onChange={(e) => setAgent(e.target.value)} aria-label="Agent">
              <option value="all">All agents</option>
              {source?.agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select>
          </div>
          <div className="filters">
            <button className={`filter ${type === 'all' ? 'on' : ''}`} onClick={() => setType('all')}>All <span className="n">{ws.visible.length}</span></button>
            {[...counts].sort((a, b) => b[1] - a[1]).map(([t, n]) => (
              <button key={t} className={`filter ${type === t ? 'on' : ''}`} onClick={() => setType(t)}>{TYPE_LABEL[t]} <span className="n">{n}</span></button>
            ))}
          </div>
        </div>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>#</th><th>Time (UTC)</th><th>Agent</th><th>Type</th><th>Task</th><th>Record</th><th>Provenance</th></tr></thead>
            <tbody>
              {rows.slice(0, limit).map((e) => input.withheld.has(e.id) ? (
                <tr key={e.id} className="clickable" onClick={() => openRecord(e.id)}>
                  <td className="mono">{e.sequence}</td><td className="mono muted">—</td><td colSpan={4}><span className="tag withheld">Withheld</span> <span className="muted small">{e.id} is withheld from this analysis. Content hidden.</span></td><td />
                </tr>
              ) : (
                <tr key={e.id} className="clickable" onClick={() => openRecord(e.id)}>
                  <td className="mono">{e.sequence}</td>
                  <td className="mono">{e.timestamp.slice(11, 19)}</td>
                  <td style={{ whiteSpace: 'nowrap' }}>{name(e.agentId)}</td>
                  <td><span className={`rec-type tone-${eventTone(e)}`}>{TYPE_LABEL[e.type]}</span></td>
                  <td className="mono small">{e.taskId ?? '—'}</td>
                  <td><div className="clip">{e.text}</div></td>
                  <td><ProvenanceTag p={e.provenance} /></td>
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length === 0 && <div className="empty-state"><b>No matching records</b>Adjust the filters or scrub the timeline.</div>}
          {rows.length > limit && <div style={{ padding: 16, textAlign: 'center' }}><button className="btn btn-secondary btn-sm" onClick={() => setLimit((l) => l + PAGE)}>Show {Math.min(PAGE, rows.length - limit)} more of {rows.length - limit}</button></div>}
        </div>
      </section>
    </div>
  );
}
