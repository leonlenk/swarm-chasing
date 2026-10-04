// Shared phrasing helpers for monitor findings. Wording only; no monitor logic lives here.

import type { ArtifactRef, EventOf } from '../../model/types';
import type { WorldState } from '../reconstruct';

export const subj = (s?: ArtifactRef) => (!s ? '?' : s.version === 'live' ? s.artifact : `${s.artifact}@${s.version}`);
export const isLive = (s?: { version: string }) => s?.version === 'live';

/** What the claim asserted, in plain words. */
export const asserted = (c: EventOf<'claim'>) => (isLive(c.payload.subject)
  ? `${subj(c.payload.subject)} is live`
  : c.payload.asserts === 'verification_passed' ? `verification passed for ${subj(c.payload.subject)}` : `${subj(c.payload.subject)} is complete`);

export const doneClause = (c: EventOf<'claim'>) => (c.statusAfter === 'done' ? ' and marked the task done' : '');

/** The kind of check, in plain words. */
export const checkNoun = (s?: { version: string }) => (isLive(s) ? 'recorded check of that URL' : 'verification of that exact version');

/** Why a cited record is unavailable, phrased for missing[]. */
export const unavailableWhy = (ws: WorldState, id: string) => (ws.refStatus(id) === 'withheld'
  ? 'withheld from this analysis by the evidence visibility experiment (present in the source)'
  : 'not present in the source dataset');
