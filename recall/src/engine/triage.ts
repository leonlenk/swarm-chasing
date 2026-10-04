// Triage (owner ruling 2026-10-04): where each finding is shown, and what to do about it.
//
//   open     — contradicted-class findings: the records contradict what was claimed (A, D, E, G, J, W, Y, AO, AP, AQ, AR, AY).
//   needs    — a claim the records cannot confirm yet: incident-grade C, Z, AM, AC, BM, BJ, BK, plus any
//              contradicted-class finding that is insufficient.
//   patterns — every other unresolved finding (process, swarm, session, human patterns), so nothing is hidden.
//   resolved — unchanged: every resolved finding.
//   count    — count-grade C (repo pushed, file exists, fixed without a subject, proc running): counts only.
//
// Pure functions over WorldState; the views never decide this themselves.

import type { Finding } from './monitors/types';
import type { WorldState } from './reconstruct';

export type Bucket = 'open' | 'needs' | 'patterns' | 'resolved' | 'count';

export const CONTRADICTED = new Set(['A', 'D', 'E', 'G', 'J', 'W', 'Y', 'AO', 'AP', 'AQ', 'AR', 'AY']);
export const NEEDS_EVIDENCE = new Set(['C', 'Z', 'AM', 'AC', 'BM', 'BJ', 'BK']);

export type SubjectType = 'url-live' | 'pages-built' | 'pr-merged' | 'tests' | 'repo-pushed' | 'file-exists' | 'proc-running' | 'no-subject' | 'other';

export function subjectType(s?: { artifact: string; version: string }): SubjectType {
  if (!s) return 'no-subject';
  if (s.artifact.startsWith('pages:')) return 'pages-built';
  if (s.artifact.startsWith('pr:')) return 'pr-merged';
  if (s.artifact.startsWith('tests:')) return 'tests';
  if (s.artifact.startsWith('repo:')) return 'repo-pushed';
  if (s.artifact.startsWith('file:')) return 'file-exists';
  if (s.artifact.startsWith('proc:')) return 'proc-running';
  if (/^https?:\/\//.test(s.artifact)) return 'url-live';
  return 'other';
}

/** Incident-grade subjects: a verification rule exists and the artifact is external. */
const INCIDENT_GRADE = new Set<SubjectType>(['url-live', 'pages-built', 'pr-merged', 'tests']);

export const COUNT_GRADE_LABEL: Record<string, string> = {
  'repo-pushed': 'repo pushed', 'file-exists': 'file exists', 'no-subject': 'fixed / done without a subject', 'proc-running': 'process running', other: 'other',
};

export const claimSubjectType = (f: Finding, ws: WorldState) => subjectType(ws.claims.get(f.claimId)?.subject);

export function bucketOf(f: Finding, ws: WorldState): Bucket {
  if (f.state === 'resolved') return 'resolved';
  if (f.monitor === 'C' && !INCIDENT_GRADE.has(claimSubjectType(f, ws))) return 'count';
  if (CONTRADICTED.has(f.monitor)) return f.state === 'active' ? 'open' : 'needs';
  if (NEEDS_EVIDENCE.has(f.monitor)) return 'needs';
  return 'patterns';
}

export function triage(findings: Finding[], ws: WorldState) {
  const by: Record<Bucket, Finding[]> = { open: [], needs: [], patterns: [], resolved: [], count: [] };
  for (const f of findings) by[bucketOf(f, ws)].push(f);
  return by;
}

/** Count-grade C grouped by subject type, then agent: every count opens its claims. */
export function countGradeClaims(findings: Finding[], ws: WorldState): { type: string; label: string; byAgent: Map<string, Finding[]>; total: number }[] {
  const types = new Map<string, Map<string, Finding[]>>();
  for (const f of findings) {
    if (bucketOf(f, ws) !== 'count') continue;
    const t = claimSubjectType(f, ws);
    const m = types.get(t) ?? new Map<string, Finding[]>();
    m.set(f.agentId, [...(m.get(f.agentId) ?? []), f]);
    types.set(t, m);
  }
  return [...types.entries()].map(([type, byAgent]) => ({ type, label: COUNT_GRADE_LABEL[type] ?? type, byAgent, total: [...byAgent.values()].reduce((n, l) => n + l.length, 0) }))
    .sort((a, b) => b.total - a.total);
}

// ---------------------------------------------------------------- remediation
//
// Deterministic next steps per monitor, filled from the finding's own records. They tell an operator (or the
// agent) what record would resolve the finding, which is exactly the monitor's "Resolves when" column. No model
// writes these; they are templates, and the UI labels them as such.

const subj = (f: Finding, ws: WorldState) => {
  const s = ws.claims.get(f.claimId)?.subject;
  return s ? s.artifact.replace(/^(repo|file|pr|pages|proc|tests):/, '') : 'the subject';
};

const checkFor = (f: Finding, ws: WorldState): string => {
  const s = ws.claims.get(f.claimId)?.subject;
  const t = subjectType(s);
  const a = subj(f, ws);
  if (t === 'url-live') return `curl -sSI ${a}  (record the status line and the final URL)`;
  if (t === 'pages-built') return `gh api repos/${a}/pages/builds/latest --jq .status`;
  if (t === 'pr-merged') return `gh pr view ${a.split('#')[1] ?? ''}${a.includes('/') ? ` -R ${a.split('#')[0]}` : ''} --json state`;
  if (t === 'tests') return `run the full suite in ${a} (no -k, no single file) and keep the summary line`;
  if (t === 'file-exists') return `ls -l ${a}`;
  if (t === 'proc-running') return `lsof -i :${a}  or  curl -sI http://localhost:${a}`;
  if (t === 'repo-pushed') return `git log --oneline -1 origin/HEAD in ${a}`;
  return 'a verification of the exact subject';
};

export interface Remediation { owner: string; steps: string[]; resolvesWith: string }

export function remediationFor(f: Finding, ws: WorldState): Remediation | null {
  if (f.state === 'resolved') return null;
  const who = ws.agentName(f.agentId);
  const a = subj(f, ws);
  const run = checkFor(f, ws);
  const claim = f.claimId || 'the claim';
  const R = (steps: string[], resolvesWith: string, owner = who): Remediation => ({ owner, steps, resolvesWith });
  switch (f.monitor) {
    case 'A': case 'AO': case 'G': case 'D': case 'E': case 'AG':
      return R([`Pause anything that depends on ${claim} (${a}).`, `Re-run the check: ${run}.`, `If it fails, ${who} posts a correction of ${claim} in the same room; if it passes, post the passing record.`],
        'a passing check of the subject, or a correction by the claimant');
    case 'J':
      return R([`Run one tie-breaking check of ${a} from a neutral agent: ${run}.`, 'Post both earlier runs next to it so the room sees why they differed.'], 'a later check of the subject by either agent');
    case 'AP':
      return R([`Re-check without following redirects to a different page: ${run}.`, 'If the final URL is a login or catch-all page, correct the claim.'], 'a pass whose final URL equals the claimed URL');
    case 'AQ':
      return R([`Check the public URL, not localhost: ${run}.`], 'a passing check of the public URL');
    case 'AR':
      return R([`Run the full suite: ${run}.`, 'Claim "tests pass" only from a full-scope summary.'], 'a full-scope passing run');
    case 'AU':
      return R(['Quote the number from the record, not from memory.', `Re-run and post the summary line: ${run}.`], 'a record showing the stated number');
    case 'W': case 'Y':
      return R([`${who} posts what failed (the failing record) before moving on.`, 'If the failure was the agent\'s own command, say so.'], 'a message naming the failure, or a pass');
    case 'C': case 'AM': case 'BP': case 'AE':
      return R([`Verify before relying on it: ${run}.`, `Attach the record to ${claim} in the room.`], 'a verification of the subject');
    case 'BJ':
      return R([`Re-run without error suppression (drop "|| true", "2>/dev/null"): ${run}.`], 'an unsuppressed passing run');
    case 'BK':
      return R([`Re-run so the command prints (e.g. curl -sSI, not -o /dev/null): ${run}.`], 'a non-empty run');
    case 'BM':
      return R([`Add a text verdict next to the screenshot: ${run}.`], 'a text verdict of the subject');
    case 'Z':
      return R([`Show where ${f.missing[0] ?? 'the reference'} came from: ls / curl it, or correct the message.`], 'the reference appearing in tool output');
    case 'AC':
      return R([`Ask ${f.missing[0]?.replace('observed uptake by ', '') ?? 'the addressee'} to acknowledge or start a matching session.`], 'a matching session or an acknowledgement');
    case 'F':
      return R([`Stop repeating ${claim}; run one check of ${a}: ${run}.`], 'a check inside the repeat chain');
    case 'H':
      return R([`${who} closes the hedge: post an unhedged claim with a passing record, or correct it.`], 'an unhedged claim with a pass, or a correction');
    case 'O':
      return R([`Re-check ${a} before re-asserting it: ${run}.`], 'a passing check after the re-assertion');
    case 'AT':
      return R([`Re-check ${a} after the last write: ${run}.`], 'a passing check after the write');
    case 'BI':
      return R(['Stop the destructive retry; read the failing verdict first.', 'Snapshot the working tree (git stash) before any further destructive command.'], 'no automatic resolution; review the session');
    case 'BD':
      return R([`Open a follow-up session that starts from the failing record, or post the failure in chat.`], 'no automatic resolution');
    case 'B':
      return R([`Ask the acting agent to acknowledge the correction and redo the action.`], 'an acknowledgement by the acting agent');
    case 'AH': case 'BN': case 'BR':
      return R(['Reply in the same room, naming the subject or answering the question.'], 'an agent reply in that room');
    case 'AI':
      return R([`Stop re-asserting ${a}; run ${run} and reply to the human with the record.`], 'no further claim after the second report');
    case 'X':
      return R(['Break the loop: change the command or read its output before running it again.'], 'the output changes');
    case 'U': case 'V':
      return R(['Coordinate in chat: one session owns the goal; the other reads its result.'], 'a later passing session');
    case 'BC':
      return R([`Run the verification the goal named: ${run}.`], 'a verification verdict in the session');
    case 'BL':
      return R(['Treat findings in this session as unconfirmed; re-run the key checks after the reset.'], 'not applicable (data-quality guard)');
    default:
      return R([`Review the linked records.`], 'see the monitor rule');
  }
}
