// AI · Repeated human correction ("had to say it twice").
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { claimsOn, finding, quotesOf, subjectLabel } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  const keys = new Set(ws.visible.flatMap((e) => (e.type === 'quote' && e.payload.isHuman ? [`${e.payload.subject.artifact}@${e.payload.subject.version}`] : [])));
  for (const key of keys) {
    const human = quotesOf(ws, key).filter((q) => q.payload.isHuman);
    const claims = claimsOn(ws, key);
    for (let i = 1; i < human.length; i++) {
      const h1 = human[i - 1]; const h2 = human[i];
      const between = claims.find((c) => c.sequence > h1.sequence && c.sequence < h2.sequence);
      if (!between) continue;
      const again = claims.find((c) => c.sequence > h2.sequence);
      out.push(finding({
        attributes: [again ? 'claimed again after the second correction' : 'no further claim after the second correction'],
        id: `AI:${h2.id}`, monitor: 'AI', claimId: between.payload.claimId, taskId: between.taskId ?? '', agentId: between.agentId,
        title: `Repeated human correction: ${subjectLabel(key)}`,
        summary: `A human reported ${subjectLabel(key)} failing twice; ${ws.agentName(between.agentId)} claimed it live in between` + (again ? `, and it was claimed again at #${again.sequence}.` : '.'),
        explanation: `${h1.id} and ${h2.id} are human failure reports naming ${subjectLabel(key)}; claim ${between.payload.claimId} lies between them.`,
        detectedAt: h2.sequence, detectedEventId: h2.id,
        evidence: [{ eventId: h1.id, role: 'First human report' }, { eventId: between.id, role: `Claim ${between.payload.claimId} in between` }, { eventId: h2.id, role: 'Second human report' },
          ...(again ? [{ eventId: again.id, role: 'Claimed again' }] : [])],
        state: 'active',
      }, ws));
      break;
    }
  }
  return out;
}

export const AI: MonitorDef = {
  id: 'AI', title: 'Repeated human correction', family: 'human', needs: ['quote', 'claim'], fixture: 'src/data/fixtures/AI.json',
  rule: 'Active when: Two human messages naming S with `NEGATIVE_RE`, and an agent claim of S live/pass between them\n' +
    'Resolves when: No further claim after second',
  run,
};
