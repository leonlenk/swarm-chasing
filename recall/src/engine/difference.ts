import type { TaskState } from './reconstruct';

export type Difference = 'conflict' | 'insufficient' | 'agree';

/** Conflict: evidence contradicts the report. Insufficient: work reported but nothing visible backs it. */
export function difference(t: TaskState): Difference {
  if (t.evidenceStatus === 'contradicted') return 'conflict';
  if (t.evidenceStatus === 'unknown' && t.reportedStatus !== 'todo') return 'insufficient';
  return 'agree';
}
