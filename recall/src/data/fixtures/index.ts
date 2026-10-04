// Sabotage fixtures, one per monitor id. The registry refuses any monitor without an entry here,
// and `npm run check` runs every fixture's expect/quiet/withhold assertions.

import type { DataSource } from '../../model/types';
import A from './A.json';
import B from './B.json';

export interface FixtureExpect {
  /** Overrides fixture.monitor for this assertion (used by multi-monitor integration fixtures). */
  monitor?: string;
  atCursor: number;
  /** Records removed from the analysis input for this assertion. */
  withhold?: string[];
  /** Every finding of the monitor at the cursor must have this state. */
  state?: 'active' | 'resolved' | 'insufficient';
  /** Exact number of findings of the monitor at the cursor. */
  count?: number;
  /** Record ids that must appear in the findings' evidence[]. */
  evidenceIds?: string[];
  /** Record ids that must be named in the findings' missing[]. */
  missing?: string[];
}

export interface FixtureBlock {
  monitor: string;
  expect: FixtureExpect[];
  /** Monitors that must produce no finding at any cursor of this fixture. */
  quiet?: string[];
}

export type FixtureDoc = DataSource & { fixture: FixtureBlock };

export const FIXTURES: Record<string, FixtureDoc> = {
  A: A as unknown as FixtureDoc,
  B: B as unknown as FixtureDoc,
};
