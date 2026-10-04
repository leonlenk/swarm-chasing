// One card per SubjectIncident: mini timeline, roles, derived counts (each opens its records), member findings
// inside the card, and the read-only "who should recheck what" panel. RECALL only suggests; it never posts.
import { useState, type ReactNode } from 'react';
import { useRecall } from './context';
import type { Finding } from '../engine/monitors';
import type { RecallEvent } from '../model/types';
import { recheckSuggestions, ROLE_LABEL, type SubjectIncident } from '../engine/subjects';
import { plain, subjectLabel } from './labels';
import { Avatar } from './Pills';

type Mark = { e: RecallEvent; kind: 'claim' | 'repeat' | 'pass' | 'fail' | 'report' | 'correction'; label: string };

const KIND_GLYPH: Record<Mark['kind'], string> = { claim: '●', repeat: '○', pass: '✓', fail: '✗', report: '!', correction: '↺' };

export function SubjectCard({ inc, members, selectedId, renderRow }: { inc: SubjectIncident; members: Finding[]; selectedId?: string; renderRow: (f: Finding) => ReactNode }) {
  const { ws, name, agents, openRecord, seek } = useRecall();
  const [open, setOpen] = useState<null | 'cascade' | 'check' | 'correction' | 'recheck'>(null);
  const marks: Mark[] = [
    { e: inc.firstClaim, kind: 'claim' as const, label: `First claim · ${name(inc.firstClaim.agentId)}` },
    ...inc.repeats.map((e) => ({ e, kind: 'repeat' as const, label: `Repeat · ${name(e.agentId)}` })),
    ...inc.checks.filter((k) => k.payload.outcome !== 'inconclusive').map((e) => ({ e, kind: e.payload.outcome === 'pass' ? 'pass' as const : 'fail' as const, label: `${e.payload.outcome === 'pass' ? 'Pass' : 'Fail'} · ${name(e.agentId)}` })),
    ...inc.failureReports.map((e) => ({ e, kind: 'report' as const, label: `Failure report · ${e.payload.isHuman ? 'human' : name(e.agentId)}` })),
    ...inc.corrections.map((e) => ({ e, kind: 'correction' as const, label: `Correction · ${name(e.agentId)}` })),
  ].sort((a, b) => a.e.sequence - b.e.sequence);
  const shown = marks.length > 16 ? [...marks.slice(0, 8), ...marks.slice(-7)] : marks;
  const lo = marks[0].e.sequence; const hi = Math.max(marks[marks.length - 1].e.sequence, lo + 1);
  const recheck = recheckSuggestions(inc, ws);
  const byAgent = new Map<string, string[]>();
  inc.participants.forEach((p) => byAgent.set(p.agent, [...(byAgent.get(p.agent) ?? []), ROLE_LABEL[p.role]]));
  const list = (records: RecallEvent[]) => (
    <div className="count-list">
      {records.length === 0 ? <span className="muted small">No records.</span> : records.map((e) => (
        <button key={e.id} className="link small" onClick={() => openRecord(e.id)}>#{e.sequence} · {name(e.agentId)}: {plain(e.text, 90)}</button>
      ))}
    </div>
  );

  return (
    <section className={`subject-card standing-${inc.standing}`} aria-label={`Subject ${inc.artifact}`}>
      <div className="subject-head">
        <b className="subject-name">{subjectLabel(inc.artifact)}</b>
        <span className={`tag standing ${inc.standing}`}>{inc.standing === 'unchecked' ? 'never checked' : inc.standing}</span>
      </div>
      <div className="subject-counts">
        <button className={`count-chip ${open === 'cascade' ? 'on' : ''}`} onClick={() => setOpen(open === 'cascade' ? null : 'cascade')} title="Distinct agents other than the announcer who repeated the claim">
          <b>{inc.cascade.n}</b> repeater{inc.cascade.n === 1 ? '' : 's'}
        </button>
        <button className={`count-chip ${open === 'check' ? 'on' : ''}`} onClick={() => setOpen(open === 'check' ? null : 'check')} title="Subject records (repeats, failure reports) between the first claim and the first check">
          <b>{inc.toFirstCheck.n}</b> {inc.toFirstCheck.reached ? 'records before first check' : 'records, no check since first claim'}
        </button>
        <button className={`count-chip ${open === 'correction' ? 'on' : ''}`} onClick={() => setOpen(open === 'correction' ? null : 'correction')} title="Subject records between the first claim and the first correction or failure report">
          <b>{inc.toCorrection.n}</b> {inc.toCorrection.reached ? 'records before correction' : 'records, no correction since first claim'}
        </button>
      </div>

      <div className="mini-timeline" role="list" aria-label="Subject timeline">
        {shown.map((m, i) => (
          <button key={m.e.id} role="listitem" className={`mt-mark ${m.kind}`} style={{ left: `${((m.e.sequence - lo) / (hi - lo)) * 100}%` }}
            title={`#${m.e.sequence} · ${m.label}`} onClick={() => { seek(m.e.sequence); openRecord(m.e.id); }}>
            {KIND_GLYPH[m.kind]}{i === 7 && marks.length > 16 ? <span className="mt-gap">+{marks.length - 15}</span> : null}
          </button>
        ))}
      </div>
      <div className="mt-legend muted small">● first claim · ○ repeat · ✓/✗ check · ! failure report · ↺ correction · #{lo}–#{hi}</div>

      <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
        {[...byAgent.entries()].map(([a, roles]) => (
          <span key={a} className="role-chip"><Avatar name={name(a)} color={agents.get(a)?.color} size="sm" />{name(a)}<span className="muted small">{roles.join(', ')}</span></span>
        ))}
      </div>

      {open === 'cascade' && list(inc.cascade.records)}
      {open === 'check' && list(inc.toFirstCheck.records)}
      {open === 'correction' && list(inc.toCorrection.records)}

      {members.length > 0 && (
        <div className="subject-members">
          <div className="section-title">{members.length} finding{members.length === 1 ? '' : 's'} on this subject</div>
          {members.map((f) => <div key={f.id} className={selectedId === f.id ? 'member sel' : 'member'}>{renderRow(f)}</div>)}
        </div>
      )}

      {recheck.length > 0 && (
        <div className="recheck">
          <button className="link small" onClick={() => setOpen(open === 'recheck' ? null : 'recheck')}>
            {open === 'recheck' ? 'Hide' : 'Who should recheck what'} ({recheck.length}) · read-only suggestion
          </button>
          {open === 'recheck' && (
            <ul>
              {recheck.map((r, i) => (
                <li key={i}><b>{name(r.agent)}</b>: {r.action} <span className="muted small">{r.why}</span>
                  {r.records.map((id) => <button key={id} className="link small" onClick={() => openRecord(id)}> #{ws.byId.get(id)?.sequence}</button>)}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

