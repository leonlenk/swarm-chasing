import { useMemo } from 'react';
import { useRecall } from './context';
import { difference } from '../engine/difference';
import { initials, plain } from './labels';
import { STATUS_LABEL, EVIDENCE_LABEL } from './format';
import type { ViewMode } from './TaskNode';

const hm = (ms: number) => new Date(ms).toISOString().slice(11, 16);

/**
 * Swimlanes: one lane per agent, one bar per task (session) across the source window.
 * Used for sources without dependency structure, where a DAG would just be a grid.
 * Bars end at the task's 'ended'/'done' record, or at the cursor while still open.
 */
export function TaskLanes({ mode, selected, onSelect }: { mode: ViewMode; selected?: string | null; onSelect: (id: string) => void }) {
  const { source, ws, agents } = useRecall();
  const events = useMemo(() => source?.events ?? [], [source]);
  const t0 = events.length ? Date.parse(events[0].timestamp) : 0;
  const t1 = events.length ? Date.parse(events[events.length - 1].timestamp) : 1;
  const span = Math.max(1, t1 - t0);
  const now = ws.visible.length ? Date.parse(ws.visible[ws.visible.length - 1].timestamp) : t0;
  const x = (ms: number) => `${((ms - t0) / span) * 100}%`;

  const lanes = useMemo(() => {
    const byAgent = new Map<string, { id: string; start: number; end: number; open: boolean }[]>();
    for (const t of ws.tasks.values()) {
      const start = Date.parse(t.history[0]?.timestamp ?? '') || t0;
      const closed = [...t.history].reverse().find((h) => h.status === 'ended' || h.status === 'done' || h.status === 'failed');
      const end = closed ? Date.parse(closed.timestamp) : now;
      byAgent.set(t.owner, [...(byAgent.get(t.owner) ?? []), { id: t.id, start, end: Math.max(end, start + span * 0.004), open: !closed }]);
    }
    const order = source?.agents.map((a) => a.id) ?? [];
    return [...byAgent].sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]));
  }, [ws, source, t0, now, span]);

  const marks = useMemo(() => ws.visible.filter((e) => e.taskId && (e.type === 'claim' || e.type === 'correction' ||
    (e.type === 'tool_result' && e.payload.category === 'verification'))), [ws]);

  const ticks = useMemo(() => {
    const step = span > 6 * 3600e3 ? 3600e3 : span > 2 * 3600e3 ? 1800e3 : 900e3;
    const out: number[] = [];
    for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) out.push(t);
    return out;
  }, [t0, t1, span]);

  const tone = (id: string) => {
    const t = ws.tasks.get(id)!;
    if (mode === 'reported') return `rs-${t.reportedStatus}`;
    if (mode === 'evidence') return `ev-${t.evidenceStatus}`;
    return `df-${difference(t)}`;
  };

  if (!lanes.length) return <div className="empty-state"><b>No tasks yet</b>Scrub the timeline forward.</div>;
  return (
    <div className="lanes">
      <div className="lanes-axis">
        <span className="lanes-gutter" />
        <div className="lanes-track">{ticks.map((t) => <span key={t} style={{ left: x(t) }}>{hm(t)}</span>)}</div>
      </div>
      <div className="lanes-body">
        {lanes.map(([agentId, bars]) => {
          const a = agents.get(agentId);
          return (
            <div className="lane" key={agentId}>
              <div className="lanes-gutter lane-who" title={a?.role}>
                <span className="avatar sm" style={{ background: a?.color }}>{initials(a?.name ?? agentId)}</span>
                <span className="lane-name">{a?.name ?? agentId}</span>
                <span className="lane-n">{bars.length}</span>
              </div>
              <div className="lanes-track">
                {ticks.map((t) => <i key={t} className="lane-grid" style={{ left: x(t) }} />)}
                {bars.map((b) => {
                  const t = ws.tasks.get(b.id)!;
                  return (
                    <button key={b.id} className={`lane-bar ${tone(b.id)} ${b.open ? 'open' : ''} ${selected === b.id ? 'sel' : ''}`}
                      style={{ left: x(b.start), width: `calc(${((b.end - b.start) / span) * 100}% - 1px)` }}
                      onClick={() => onSelect(b.id)}
                      title={`${plain(t.title)}\nReported: ${STATUS_LABEL[t.reportedStatus]} · Evidence: ${EVIDENCE_LABEL[t.evidenceStatus]}\n${hm(b.start)}–${b.open ? 'open' : hm(b.end)}`}>
                      <span>{plain(t.title)}</span>
                    </button>
                  );
                })}
                {marks.filter((m) => ws.tasks.get(m.taskId!)?.owner === agentId).map((m) => (
                  <i key={m.id} className={`lane-mark ${m.type === 'claim' ? 'claim' : m.type === 'correction' ? 'corr' : m.type === 'tool_result' && m.payload.outcome === 'fail' ? 'fail' : 'pass'}`}
                    style={{ left: x(Date.parse(m.timestamp)) }} title={`${m.type === 'claim' ? 'Claim' : m.type === 'correction' ? 'Correction' : 'Check'} · ${hm(Date.parse(m.timestamp))}`} />
                ))}
                <i className="lane-now" style={{ left: x(now) }} />
              </div>
            </div>
          );
        })}
      </div>
      <div className="lanes-legend">
        <span><i className="lane-mark claim" /> claim</span>
        <span><i className="lane-mark fail" /> failed check</span>
        <span><i className="lane-mark pass" /> passed check</span>
        <span><i className="lane-mark corr" /> correction</span>
        <span className="muted">Bars run from session start to its recorded end · open bars are still running at the cursor</span>
      </div>
    </div>
  );
}
