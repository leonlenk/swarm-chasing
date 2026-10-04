import { createContext, useContext } from 'react';
import type { Agent, DataSource } from '../model/types';
import type { LiveSessionEntry, ScopeIndex } from '../model/scope';
import type { AnalysisInput, WorldState } from '../engine/reconstruct';
import type { Finding } from '../engine/monitors';

export type View = 'overview' | 'propagation' | 'tasks' | 'agents' | 'incidents' | 'monitors' | 'evidence' | 'swarm' | 'sessions' | 'explorer' | 'subtasks';
export const VIEWS: View[] = ['overview', 'propagation', 'tasks', 'agents', 'incidents', 'monitors', 'evidence', 'swarm', 'sessions', 'explorer', 'subtasks'];

export interface SourceEntry {
  id: string;
  label: string;
  /** Source-menu group: the parent window label for split AI Village parts, or 'Demo' / 'Imported'. */
  group: string;
  origin: 'huggingface' | 'synthetic' | 'file' | 'live';
  /** Live sources: the session's entry in public/data/scope/index.json. */
  live?: LiveSessionEntry;
  part?: { index: number; count: number; parent: string };
  description?: string;
  window?: { from: string; to: string };
  counts?: Record<string, number>;
  highlight?: string;
  load: () => Promise<DataSource>;
}

export interface HfIndex {
  generatedAt: string;
  dataset: string;
  sources: {
    id: string; file: string; label: string; goal?: string; window?: { from: string; to: string };
    counts?: Record<string, number>; highlight?: string;
    parent?: string; parentLabel?: string; part?: number; parts?: number;
  }[];
}

export interface RecallState {
  // sources
  sources: SourceEntry[];
  source: DataSource | null;
  sourceEntry: SourceEntry | null;
  loading: string | null;
  error: string | null;
  selectSource: (id: string) => void;
  importFile: (f: File) => Promise<void>;
  /** public/data/scope/index.json from `swarm-mcp render recall`: store sources and live sessions (null if absent). */
  scope: ScopeIndex | null;
  /** Live source: keep the cursor at the newest record as the session records more. */
  following: boolean;
  setFollowing: (v: boolean) => void;
  // route
  view: View;
  param?: string;
  navigate: (view: View, param?: string) => void;
  // time
  cursor: number;
  minSeq: number;
  maxSeq: number;
  seek: (seq: number) => void;
  playing: boolean;
  togglePlay: () => void;
  replay: () => void;
  // analysis
  experimentOn: boolean;
  setExperimentOn: (v: boolean) => void;
  input: AnalysisInput;
  ws: WorldState;
  findings: Finding[];
  allFindings: Finding[];
  agents: Map<string, Agent>;
  name: (id: string) => string;
  // drawer + misc
  drawer: string | null;
  openRecord: (eventId: string | null) => void;
  reviewed: Set<string>;
  toggleReviewed: (findingId: string) => void;
  paletteOpen: boolean;
  setPaletteOpen: (v: boolean) => void;
}

export const Ctx = createContext<RecallState | null>(null);

export function useRecall(): RecallState {
  const v = useContext(Ctx);
  if (!v) throw new Error('useRecall outside RecallProvider');
  return v;
}

