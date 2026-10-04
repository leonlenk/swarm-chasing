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
  (e.type === 'message' && !e.payload.isHuman) || (e.type === 'quote' && !e.payload.isHuman) || e.type === 'directive' ||
  ((e.type === 'claim' || e.type === 'correction') && e.room !== undefined);

export const reachOf = (ws: WorldState, taskId: string | null) => (taskId ? dependencyReach(ws, taskId) : []);

/** Builds a Finding with the shared defaults. */
export function finding(f: Omit<Finding, 'reach' | 'missing' | 'claimId' | 'taskId'> & Partial<Pick<Finding, 'reach' | 'missing' | 'claimId' | 'taskId'>>, ws: WorldState): Finding {
  return { reach: reachOf(ws, f.taskId ?? null), missing: [], claimId: '', taskId: '', ...f };
}

/** Same-agent claims about a subject key, in order. */
export const claimsOn = (ws: WorldState, key: string) => subjectClaims(ws).filter((c) => keyOf(c) === key);

/** Display label for a subject key (URL or artifact@version). */
export const subjectLabel = (key: string) => key.replace(/@live$/, '');

// ---- Milestone 3 accessors (typed adapter fields only) ----

/** Failure reports (quotes) about a subject key, in order. */
export const quotesOf = (ws: WorldState, key: string) =>
  ws.visible.filter((e): e is EventOf<'quote'> => e.type === 'quote' && subjectKey(e.payload.subject) === key);

/** Does an agent chat record name the subject (claim/quote subject, correction of a claim on it, or message.names)? */
export function namesSubject(ws: WorldState, e: RecallEvent, key: string): boolean {
  if (e.type === 'claim' || e.type === 'quote') return subjectKey(e.payload.subject) === key;
  if (e.type === 'correction') return subjectKey(ws.claims.get(e.payload.supersedes)?.subject) === key;
  if (e.type === 'message') return !!e.payload.names?.some((n) => `${n}@live` === key);
  return false;
}

/** Agent chat that reports a failure: a quote, a correction, or a message flagged negative. */
export const isNegativeChat = (e: RecallEvent) =>
  e.type === 'quote' || e.type === 'correction' || (e.type === 'message' && !!e.payload.negative);

/** First agent chat by an agent after a sequence number (messages, summaries, claims, quotes, directives, corrections). */
export const nextChatBy = (ws: WorldState, agentId: string, after: number) =>
  ws.visible.find((e) => e.agentId === agentId && e.sequence > after && (isAgentChat(e) || (e.type === 'message' && !e.payload.isHuman) || e.type === 'claim' || e.type === 'correction'));

/** Passing / failing tool results (any category) of a subject key. */
export const resultsOf = (ws: WorldState, key: string) =>
  ws.visible.filter((e): e is EventOf<'tool_result'> => e.type === 'tool_result' && keyOf(e) === key);

/** Session ids (taskIds) that have task_created records, with their owner. */
export const sessionsOf = (ws: WorldState) =>
  ws.visible.flatMap((e) => (e.type === 'task_created' ? e.payload.tasks.map((t) => ({ spec: t, created: e })) : []));
