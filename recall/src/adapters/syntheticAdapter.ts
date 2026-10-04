import type { DataSource, RecallEvent } from '../model/types';
import raw from '../data/synthetic-release.json';

const TYPES = new Set([
  'task_created', 'task_assigned', 'dependency_created', 'status_updated', 'message',
  'claim', 'tool_result', 'correction', 'acknowledgement', 'action', 'quote', 'directive',
]);

/** Validates a RECALL-native JSON document and returns events sorted by sequence. */
export function parseRecallDocument(doc: unknown): DataSource {
  const d = doc as Partial<DataSource>;
  if (!d || !Array.isArray(d.events) || !Array.isArray(d.agents)) {
    throw new Error('Not a RECALL document: expected { agents: [], events: [] }');
  }
  const seen = new Set<string>();
  for (const e of d.events as RecallEvent[]) {
    if (!e.id || typeof e.sequence !== 'number' || !TYPES.has(e.type)) {
      throw new Error(`Malformed event: ${JSON.stringify(e).slice(0, 120)}`);
    }
    if (seen.has(e.id)) throw new Error(`Duplicate event id ${e.id}`);
    // Auditability invariants (brief): every claim names the rule that produced it, and every inferred record does too.
    const rule = (e.payload as { rule?: unknown }).rule;
    if (e.type === 'claim' && !rule) throw new Error(`Claim ${e.id} has no payload.rule`);
    if (e.provenance === 'inferred' && !rule) throw new Error(`Inferred record ${e.id} (${e.type}) has no payload.rule`);
    seen.add(e.id);
  }
  const events = [...(d.events as RecallEvent[])].sort((a, b) => a.sequence - b.sequence);
  return {
    id: d.id ?? 'recall-doc',
    label: d.label ?? 'Imported RECALL document',
    kind: d.kind ?? 'synthetic',
    description: d.description ?? '',
    agents: d.agents,
    events,
    experiment: d.experiment,
    referencesSeen: d.referencesSeen,
    meta: d.meta ?? { origin: 'synthetic' },
  };
}

export function loadSyntheticRelease(): DataSource {
  return parseRecallDocument(raw);
}
