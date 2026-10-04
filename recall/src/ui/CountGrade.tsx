// Count-grade C (owner ruling): claims on subjects that are not external artifacts (repo pushed, file exists,
// fixed without a subject, process running). Shown only as counts; every count opens its claims.
import { useMemo, useState } from 'react';
import { useRecall } from './context';
import { countGradeClaims } from '../engine/triage';
import { plain } from './labels';

export function CountGrade({ agent }: { agent?: string }) {
  const { findings, ws, name, openRecord } = useRecall();
  const rows = useMemo(() => countGradeClaims(findings, ws), [findings, ws]);
  const [open, setOpen] = useState<string | null>(null);
  const filtered = rows.map((r) => ({ ...r, list: agent ? (r.byAgent.get(agent) ?? []) : [...r.byAgent.values()].flat() })).filter((r) => r.list.length);
  if (!filtered.length) return <p className="muted small">No count-grade claims at #{ws.cursor}.</p>;
  return (
    <div className="count-grade">
      {filtered.map((r) => (
        <div key={r.type}>
          <button className="count-row" onClick={() => setOpen(open === r.type ? null : r.type)} aria-expanded={open === r.type}>
            <b>{r.list.length}</b><span>{r.label}</span>{!agent && <span className="muted small">{r.byAgent.size} agent{r.byAgent.size > 1 ? 's' : ''}</span>}
          </button>
          {open === r.type && (
            <div className="count-list">
              {r.list.slice(0, 60).map((f) => {
                const c = ws.claims.get(f.claimId);
                return (
                  <button key={f.id} className="link small" onClick={() => c && openRecord(c.eventId)}>
                    {!agent && <b>{name(f.agentId)}: </b>}{plain(c?.text ?? f.title, 90)}
                  </button>
                );
              })}
              {r.list.length > 60 && <span className="muted small">+{r.list.length - 60} more</span>}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
