import { useState } from 'react';
import type { Agent, RecallEvent } from '../model/types';
import type { RefStatus } from '../engine/reconstruct';
import { eventTone, time, TYPE_LABEL } from './format';
import { ProvenanceTag } from './Pills';

interface Props {
  event: RecallEvent;
  agent?: Agent;
  role?: string;
  cursor: number;
  active?: boolean;
  refStatus: (id: string) => RefStatus;
  onJump?: (seq: number) => void;
  onInspect?: (eventId: string) => void;
}

const REF_NOTE: Record<Exclude<RefStatus, 'available'>, string> = {
  withheld: 'withheld',
  missing: 'not in dataset',
  future: 'later',
};

/** A cited record id, annotated with whether the analysis can see it. */
export function RefLink({ id, status, onInspect }: { id: string; status: RefStatus; onInspect?: (id: string) => void }) {
  if (status === 'available') {
    return onInspect
      ? <button className="ref ref-available" onClick={() => onInspect(id)}>{id}</button>
      : <span className="ref ref-available">{id}</span>;
  }
  return <span className={`ref ref-${status}`} title={status === 'withheld' ? 'Hidden by the evidence visibility experiment' : undefined}>{id} · {REF_NOTE[status]}</span>;
}

/** Placeholder for a cited record the analysis cannot see. Carries no content from the record. */
export function UnavailableRecord({ id, status, role }: { id: string; status: RefStatus; role?: string }) {
  return (
    <article className={`record unavailable ua-${status}`}>
      {role && <div className="rec-role">{role}</div>}
      <header>
        <span className="rec-type">{status === 'withheld' ? 'Withheld' : status === 'missing' ? 'Not in dataset' : 'Not yet recorded'}</span>
        <span className="rec-meta mono">{id}</span>
      </header>
      <p className="rec-text">
        {status === 'withheld'
          ? 'This record exists in the source but is withheld from the analysis by the evidence visibility experiment. Its content is not shown and was not used.'
          : status === 'missing'
            ? 'This id is cited, but no such record exists in the source dataset.'
            : 'This record is after the current timeline position.'}
      </p>
    </article>
  );
}

/** One source record, rendered as close to the original as possible. */
export function Record({ event, agent, role, cursor, active, refStatus, onJump, onInspect }: Props) {
  const [raw, setRaw] = useState(false);
  return (
    <article className={`record tone-${eventTone(event)} ${active ? 'active' : ''}`}>
      {role && <div className="rec-role">{role}</div>}
      <header>
        <span className={`rec-type tone-${eventTone(event)}`}>{TYPE_LABEL[event.type]}</span>
        <span className="rec-agent" style={{ color: agent?.color }}>{agent?.name ?? event.agentId}</span>
        <span className="rec-meta mono">#{event.sequence} · {time(event.timestamp)} UTC</span>
      </header>
      <p className="rec-text">{event.text}</p>
      {event.type === 'tool_result' && (
        <pre className={`rec-output ${event.payload.outcome}`}>
          <span className="mono muted">$ {event.payload.tool} · {event.payload.runId}
            {event.payload.subject && ` · ${event.payload.subject.artifact}@${event.payload.subject.version}`}</span>
          {'\n'}{event.payload.output}
        </pre>
      )}
      <footer>
        <span className="mono muted">{event.id}</span>
        <ProvenanceTag p={event.provenance} />
        {event.evidenceRefs.length > 0 && (
          <span className="refs">
            <span className="muted">cites</span>
            {event.evidenceRefs.map((r) => <RefLink key={r} id={r} status={refStatus(r)} onInspect={onInspect} />)}
          </span>
        )}
        <span className="spacer" />
        {event.type === 'correction' && onInspect && !active && (
          <button className="link-btn" onClick={() => onInspect(event.id)}>acknowledgements</button>
        )}
        {onJump && event.sequence !== cursor && (
          <button className="link-btn" onClick={() => onJump(event.sequence)}>jump to #{event.sequence}</button>
        )}
        <button className="link-btn" onClick={() => setRaw((r) => !r)}>{raw ? 'hide raw' : 'raw'}</button>
      </footer>
      {raw && <pre className="rec-raw">{JSON.stringify(event, null, 2)}</pre>}
    </article>
  );
}
