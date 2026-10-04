// Data written by `swarm-mcp render recall` (swarm_mcp/src/swarm_mcp/scope/viz/recall_bundle.py) under
// public/data/scope/. The explorer and subtask payloads are the exact payloads of `render timeline` and
// `render subtasks` (timeline_html.build_timeline, subtasks_html.build_subtasks); their compact encodings are
// documented field by field below. All text is masked in Python and is untrusted agent output: render as text only.

export interface ScopeSource {
  source: string;
  adapter: string;
  ingestedAt?: string | null;
  counts: Record<string, number>;
  span: [string | null, string | null];
  notes: string[];
  explorer?: { file: string; bytes: number; lanes: number; sampled: boolean } | null;
  subtasks?: { file: string; bytes: number; units: number; edges: number } | null;
  errors?: string[];
}

export interface LiveSessionEntry {
  id: string;
  synthetic: boolean;
  file: string;
  label: string;
  folder: string | null;
  status: 'running' | 'stopped';
  start: string | null;
  updated: string | null;
  agents: number;
  subagents: number;
  actions: number;
  errors: number;
  messages: number;
}

export interface ScopeIndex {
  v: 1;
  generatedAt: string;
  store: string | null;
  sources: ScopeSource[];
  live: { recordings: string | null; sessions: LiveSessionEntry[]; updatedAt?: string | null; note?: string };
}

/** One recorded Claude Code session: its main agent and subagents (claude_code adapter rows, store evidence ids). */
export interface LiveSession {
  v: 1;
  kind: 'claude-code-session';
  source: string;
  session: string;
  synthetic: boolean;
  label: string;
  folder: string | null;
  status: 'running' | 'stopped';
  start: string | null;
  updated: string | null;
  agents: { id: string; name: string; kind: 'main' | 'subagent'; agentType: string | null; parent: string | null; task: string; status: string | null; first: string | null; last: string | null }[];
  periods: { id: string; agent: string; kind: string; label: string; start: string | null; end: string | null; parent: string | null; status: string | null }[];
  messages: { id: string; author: string; to: string[]; replyTo: string | null; t: string | null; type: string; text: string }[];
  actions: { id: string; agent: string; run: string | null; t: string | null; end: string | null; tool: string; text: string; command: string | null; output: string; status: string | null; spawned: string | null }[];
  notes: string[];
}

// ---------- explorer payload (timeline_html.build_timeline, "v": 2) ----------

export interface ExplorerLane { name: string; id: string; n: number; first: number; kind: string; lab: string | null; labg: string | null; shown: number }
export interface ExplorerRecap {
  totals: { messages?: number; agent_messages?: number; actions?: number; actors_active?: number; baseline_agent_messages?: number };
  terms: { term: string; n: number; n_before: number; agents: number; why: string; first_id?: string | null }[];
  bursts: { channel: string | null; s: number | null; e: number | null; n: number; agents: string[]; ids: string[] }[];
  baseline: boolean;
  /** lane index -> [messages, actions] in the window */
  act: Record<string, [number, number]>;
  /** [lane i, lane j, mentions of j by i] */
  ment: [number, number, number][];
}
export interface ExplorerMoment {
  kind: 'burst' | 'silence' | 'partner_shift' | 'first_use' | string;
  t: number | null; e: number | null; agent: string | null; lane: number | null; channel: string | null;
  term: string | null; score: number | null; why: string; ids: string[];
}
export interface ExplorerArc {
  /** [bin start ms, messages, actions] */
  bins: [number, number, number][];
  bin_days: number;
  partners: { id: string; label: string; s: number | null; e: number | null; out: number; in: number; to: [string, number][]; from: [string, number][]; js: number | null }[];
  terms: { term: string; role: 'coined' | 'adopted'; first_id: string | null; n: number; n_total: number; agents: number; adopters?: string[] | number; t: number | null }[];
  term_counts: Record<string, number>;
  notes: string[];
}
/** A metric over days: each point is [day index into starts, raw, rolling mean, lo, hi, numerator, denominator]. */
export interface ExplorerSeries {
  label: string; unit: string | null; kind: string | null; by: string | null; window: number | null;
  starts: number[];
  groups: { key: string; name: string; pts: [number, number | null, number | null, number | null, number | null, number | null, number | null][] }[];
  notes: string[];
}
export interface ExplorerPayload {
  v: 2;
  /** epoch ms; row times are seconds after it */
  t0: number;
  t: number[]; c: number[]; au: number[]; len: number[];
  /** evidence ids are idp + id[i] */
  idp: string; id: string[];
  /** masked snippets */
  s: string[];
  actors: { name: string; id: string; kind: string; lab: string | null; labg: string | null }[];
  lanes: ExplorerLane[];
  /** per lane, flat [bin, channel index, count]* ; bin start = base + bin * bin ms */
  dens: { bin: number; base: number; lanes: number[][] };
  /** per lane, flat [bin, count]* (actions) */
  acts: number[][];
  /** flat [bin, from lane, to lane, count]* */
  ment: number[];
  periods: { id: string; kind: string; label: string; s: number; e: number | null }[];
  days: { day_one: string; tz: string; basis: string } | null;
  channels: { name: string; n: number; slot: number }[];
  start: number; end: number; sampled: boolean;
  x: {
    recap_all?: ExplorerRecap;
    recaps?: Record<string, ExplorerRecap>;
    moments?: ExplorerMoment[];
    arcs?: Record<string, ExplorerArc>;
    agent_rates?: { starts: number[]; window: number; notes: string[]; lanes: Record<string, ExplorerSeries['groups'][number]['pts']> };
    series?: ExplorerSeries[];
    errors?: string[];
  };
  meta: { source: string | null; total: number; humans: number; n_agents: number; n_lanes: number; lane_total: number; marks: number; sampled: boolean; range: [string | null, string | null]; days: string | null; generated: string; snippet_chars: number };
}

// ---------- subtask payload (subtasks_html.build_subtasks) ----------

/** [evidence id, short label, title, author actor (-1 none), state, completed (1/0/null), start ms, end ms, actors touching, chat msgs, tags] */
export type SubtaskUnit = [string, string, string, number, string, number | null, number | null, number | null, number[], number, string[]];
/** [src unit, dst unit, kind, giver actor, taker actor, evidence ids, artifacts, score (duplicates)] */
export type SubtaskEdge = [number, number, 'builds_on' | 'integrates' | 'tests' | 'fixes' | 'resubmits' | 'duplicate' | string, number, number, string[], string[], number | null];
export interface SubtasksPayload {
  corpus: string; unit: string; action: string; artifact: string; tag: string | null;
  methods: string[]; method_label: Record<string, string>; method_desc: Record<string, string>;
  unfinished: string[]; levels: string[];
  actors: string[]; slots: number;
  units: SubtaskUnit[];
  /** per unit: [neighbour unit, similarity, {method: [score, shared terms]}] */
  nbrs: [number, number, Record<string, [number, string[]]>][][];
  edges: SubtaskEdge[];
  /** method -> level -> subtasks, each a list of unit indices */
  clusters: Record<string, Record<string, number[][]>>;
  names: Record<string, Record<string, string[]>>;
  keywords: Record<string, Record<string, string[]>>;
  objectives: Record<string, Record<string, (string | null)[]>>;
  /** unit index whose title names the subtask, -1 keywords, 'llm' / 'agent' */
  name_src: Record<string, Record<string, (number | string)[]>>;
  /** adjusted Rand index between methods (medium level) */
  agreement: Record<string, Record<string, number>>;
  /** active time segments [start ms, end ms]; idle gaps between them are compressed */
  segments: [number, number][];
  notes: string[];
}
