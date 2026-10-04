// Monitor contract. A monitor is a pure function over WorldState at the cursor; it never reads
// text, never calls anything, and only links records that are visible at the cursor.

import type { EventType } from '../../model/types';
import type { WorldState } from '../reconstruct';

export type FindingState = 'active' | 'resolved' | 'insufficient';

export interface EvidenceLink {
  eventId: string;
  role: string;
}

export interface Finding {
  /** `${monitor}:${anchor}` — stable across cursor positions so selection and review state persist. */
  id: string;
  /** MonitorDef.id ('A', 'B', …). */
  monitor: string;
  title: string;
  /** One plain-language sentence, used as the main-screen headline. */
  summary: string;
  explanation: string;
  detectedAt: number;
  detectedEventId: string;
  taskId: string;
  claimId: string;
  agentId: string;
  evidence: EvidenceLink[];
  /** Structural dependency reach, not proven damage. */
  reach: string[];
  missing: string[];
  /** insufficient = the analysis lacks a record it needs to confirm or clear the finding. */
  state: FindingState;
  resolution?: { eventId: string; text: string };
}

export type MonitorFamily =
  | 'claim-evidence' | 'propagation' | 'belief' | 'session' | 'process' | 'swarm' | 'human' | 'meta';

/** What WorldState must contain for the monitor to be meaningful on a source. */
export type MonitorNeed = EventType | 'subject' | 'dependency';

export interface MonitorDef {
  /** Matches the catalog: 'A', 'B', 'C', … */
  id: string;
  title: string;
  family: MonitorFamily;
  /** Pseudo-code shown verbatim in the Monitors view. */
  rule: string;
  needs: MonitorNeed[];
  run(ws: WorldState): Finding[];
  /** Path of the sabotage fixture: `src/data/fixtures/<id>.json`. Required; the registry refuses without it. */
  fixture: string;
}
