// Information flow between agents and evidence, derived only from explicit references in visible
// records (evidenceRefs, cited claims, acknowledged corrections) plus exact-subject links between a
// claim and checks of that same subject. Nothing is inferred from timing.

import type { ArtifactRef, Provenance, RecallEvent } from '../model/types';
import type { WorldState } from './reconstruct';

export type FlowTone = 'normal' | 'unverified' | 'correction' | 'ack';

export interface FlowNode {
  id: string;
  kind: 'agent' | 'evidence';
  agentId?: string;
  /** Evidence nodes: the checks of this subject visible at the cursor (latest last). */
  eventIds?: string[];
  subject?: ArtifactRef;
  label: string;
  sublabel?: string;
  /** Evidence nodes: outcome of the latest visible check. */
  outcome?: 'pass' | 'fail' | 'inconclusive';
}

export interface FlowEdge {
  id: string;
  from: string;
  to: string;
  tone: FlowTone;
  provenance: Provenance;
  eventIds: string[];
  label: string;
}

const key = (s: ArtifactRef) => `${s.artifact}@${s.version}`;
const agentNode = (id: string) => `agent:${id}`;
const subjectNode = (s: ArtifactRef) => `ev:${key(s)}`;

function shortSubject(s: ArtifactRef): string {
  const m = s.artifact.match(/^https?:\/\/([^/]+)(\/.*)?$/);
  const base = m ? (m[2] && m[2] !== '/' ? m[2].split('/').filter(Boolean).pop()! : m[1]) : s.artifact.replace(/^tests:/, 'tests ');
  return s.version === 'live' ? base : `${base} ${s.version}`;
}

/**
 * Subjects worth drawing: those a claim is about, or that a record cites. Checks of anything else
 * are routine and stay in the Evidence view.
 */
export function relevantSubjects(events: RecallEvent[]): Set<string> {
  const subjects = new Set<string>();
  const cited = new Set<string>();
  for (const e of events) {
    if (e.type === 'claim' && e.payload.subject) subjects.add(key(e.payload.subject));
    e.evidenceRefs.forEach((r) => cited.add(r));
  }
  for (const e of events) {
    if (e.type === 'tool_result' && e.payload.category === 'verification' && e.payload.subject && cited.has(e.id)) subjects.add(key(e.payload.subject));
  }
  return subjects;
}

function claimTone(ws: WorldState, claimId: string): FlowTone {
  const c = ws.claims.get(claimId);
  return c && (c.standing === 'superseded' || c.standing === 'contradicted') ? 'unverified' : 'normal';
}

export function informationFlow(ws: WorldState, agentIds: string[], agentName: (id: string) => string) {
  const nodes = new Map<string, FlowNode>();
  const edges = new Map<string, FlowEdge>();
  const relevant = relevantSubjects(ws.visible);
  const addEdge = (from: string, to: string, e: RecallEvent, tone: FlowTone, label: string, provenance: Provenance = e.provenance) => {
    if (from === to) return;
    const id = `${from}->${to}:${tone}:${label}`;
    const cur = edges.get(id);
    if (cur) { if (!cur.eventIds.includes(e.id)) cur.eventIds.push(e.id); }
    else edges.set(id, { id, from, to, tone, provenance, eventIds: [e.id], label });
  };
  const ensureAgent = (id: string) => {
    if (!nodes.has(agentNode(id))) nodes.set(agentNode(id), { id: agentNode(id), kind: 'agent', agentId: id, label: agentName(id) });
  };

  // Evidence: one node per relevant subject, aggregating its visible checks.
  for (const e of ws.visible) {
    if (e.type !== 'tool_result' || e.payload.category !== 'verification' || !e.payload.subject) continue;
    if (!relevant.has(key(e.payload.subject))) continue;
    const id = subjectNode(e.payload.subject);
    const n = nodes.get(id) ?? { id, kind: 'evidence' as const, subject: e.payload.subject, eventIds: [], label: shortSubject(e.payload.subject) };
    n.eventIds!.push(e.id);
    n.outcome = e.payload.outcome;
    nodes.set(id, n);
    ensureAgent(e.agentId);
    addEdge(agentNode(e.agentId), id, e, 'normal', 'checked', 'observed');
  }
  for (const n of nodes.values()) {
    if (n.kind !== 'evidence') continue;
    const outs = n.eventIds!.map((id) => ws.byId.get(id)).map((x) => (x?.type === 'tool_result' ? x.payload.outcome : null));
    const fails = outs.filter((o) => o === 'fail').length;
    n.sublabel = `${outs.length} check${outs.length > 1 ? 's' : ''}${fails ? ` · ${fails} failed` : ''} · latest ${n.outcome === 'pass' ? 'passed' : 'failed'}`;
  }

  // Claims link to their subject's evidence node (explicitly cited, or same exact subject).
  for (const e of ws.visible) {
    if (e.type !== 'claim' || !e.payload.subject) continue;
    const id = subjectNode(e.payload.subject);
    if (!nodes.has(id)) continue;
    ensureAgent(e.agentId);
    const cited = e.evidenceRefs.some((r) => nodes.get(id)!.eventIds!.includes(r));
    addEdge(id, agentNode(e.agentId), e, claimTone(ws, e.payload.claimId), cited ? 'cited' : 'claimed', cited ? e.provenance : 'inferred');
  }

  // Agent → agent: explicit references to another agent's claim, correction or record.
  for (const e of ws.visible) {
    const refs = new Set(e.evidenceRefs);
    if (e.type === 'acknowledgement') refs.add(e.payload.acknowledges);
    if (e.type === 'action') for (const cid of e.payload.referencesClaims) { const c = ws.claims.get(cid); if (c) refs.add(c.eventId); }
    for (const rid of refs) {
      const ref = ws.byId.get(rid);
      if (!ref || ref.agentId === e.agentId || ref.type === 'tool_result') continue;
      const tone: FlowTone = e.type === 'acknowledgement' ? 'ack' : ref.type === 'correction' ? 'correction'
        : ref.type === 'claim' ? claimTone(ws, ref.payload.claimId)
          : e.type === 'action' && e.payload.referencesClaims.some((c) => claimTone(ws, c) === 'unverified') ? 'unverified' : 'normal';
      ensureAgent(ref.agentId); ensureAgent(e.agentId);
      addEdge(agentNode(ref.agentId), agentNode(e.agentId), e, tone,
        tone === 'ack' ? 'acknowledged' : ref.type === 'correction' ? 'correction' : ref.type === 'claim' ? 'claim' : 'reference');
    }
    for (const m of e.mentions ?? []) {
      if (m !== e.agentId && agentIds.includes(m)) { ensureAgent(e.agentId); ensureAgent(m); addEdge(agentNode(e.agentId), agentNode(m), e, 'normal', 'mention'); }
    }
  }
  return { nodes: [...nodes.values()], edges: [...edges.values()] };
}

/**
 * Stable positions computed once from the full log: relevant subjects on an inner ring,
 * agents on an outer ring (ordered so agents sharing subjects sit near each other).
 */
export function flowLayout(agentIds: string[], events: RecallEvent[]) {
  const pos = new Map<string, { x: number; y: number }>();
  const subjects = [...relevantSubjects(events)];
  const firstSubject = new Map<string, number>();
  for (const e of events) {
    const s = e.type === 'claim' ? e.payload.subject : e.type === 'tool_result' ? e.payload.subject : undefined;
    if (!s) continue;
    const i = subjects.indexOf(key(s));
    if (i >= 0 && !firstSubject.has(e.agentId)) firstSubject.set(e.agentId, i);
  }
  const ordered = [...agentIds].sort((a, b) => (firstSubject.get(a) ?? 1e9) - (firstSubject.get(b) ?? 1e9));
  const n = Math.max(ordered.length, 1);
  const small = n <= 4;
  const rx = small ? 330 : Math.max(420, n * 34);
  const ry = small ? 170 : Math.max(260, n * 20);
  ordered.forEach((id, i) => {
    const a = (i / n) * Math.PI * 2 + (small ? Math.PI : -Math.PI / 2);
    pos.set(agentNode(id), { x: Math.cos(a) * rx, y: Math.sin(a) * ry });
  });
  const m = Math.max(subjects.length, 1);
  subjects.forEach((s, i) => {
    if (m === 1) { pos.set(`ev:${s}`, { x: -95, y: 60 }); return; }
    const a = (i / m) * Math.PI * 2 - Math.PI / 2;
    pos.set(`ev:${s}`, { x: Math.cos(a) * rx * 0.45 - 95, y: Math.sin(a) * ry * 0.45 });
  });
  return pos;
}

/**
 * Compact left→right layout for a focused set of subjects: agents who checked them (left),
 * the subjects (middle), agents who claimed them (right). Computed from the full log so
 * positions stay put while scrubbing.
 */
export function focusLayout(events: RecallEvent[], subjectKeys: string[]) {
  const pos = new Map<string, { x: number; y: number }>();
  const checkers: string[] = [];
  const claimants: string[] = [];
  for (const e of events) {
    const s = e.type === 'claim' ? e.payload.subject : e.type === 'tool_result' && e.payload.category === 'verification' ? e.payload.subject : undefined;
    if (!s || !subjectKeys.includes(key(s))) continue;
    if (e.type === 'claim') { if (!claimants.includes(e.agentId)) claimants.push(e.agentId); }
    else if (!checkers.includes(e.agentId)) checkers.push(e.agentId);
  }
  const left = checkers.filter((a) => !claimants.includes(a));
  const col = (ids: string[], x: number, gap: number) => {
    const h = (ids.length - 1) * gap;
    ids.forEach((id, i) => pos.set(id, { x, y: i * gap - h / 2 }));
  };
  col(left.map((a) => agentNode(a)), -420, 130);
  col(subjectKeys.map((s) => `ev:${s}`), -95, 96);
  col(claimants.map((a) => agentNode(a)), 300, 130);
  return pos;
}
