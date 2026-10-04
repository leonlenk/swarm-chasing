import type { ReactNode } from 'react';
import type { Agent, DataSource, RecallEvent } from '../model/types';
import {
  claimImpact, correctionReport, type ClaimState, type WorldState,
} from '../engine/reconstruct';
import { MONITOR_LABEL, type Finding } from '../engine/monitors';
import { Record, UnavailableRecord } from './Record';
import { AgentChip, EvidencePill, StatusPill } from './Pills';
import { time } from './format';
import { plain } from './labels';

export type Selection =
  | { kind: 'none' }
  | { kind: 'task'; id: string }
  | { kind: 'finding'; id: string }
  | { kind: 'claim'; id: string }
  | { kind: 'event'; id: string };

interface Props {
  selection: Selection;
  ws: WorldState;
  findings: Finding[];
  agents: Map<string, Agent>;
  source: DataSource;
  experimentOn: boolean;
  onSelectTask: (id: string) => void;
  onSelectFinding: (f: Finding) => void;
  onSelectClaim: (id: string) => void;
  onSelectEvent: (id: string) => void;
  onJump: (seq: number) => void;
}

const STANDING_TO_EVIDENCE = { supported: 'supported', unknown: 'unknown', contradicted: 'contradicted', superseded: 'contradicted' } as const;
const STATE_LABEL = { active: 'Active', resolved: 'Resolved', insufficient: 'Insufficient evidence' } as const;
const monLetter = (f: Finding) => f.monitor;

function ClaimCard({ c, ws, agents, onSelect }: { c: ClaimState; ws: WorldState; agents: Map<string, Agent>; onSelect?: (id: string) => void }) {
  const agent = agents.get(c.agentId);
  const body = (
    <>
      <div className="claim-top">
        <span className="claim-id mono">{c.id}</span>
        <EvidencePill status={STANDING_TO_EVIDENCE[c.standing]} />
        {c.standing === 'superseded' && <span className="tag-superseded">Superseded</span>}
        <span className="spacer" />
        <span className="muted small">by <b style={{ color: agent?.color }}>{agent?.name}</b> · #{ws.byId.get(c.eventId)?.sequence}</span>
      </div>
      <p className="claim-text">“{plain(c.text, 240)}”</p>
      <p className="claim-reason">{c.reason}</p>
      {onSelect && <span className="claim-cta">Show tasks citing {c.id} →</span>}
    </>
  );
  return onSelect
    ? <button className={`claim-card clickable st-${c.standing}`} onClick={() => onSelect(c.id)}>{body}</button>
    : <div className={`claim-card st-${c.standing}`}>{body}</div>;
}

function Section({ title, count, children }: { title: string; count?: number; children: ReactNode }) {
  return (
    <section className="insp-section">
      <h3>{title}{count !== undefined && <span className="count">{count}</span>}</h3>
      {children}
    </section>
  );
}

function TaskRow({ ws, id, note, onSelectTask }: { ws: WorldState; id: string; note?: string; onSelectTask: (id: string) => void }) {
  const t = ws.tasks.get(id);
  if (!t) return null;
  return (
    <button className="reach-row" onClick={() => onSelectTask(id)}>
      <span className="mono">{id}</span>
      <span className="reach-title">{t.title}{note && <span className="muted small"> · {note}</span>}</span>
      <StatusPill status={t.reportedStatus} />
      <EvidencePill status={t.evidenceStatus} />
    </button>
  );
}

export function Inspector(props: Props) {
  const { selection, ws, findings, agents, source, experimentOn, onSelectTask, onSelectFinding, onSelectClaim, onSelectEvent, onJump } = props;
  const rec = (e: RecallEvent | undefined, role?: string, active?: boolean) =>
    e && <Record key={e.id + (role ?? '')} event={e} agent={agents.get(e.agentId)} role={role} cursor={ws.cursor}
      active={active} refStatus={ws.refStatus} onJump={onJump} onInspect={onSelectEvent} />;
  const recOrPlaceholder = (id: string, role?: string, active?: boolean) => {
    const e = ws.byId.get(id);
    return e ? rec(e, role, active) : <UnavailableRecord key={id + (role ?? '')} id={id} status={ws.refStatus(id)} role={role} />;
  };
  const name = (id: string) => agents.get(id)?.name ?? id;

  if (selection.kind === 'finding') {
    const f = findings.find((x) => x.id === selection.id);
    if (!f) return <EmptyInspector text="This finding is not visible at the current timeline position." />;
    const task = ws.tasks.get(f.taskId);
    const claim = ws.claims.get(f.claimId);
    return (
      <div className="inspector-body">
        <div className={`insp-hero finding ${f.state}`}>
          <div className="hero-kicker">
            <span className={`inc-mon mon-${f.monitor}`}>{monLetter(f)}</span>
            {MONITOR_LABEL[f.monitor]}
            <span className={`inc-state ${f.state}`}>{STATE_LABEL[f.state]}</span>
          </div>
          <h2>{f.title}</h2>
          <p className="hero-expl">{f.explanation}</p>
          {f.resolution && <p className="hero-resolution"><span className="glyph">✓</span> {f.resolution.text}</p>}
          <div className="hero-links">
            {task && <button className="chip-btn" onClick={() => onSelectTask(task.id)}>Task {task.id} · {task.title}</button>}
            {claim && <button className="chip-btn mono" onClick={() => onSelectClaim(claim.id)}>Claim {f.claimId}</button>}
          </div>
        </div>

        <Section title="Source records" count={f.evidence.length}>
          {f.evidence.map((ev) => recOrPlaceholder(ev.eventId, ev.role, ev.eventId === f.detectedEventId))}
        </Section>

        {claim && <Section title="Claim in question"><ClaimCard c={claim} ws={ws} agents={agents} onSelect={onSelectClaim} /></Section>}

        <Section title="Dependency reach" count={f.reach.length}>
          <p className="note">Tasks downstream of {f.taskId} in the dependency graph known at #{ws.cursor}. This is structural reach, not proven damage.</p>
          {f.reach.length === 0 ? <p className="empty">No downstream tasks.</p>
            : <div className="reach-list">{f.reach.map((id) => <TaskRow key={id} ws={ws} id={id} onSelectTask={onSelectTask} />)}</div>}
        </Section>

        <Section title="Missing evidence" count={f.missing.length}>
          {f.missing.length === 0 ? <p className="empty">None noted.</p>
            : <ul className="missing">{f.missing.map((m) => <li key={m}>{m}</li>)}</ul>}
        </Section>
      </div>
    );
  }

  if (selection.kind === 'claim') {
    const c = ws.claims.get(selection.id);
    if (!c) return <EmptyInspector text={`Claim ${selection.id} has not been made yet at #${ws.cursor}.`} />;
    const impact = claimImpact(ws, c.id);
    const corrections = ws.visible.filter((e) => e.type === 'correction' && e.payload.supersedes === c.id);
    return (
      <div className="inspector-body">
        <div className="insp-hero">
          <div className="hero-kicker"><span className="mono">{c.id}</span> · Claim</div>
          <ClaimCard c={c} ws={ws} agents={agents} />
          <div className="hl-legend">
            <span className="hl-key cites">cites {c.id}</span> tasks with a record that states or explicitly cites it
            <span className="hl-key reach">dependency reach</span> downstream of those tasks (structural, not proven damage)
          </div>
        </div>
        <Section title={`Tasks explicitly referencing ${c.id}`} count={impact.referencing.length}>
          <div className="reach-list">
            {impact.referencing.map((r) => <TaskRow key={r.taskId} ws={ws} id={r.taskId} note={`via ${r.eventIds.join(', ')}`} onSelectTask={onSelectTask} />)}
          </div>
        </Section>
        <Section title="Downstream dependency reach" count={impact.reach.length}>
          {impact.reach.length === 0 ? <p className="empty">No further downstream tasks.</p>
            : <div className="reach-list">{impact.reach.map((id) => <TaskRow key={id} ws={ws} id={id} onSelectTask={onSelectTask} />)}</div>}
        </Section>
        <Section title="Records citing this claim" count={impact.referencing.flatMap((r) => r.eventIds).length}>
          {impact.referencing.flatMap((r) => r.eventIds).map((id) => rec(ws.byId.get(id)))}
        </Section>
        {corrections.length > 0 && (
          <Section title="Corrections" count={corrections.length}>{corrections.map((e) => rec(e))}</Section>
        )}
      </div>
    );
  }

  if (selection.kind === 'task') {
    const t = ws.tasks.get(selection.id);
    if (!t) return <EmptyInspector text="This task does not exist yet at the current timeline position." />;
    const agent = agents.get(t.owner);
    const events = t.eventIds.map((id) => ws.byId.get(id)!).filter((e) => e.type !== 'task_created');
    const claims = t.claimsUsed.map((id) => ws.claims.get(id)).filter((c): c is ClaimState => !!c);
    const toolIds = new Set([
      ...events.filter((e) => e.type === 'tool_result').map((e) => e.id),
      ...claims.flatMap((c) => c.evidence),
    ]);
    const tools = [...toolIds].map((id) => ws.byId.get(id)!).sort((a, b) => a.sequence - b.sequence);
    const unavailable = [...new Set(events.flatMap((e) => e.evidenceRefs))].filter((id) => ws.refStatus(id) === 'withheld' || ws.refStatus(id) === 'missing');
    const messages = events.filter((e) => e.type !== 'tool_result');
    const related = findings.filter((f) => f.taskId === t.id || f.reach.includes(t.id));
    return (
      <div className="inspector-body">
        <div className={`insp-hero task tn-${t.evidenceStatus}`}>
          <div className="hero-kicker"><span className="mono">{t.id}</span> · Task</div>
          <h2>{t.title}</h2>
          <div className="hero-owner">
            <AgentChip name={agent?.name ?? t.owner} color={agent?.color ?? '#888'} />
            {agent && <span className="muted small">{agent.role}</span>}
          </div>
          <div className="hero-status">
            <div><label>Reported status</label><StatusPill status={t.reportedStatus} /></div>
            <div><label>Evidence status</label><EvidencePill status={t.evidenceStatus} /></div>
          </div>
          <p className="hero-expl">{t.evidenceReason}</p>
        </div>

        {related.length > 0 && (
          <Section title="Incidents touching this task" count={related.length}>
            {related.map((f) => (
              <button key={f.id} className={`incident compact ${f.state}`} onClick={() => onSelectFinding(f)}>
                <span className={`inc-mon mon-${f.monitor}`}>{monLetter(f)}</span>
                <span className="inc-title">{f.title}</span>
                <span className="muted small">{f.taskId === t.id ? 'source' : 'in reach'}</span>
              </button>
            ))}
          </Section>
        )}

        <Section title="Claims used" count={claims.length}>
          {claims.length === 0 ? <p className="empty">This task cites no claims.</p>
            : claims.map((c) => <ClaimCard key={c.id} c={c} ws={ws} agents={agents} onSelect={onSelectClaim} />)}
        </Section>

        <Section title="Status history" count={t.history.length}>
          <ol className="history">
            {t.history.map((h) => (
              <li key={h.eventId + h.status}>
                <span className="mono muted">#{h.sequence} · {time(h.timestamp)}</span>
                <StatusPill status={h.status} />
                <span className="muted small">by {name(h.agentId)}</span>
                <button className="link-btn" onClick={() => onSelectEvent(h.eventId)}>{h.eventId}</button>
              </li>
            ))}
          </ol>
        </Section>

        <Section title="Supporting messages" count={messages.length}>
          {messages.length === 0 ? <p className="empty">No messages yet.</p> : messages.map((e) => rec(e))}
        </Section>

        <Section title="Relevant tool results" count={tools.length + unavailable.length}>
          {tools.length === 0 && unavailable.length === 0 && <p className="empty">No tool output bears on this task yet.</p>}
          {tools.map((e) => rec(e))}
          {unavailable.map((id) => <UnavailableRecord key={id} id={id} status={ws.refStatus(id)} role="Cited record" />)}
        </Section>
      </div>
    );
  }

  if (selection.kind === 'event') {
    const status = ws.refStatus(selection.id);
    const e = ws.byId.get(selection.id);
    if (!e) {
      return status === 'future'
        ? <EmptyInspector text="This record is after the current timeline position." />
        : <div className="inspector-body"><UnavailableRecord id={selection.id} status={status} /></div>;
    }
    const report = e.type === 'correction' ? correctionReport(ws, e.id, source.agents.map((a) => a.id)) : null;
    return (
      <div className="inspector-body">
        <Section title={report ? 'Correction' : 'Source record'}>{rec(e, undefined, true)}</Section>
        {report && (
          <>
            <Section title="Acknowledgements" count={report.agents.filter((a) => a.ackEventId).length}>
              <p className="note">
                Up to #{ws.cursor}. “Not observed” means no acknowledgement is recorded. It does not mean the agent ignored
                the correction, and an @-mention is not treated as receipt.
              </p>
              <div className="ack-list">
                {report.agents.map((a) => (
                  <div key={a.agentId} className={`ack-row ${a.ackEventId ? 'acked' : 'unobserved'}`}>
                    <AgentChip name={name(a.agentId)} color={agents.get(a.agentId)?.color ?? '#888'} />
                    {a.addressed && <span className="muted small">@-mentioned</span>}
                    <span className="spacer" />
                    {a.ackEventId
                      ? <button className="ack-state acked" onClick={() => onSelectEvent(a.ackEventId!)}>✓ Acknowledged · #{a.ackSeq}</button>
                      : <span className="ack-state unobserved">Not observed</span>}
                  </div>
                ))}
              </div>
            </Section>
            <Section title={`Later actions still citing ${report.claimId}`} count={report.staleActions.length}>
              {report.staleActions.length === 0
                ? <p className="empty">None recorded up to #{ws.cursor}.</p>
                : report.staleActions.map((s) => (
                  <div key={s.eventId}>
                    {rec(ws.byId.get(s.eventId), s.ackedBefore
                      ? `${name(s.agentId)} had acknowledged before acting`
                      : `No acknowledgement from ${name(s.agentId)} observed before this action`)}
                  </div>
                ))}
            </Section>
            <Section title="Superseded claim">
              {ws.claims.get(report.claimId) && <ClaimCard c={ws.claims.get(report.claimId)!} ws={ws} agents={agents} onSelect={onSelectClaim} />}
            </Section>
          </>
        )}
        {e.taskId && ws.tasks.has(e.taskId) && (
          <button className="chip-btn" onClick={() => onSelectTask(e.taskId!)}>Open task {e.taskId} · {ws.tasks.get(e.taskId)!.title}</button>
        )}
      </div>
    );
  }

  return (
    <div className="inspector-body">
      <div className="insp-hero intro">
        <div className="hero-kicker">{source.label}</div>
        <h2>How to read this</h2>
        <p className="hero-expl">{source.description}</p>
      </div>
      {experimentOn && source.experiment && (
        <div className="experiment-note">
          <b>Evidence visibility experiment is on.</b> {source.experiment.description}
        </div>
      )}
      <Section title="Views">
        <p className="note"><b>Reported</b>: what agents said each task's status is. <b>Evidence</b>: what tool results and records establish. <b>Difference</b>: tasks where the two conflict, or where work is reported with nothing to back it.</p>
      </Section>
      <Section title="Evidence status">
        <div className="legend-grid">
          <EvidencePill status="supported" /><span>A matching tool result or acknowledged correction backs the status.</span>
          <EvidencePill status="unknown" /><span>Nothing visible confirms or refutes it.</span>
          <EvidencePill status="contradicted" /><span>A failed verification or a correction contradicts it.</span>
        </div>
      </Section>
      <Section title="Monitors">
        <p className="note"><b>A · Unsupported completion</b>: a task claimed complete after an explicit failed verification of the same artifact version, with no passing run available. If the cited run can't be seen, the finding reports insufficient evidence.</p>
        <p className="note"><b>B · Superseded claim reused</b>: an action explicitly cites a claim that an earlier correction withdrew. An agent only counts as having seen a correction if it recorded an acknowledgement.</p>
      </Section>
      <Section title="Provenance">
        <p className="note"><span className="prov prov-observed">observed</span> tool output or system record · <span className="prov prov-declared">declared</span> stated by an agent · <span className="prov prov-inferred">inferred</span> derived by an adapter.</p>
      </Section>
    </div>
  );
}

function EmptyInspector({ text }: { text: string }) {
  return <div className="inspector-body"><p className="empty">{text}</p></div>;
}
