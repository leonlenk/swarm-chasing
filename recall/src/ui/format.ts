import type { EventType, EvidenceStatus, RecallEvent, TaskStatus } from '../model/types';

export const time = (iso: string) =>
  new Date(iso).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'UTC' });

export const STATUS_LABEL: Record<TaskStatus, string> = {
  todo: 'To do', in_progress: 'In progress', blocked: 'Blocked', paused: 'Paused', done: 'Done', failed: 'Failed', ended: 'Session ended',
};

export const EVIDENCE_LABEL: Record<EvidenceStatus, string> = {
  supported: 'Supported', contradicted: 'Contradicted', unknown: 'Unknown',
};

export const TYPE_LABEL: Record<EventType, string> = {
  task_created: 'Tasks created',
  task_assigned: 'Assignment',
  dependency_created: 'Dependency',
  status_updated: 'Status update',
  message: 'Message',
  claim: 'Claim',
  tool_result: 'Tool result',
  correction: 'Correction',
  acknowledgement: 'Acknowledgement',
  action: 'Action',
};

/** Visual tone of an event on the timeline. */
export function eventTone(e: RecallEvent): string {
  switch (e.type) {
    case 'tool_result': return e.payload.outcome === 'pass' ? 'good' : 'bad';
    case 'correction': return 'warn';
    case 'claim': return 'claim';
    case 'acknowledgement': return 'ack';
    case 'action': return 'action';
    default: return 'neutral';
  }
}
