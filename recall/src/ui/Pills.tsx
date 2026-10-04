import type { EvidenceStatus, Provenance, TaskStatus } from '../model/types';
import type { Finding } from '../engine/monitors';
import { EVIDENCE_LABEL, STATUS_LABEL } from './format';
import { initials } from './labels';

export function StatusPill({ status }: { status: TaskStatus }) {
  return <span className={`pill status status-${status}`}>{STATUS_LABEL[status]}</span>;
}

export function EvidencePill({ status }: { status: EvidenceStatus }) {
  return <span className={`pill ev-${status}`}><span className="pdot" />{EVIDENCE_LABEL[status]}</span>;
}

export function ProvenanceTag({ p }: { p: Provenance }) {
  return <span className={`prov prov-${p}`} title={`Relationship provenance: ${p}`}>{p}</span>;
}

const STATE_LABEL: Record<Finding['state'], string> = { active: 'Active', insufficient: 'Insufficient evidence', resolved: 'Resolved' };
export function StatePill({ state, reviewed }: { state: Finding['state']; reviewed?: boolean }) {
  if (reviewed) return <span className="state-pill state-reviewed">Reviewed</span>;
  return <span className={`state-pill state-${state}`}>{STATE_LABEL[state]}</span>;
}

export function Avatar({ name, color, size }: { name: string; color?: string; size?: 'sm' | 'lg' }) {
  return <span className={`avatar ${size ?? ''}`} style={{ background: color ?? 'var(--green-500)' }} title={name}>{initials(name)}</span>;
}

export function AgentChip({ name, color }: { name: string; color: string }) {
  return (
    <span className="row" style={{ gap: 8, minWidth: 0 }}>
      <Avatar name={name} color={color} size="sm" />
      <span style={{ fontWeight: 500, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{name}</span>
    </span>
  );
}

/** Real series sparkline. Draws the given values; no smoothing, no invented points. */
export function Spark({ values, tone = 'green', width = 150, height = 34 }: { values: number[]; tone?: 'green' | 'amber' | 'red'; width?: number; height?: number }) {
  if (values.length < 2) return <svg width={width} height={height} aria-hidden />;
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const pts = values.map((v, i) => [(i / (values.length - 1)) * (width - 4) + 2, height - 3 - ((v - min) / span) * (height - 8)] as const);
  const d = pts.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  const color = tone === 'amber' ? 'var(--unknown)' : tone === 'red' ? 'var(--contradicted)' : 'var(--green-500)';
  const [lx, ly] = pts[pts.length - 1];
  return (
    <svg width={width} height={height} className="kpi-spark" aria-hidden>
      <path d={d} fill="none" stroke={color} strokeWidth={1.6} strokeLinejoin="round" />
      <circle cx={lx} cy={ly} r={2.6} fill={color} />
    </svg>
  );
}
