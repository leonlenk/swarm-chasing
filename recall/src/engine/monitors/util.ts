// Shared, typed accessors over WorldState for monitors. No text is read here: only typed fields,
// subjects, sequence order, and adapter-provided attributes (refs, goalKey, verifyGoal, isHuman, room).

import type { ArtifactRef, EventOf, RecallEvent } from '../../model/types';
import { dependencyReach, type WorldState } from '../reconstruct';
import type { Finding } from './types';

export const subjectKey = (s?: ArtifactRef) => (s ? `${s.artifact}@${s.version}` : '');
export const keyOf = (e: RecallEvent) => ((e.type === 'claim' || e.type === 'tool_result') ? subjectKey(e.payload.subject) : '');

/** A verification verdict. 'inconclusive' (empty output) is neither pass nor fail: it is never a verification and resolves nothing. */
export const isVerification = (e: RecallEvent): e is EventOf<'tool_result'> =>
  e.type === 'tool_result' && e.payload.category === 'verification' && e.payload.outcome !== 'inconclusive';
export const isPass = (e: EventOf<'tool_result'>) => e.payload.outcome === 'pass';
export const isFail = (e: EventOf<'tool_result'>) => e.payload.outcome === 'fail';

/** Claims with a subject (the only claims monitors can relate to checks). */
export const subjectClaims = (ws: WorldState) =>
  ws.visible.filter((e): e is EventOf<'claim'> => e.type === 'claim' && !!e.payload.subject);

/** Verification checks of a subject key, in sequence order. */
export const checksOf = (ws: WorldState, key: string) =>
  ws.visible.filter((e): e is EventOf<'tool_result'> => isVerification(e) && keyOf(e) === key);

/** All verdicts (verification or execution) recorded in a task/session. */
export const verdictsIn = (ws: WorldState, taskId: string) =>
  ws.visible.filter((e): e is EventOf<'tool_result'> => e.type === 'tool_result' && e.taskId === taskId && e.payload.outcome !== 'inconclusive');

/** The task_created record that created a task. */
export const createdOf = (ws: WorldState, taskId: string) =>
  ws.visible.find((e): e is EventOf<'task_created'> => e.type === 'task_created' && e.payload.tasks.some((t) => t.taskId === taskId));

export const specOf = (ws: WorldState, taskId: string) =>
  createdOf(ws, taskId)?.payload.tasks.find((t) => t.taskId === taskId);

/** The record that ended a session (reported status 'ended' or 'done'), if visible. */
export const endOf = (ws: WorldState, taskId: string) =>
  ws.visible.find((e) => e.taskId === taskId && (e.type === 'status_updated' ? e.payload.status === 'ended' : e.statusAfter === 'ended'));

/** Session-complete actions (turns) of a task, in order. */
export const turnsIn = (ws: WorldState, taskId: string) =>
  ws.visible.filter((e): e is EventOf<'action'> => e.type === 'action' && e.taskId === taskId && !!e.payload.turnId);

/** Chat-derived records written by agents (messages, claims, corrections from chat). */
export const isAgentChat = (e: RecallEvent) =>
  (e.type === 'message' && !e.payload.isHuman) || ((e.type === 'claim' || e.type === 'correction') && e.room !== undefined);

export const reachOf = (ws: WorldState, taskId: string | null) => (taskId ? dependencyReach(ws, taskId) : []);

/** Builds a Finding with the shared defaults. */
export function finding(f: Omit<Finding, 'reach' | 'missing' | 'claimId' | 'taskId'> & Partial<Pick<Finding, 'reach' | 'missing' | 'claimId' | 'taskId'>>, ws: WorldState): Finding {
  return { reach: reachOf(ws, f.taskId ?? null), missing: [], claimId: '', taskId: '', ...f };
}

/** Same-agent claims about a subject key, in order. */
export const claimsOn = (ws: WorldState, key: string) => subjectClaims(ws).filter((c) => keyOf(c) === key);

/** Display label for a subject key (URL or artifact@version). */
export const subjectLabel = (key: string) => key.replace(/@live$/, '');
