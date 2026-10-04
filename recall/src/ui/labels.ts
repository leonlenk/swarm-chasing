import type { Finding } from '../engine/monitors';
import type { WorldState } from '../engine/reconstruct';
import type { View } from './context';
import { monitorById } from '../engine/monitors';

/** Plain-language incident names, used consistently across views. */
export function findingLabel(f: Finding): string {
  if (f.monitor === 'B') return 'Withdrawn claim used after correction';
  if (f.monitor !== 'A') return monitorById(f.monitor)?.title ?? f.monitor;
  if (f.state === 'insufficient') return 'Completion claim cannot be verified';
  return f.title.startsWith('Claimed live') ? 'Claimed live after a failed check' : 'Completion claimed despite failed check';
}

/** Agents involved in a finding, in order of first appearance in its evidence. */
export function findingAgents(f: Finding, ws: WorldState): string[] {
  const out: string[] = [];
  for (const ev of f.evidence) {
    const a = ws.byId.get(ev.eventId)?.agentId;
    if (a && !out.includes(a)) out.push(a);
  }
  if (!out.includes(f.agentId)) out.push(f.agentId);
  return out;
}

export const STATE_ORDER = { active: 0, insufficient: 1, resolved: 2 } as const;

export function headline(findings: Finding[], cursor: number): { text: string; tone: Finding['state'] | 'calm'; finding?: Finding } {
  if (!findings.length) return { text: `No discrepancies detected in events #1–#${cursor}.`, tone: 'calm' };
  // Callers may pre-rank (Overview ranks by triage bucket); otherwise rank by state.
  const top = findings[0]?.state === 'active' ? findings[0] : [...findings].sort((a, b) => STATE_ORDER[a.state] - STATE_ORDER[b.state] || b.detectedAt - a.detectedAt)[0];
  const more = findings.length - 1;
  return { text: top.summary + (more ? ` (+${more} more finding${more > 1 ? 's' : ''})` : ''), tone: top.state, finding: top };
}

export function timeAgo(fromIso: string, toIso: string): string {
  const s = Math.max(0, (Date.parse(toIso) - Date.parse(fromIso)) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)}m earlier`;
  if (s < 86400) return `${Math.round(s / 3600)}h earlier`;
  return `${Math.round(s / 86400)}d earlier`;
}

export function initials(name: string) {
  const words = name.replace(/[()[\]]/g, ' ').split(/[\s\-_.]+/).filter(Boolean);
  if (!words.length) return '?';
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  const digits = words.find((w) => /\d/.test(w));
  return (words[0][0] + (digits ? digits.replace(/\D/g, '').slice(0, 1) || words[1][0] : words[1][0])).toUpperCase();
}


export const VIEW_LABEL: Record<View, string> = {
  overview: 'Overview', propagation: 'Propagation', tasks: 'Tasks', agents: 'Agents', incidents: 'Incidents', monitors: 'Monitors', evidence: 'Evidence', swarm: 'Swarm',
  sessions: 'Live sessions', explorer: 'Explorer', subtasks: 'Subtasks', connect: 'Connect your agents',
};


/** Display-only cleanup of chat markdown: drops emphasis markers and collapses whitespace. */
export function plain(text: string, max?: number): string {
  const t = text.replace(/\*\*|__|`/g, '').replace(/^#+\s*/gm, '').replace(/\s+/g, ' ').trim();
  return max && t.length > max ? `${t.slice(0, max - 1)}…` : t;
}

/** Short human label for a claim subject (URL → host/path). */
export function subjectLabel(artifact?: string): string {
  if (!artifact) return '';
  const m = artifact.match(/^https?:\/\/([^/]+)(\/.*)?$/);
  if (!m) return artifact;
  const host = m[1].replace(/^www\./, '');
  const path = (m[2] ?? '').replace(/\/$/, '');
  return `${host}${path}`.length > 46 ? `${host.split('.')[0]}…${path.slice(-28)}` : `${host}${path}`;
}

/** Group findings that share a monitor and claim subject, newest first within each group. */
export function groupFindings<T extends { monitor: string; detectedAt: number; claimId: string }>(
  findings: T[], subjectOf: (f: T) => string,
): { key: string; head: T; rest: T[] }[] {
  const map = new Map<string, T[]>();
  for (const f of findings) {
    const key = `${f.monitor}|${subjectOf(f) || f.claimId}`;
    map.set(key, [...(map.get(key) ?? []), f]);
  }
  return [...map].map(([key, list]) => {
    const sorted = [...list].sort((a, b) => b.detectedAt - a.detectedAt);
    return { key, head: sorted[0], rest: sorted.slice(1) };
  });
}
