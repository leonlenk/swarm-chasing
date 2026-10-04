// Split a window document into parts without losing cross-window facts or leaking the future.
//
// Owner rulings (2026-10-03):
//   - The 1,200 cap counts IN-RANGE records only: each part holds at most `cap` records of its own time range.
//   - Every part carries every window FACT (claims, tool results, corrections, acknowledgements, quotes,
//     session boundaries = task_created + status records) from earlier in the window, flagged `carried: true`.
//   - Carried records keep their ORIGINAL window sequence numbers and pass through the same
//     `sequence <= cursor` filter as everything else; nothing later than the part's range is carried, so a
//     part never sees a record from later in the window.
//   - Every part carries lightweight reference_seen records: each URL/path named in a message or output
//     earlier in the whole window (records not carried as facts) or in the 24 h lookback (sequence 0).
//   - Milestone 3: session actions that write (writes[]) or destroy (destructive) are carried as facts too.

import type { DataSource, RecallEvent, ReferenceSeen } from '../model/types';

const FACT_TYPES = new Set(['claim', 'tool_result', 'correction', 'acknowledgement', 'quote', 'task_created', 'status_updated']);
/** Write actions (owner ruling, Milestone 3): session actions flagged writes/destructive are carried too, so AT sees earlier writes. */
const isWriteAction = (e: RecallEvent) => e.type === 'action' && (!!e.payload.writes?.length || !!e.payload.destructive);
export const isWindowFact = (e: RecallEvent) => FACT_TYPES.has(e.type) || isWriteAction(e);

export interface LookbackRef { ref: string; sourceEventId: string }

function buildPart(doc: DataSource, events: RecallEvent[], i: number, j: number, index: number, count: number, lookback: LookbackRef[]): DataSource {
  const ids = new Set<string>();
  const out: RecallEvent[] = [];
  for (let k = 0; k < j; k++) {
    const e = events[k];
    const inRange = k >= i;
    if (!inRange && !isWindowFact(e)) continue;
    const ev = { ...e } as RecallEvent; // original sequence kept
    if (inRange) delete ev.carried; else ev.carried = true;
    out.push(ev);
    ids.add(ev.id);
  }
  for (const ev of out) ev.evidenceRefs = ev.evidenceRefs.filter((r) => ids.has(r));
  const seen: ReferenceSeen[] = lookback.map((r) => ({ ...r, sequence: 0 }));
  for (let k = 0; k < i; k++) {
    const e = events[k];
    if (isWindowFact(e) || !e.refs?.length) continue; // facts are carried as records; their refs are visible directly
    for (const ref of e.refs) seen.push({ ref, sourceEventId: e.id, sequence: e.sequence });
  }
  const agentIds = new Set(out.map((e) => e.agentId));
  const first = events[i]; const last = events[j - 1];
  const carried = out.filter((e) => e.carried).length;
  const carriedActions = out.filter((e) => e.carried && e.type === 'action').length;
  return {
    ...doc,
    id: count > 1 ? `${doc.id}-p${index}` : doc.id,
    label: count > 1 ? `${doc.label} · part ${index}/${count}` : doc.label,
    agents: doc.agents.filter((a) => agentIds.has(a.id)),
    events: out,
    referencesSeen: seen,
    meta: {
      ...(doc.meta ?? { origin: 'huggingface' }),
      window: { from: first.timestamp, to: last.timestamp },
      notes: [
        ...(doc.meta?.notes ?? []),
        ...(count > 1 ? [`Part ${index} of ${count} (${first.timestamp.slice(11, 16)}–${last.timestamp.slice(11, 16)} UTC): ${j - i} records in range plus ${carried} window facts carried from earlier in the window (claims, tool results, corrections, acknowledgements, session boundaries, and ${carriedActions} write actions), marked "carried" and keeping their original sequence numbers. ${seen.length} earlier references are carried as reference_seen records.`] : []),
      ],
      part: { parent: doc.id, parentLabel: doc.label, index, count, carried, carriedActions, inRange: j - i },
    },
  };
}

/** Split into parts of at most `cap` in-range records; each carries every earlier window fact. */
export function splitWindow(doc: DataSource, cap = 1200, lookback: LookbackRef[] = []): DataSource[] {
  const events = [...doc.events].sort((a, b) => a.sequence - b.sequence);
  if (!events.length) return [doc];
  const count = Math.max(1, Math.ceil(events.length / cap));
  const size = Math.ceil(events.length / count);
  const parts: DataSource[] = [];
  for (let n = 0; n < count; n++) {
    const i = n * size;
    const j = Math.min(events.length, i + size);
    if (i < j) parts.push(buildPart(doc, events, i, j, n + 1, count, lookback));
  }
  return parts;
}
