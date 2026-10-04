// Sabotage fixtures, one per monitor id. The registry refuses any monitor without an entry here,
// and `npm run check` runs every fixture's expect/quiet/withhold assertions.

import type { DataSource } from '../../model/types';
import A from './A.json';
import B from './B.json';
import C from './C.json';
import G from './G.json';
import J from './J.json';
import X from './X.json';
import Z from './Z.json';
import AE from './AE.json';
import AF from './AF.json';
import AH from './AH.json';
import V from './V.json';
import U from './U.json';
import AM from './AM.json';
import AO from './AO.json';
import AS from './AS.json';
import AW from './AW.json';
import BC from './BC.json';
import BD from './BD.json';
import BP from './BP.json';

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
  /** Attribute strings every finding must carry. */
  attributes?: string[];
}

export interface FixtureBlock {
  monitor: string;
  expect: FixtureExpect[];
  /** Monitors that must produce no finding at any cursor of this fixture. */
  quiet?: string[];
  /** The monitor has no active state by ruling (Z): an insufficient expectation satisfies the not-vacuous check. */
  neverActive?: boolean;
  /** Why a monitor that is not in quiet[] fires on this fixture by design. */
  note?: string;
}

export type FixtureDoc = DataSource & { fixture: FixtureBlock };

export const FIXTURES: Record<string, FixtureDoc> = {
  A: A as unknown as FixtureDoc,
  B: B as unknown as FixtureDoc,
  C: C as unknown as FixtureDoc,
  G: G as unknown as FixtureDoc,
  J: J as unknown as FixtureDoc,
  X: X as unknown as FixtureDoc,
  Z: Z as unknown as FixtureDoc,
  AE: AE as unknown as FixtureDoc,
  AF: AF as unknown as FixtureDoc,
  AH: AH as unknown as FixtureDoc,
  V: V as unknown as FixtureDoc,
  U: U as unknown as FixtureDoc,
  AM: AM as unknown as FixtureDoc,
  AO: AO as unknown as FixtureDoc,
  AS: AS as unknown as FixtureDoc,
  AW: AW as unknown as FixtureDoc,
  BC: BC as unknown as FixtureDoc,
  BD: BD as unknown as FixtureDoc,
  BP: BP as unknown as FixtureDoc,
};
