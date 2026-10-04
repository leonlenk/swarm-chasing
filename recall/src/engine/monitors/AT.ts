// AT · Verified before the edit.
import type { EventOf } from '../../model/types';
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { finding, isPass, keyOf, resultsOf, subjectClaims, subjectLabel } from './util';

const writesTo = (a: EventOf<'action'>, artifact: string) =>
  !!a.payload.writes?.some((w) => w === artifact || (w === 'repo:*' && artifact.startsWith('repo:')) || (artifact.startsWith('file:') && w.startsWith('file:') && (artifact.endsWith(`/${w.slice(5)}`) || w.endsWith(`/${artifact.slice(5)}`))));

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const c of subjectClaims(ws)) {
    const artifact = c.payload.subject!.artifact;
    if (!artifact.startsWith('repo:') && !artifact.startsWith('file:')) continue;
    const key = keyOf(c);
    const passes = resultsOf(ws, key).filter(isPass);
    const support = passes.filter((k) => k.sequence < c.sequence).pop();
    if (!support) continue;
    const sessionActions = ws.visible.filter((e): e is EventOf<'action'> => e.type === 'action' && e.agentId === c.agentId && !!e.payload.turnId && e.sequence < c.sequence &&
      (!c.taskId || e.taskId === c.taskId || e.taskId === support.taskId));
    const lastWrite = sessionActions.filter((a) => writesTo(a, artifact)).pop();
    if (sessionActions.length && (!lastWrite || lastWrite.sequence < support.sequence)) continue;
    const after = lastWrite ? passes.find((k) => k.sequence > lastWrite.sequence) : undefined;
    out.push(finding({
      id: `AT:${c.id}`, monitor: 'AT', claimId: c.payload.claimId, taskId: c.taskId ?? '', agentId: c.agentId,
      title: `Verified before the edit: ${subjectLabel(key)}`,
      summary: lastWrite
        ? `${ws.agentName(c.agentId)}'s claim about ${subjectLabel(key)} rests on a check that ran before the session last wrote to it (#${lastWrite.sequence})` + (after ? `; a check after the write passed at #${after.sequence}.` : '.')
        : `${ws.agentName(c.agentId)}'s claim about ${subjectLabel(key)}: the session's write actions are not in this slice.`,
      explanation: `Supporting pass ${support.id} (#${support.sequence}); ` + (lastWrite ? `last write to ${subjectLabel(key)} ${lastWrite.id} (#${lastWrite.sequence}).` : 'no session actions are visible to place the writes.'),
      detectedAt: c.sequence, detectedEventId: c.id,
      evidence: [{ eventId: support.id, role: 'Check before the edit' }, ...(lastWrite ? [{ eventId: lastWrite.id, role: 'Last write to the artifact' }] : []),
        { eventId: c.id, role: `Claim ${c.payload.claimId}` }, ...(after ? [{ eventId: after.id, role: 'Check after the write' }] : [])],
      missing: !lastWrite ? ['Session write actions (not in this slice).'] : after ? [] : [`No passing check of ${subjectLabel(key)} after the last write.`],
      state: !lastWrite ? 'insufficient' : after ? 'resolved' : 'active',
      resolution: after ? { eventId: after.id, text: `Check after the write passed at #${after.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const AT: MonitorDef = {
  id: 'AT', title: 'Verified before the edit', family: 'claim-evidence', needs: ['claim', 'tool_result', 'action', 'subject'], fixture: 'src/data/fixtures/AT.json',
  rule: 'Active when: Claim about `repo:` or `file:` cites a check that precedes the session\'s last write action to that artifact (commit, file write)\n' +
    'Resolves when: A check after the write passes\n' +
    'Insufficient when: Write actions not in slice',
  run,
};
