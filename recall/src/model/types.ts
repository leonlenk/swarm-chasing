// RECALL event model. Every view is reconstructed from an ordered list of these.

export type Provenance = 'observed' | 'declared' | 'inferred';

/** 'ended' = a session/run stopped; unlike 'done' it makes no claim that the goal was achieved. */
export type TaskStatus = 'todo' | 'in_progress' | 'blocked' | 'paused' | 'done' | 'failed' | 'ended';

export type EvidenceStatus = 'supported' | 'contradicted' | 'unknown';

export type EventType =
  | 'task_created'
  | 'task_assigned'
  | 'dependency_created'
  | 'status_updated'
  | 'message'
  | 'claim'
  | 'tool_result'
  | 'correction'
  | 'acknowledgement'
  | 'action';

export interface ArtifactRef {
  artifact: string;
  version: string;
}

export interface TaskSpec {
  taskId: string;
  title: string;
  owner: string;
  dependsOn?: string[];
}

export interface Payloads {
  task_created: { tasks: TaskSpec[] };
  task_assigned: { assignee: string; previousOwner?: string };
  dependency_created: { from: string; to: string; reason?: string };
  status_updated: { status: TaskStatus };
  message: Record<string, never>;
  claim: {
    claimId: string;
    asserts: 'verification_passed' | 'complete';
    subject?: ArtifactRef;
  };
  tool_result: {
    tool: string;
    runId: string;
    /** Only 'verification' results can support or contradict a pass/complete claim. */
    category: 'build' | 'verification' | 'execution';
    /** Deterministic rule that produced the verdict (adapters only), e.g. 'http-status'. */
    rule?: string;
    subject?: ArtifactRef;
    outcome: 'pass' | 'fail';
    output: string;
  };
  correction: { supersedes: string; addressedTo?: string[] };
  acknowledgement: { acknowledges: string };
  action: { action: string; referencesClaims: string[]; simulated: true };
}

interface BaseEvent<T extends EventType> {
  id: string;
  timestamp: string;
  sequence: number;
  agentId: string;
  taskId: string | null;
  type: T;
  /** Human-readable message or log line as it appeared in the source. */
  text: string;
  payload: Payloads[T];
  /** Event ids this record cites as its basis. */
  evidenceRefs: string[];
  /** How the relationships carried by this event are known. */
  provenance: Provenance;
  /** Optional reported-status change attached to the event. */
  statusAfter?: TaskStatus;
  /** Agent ids this record explicitly @-mentions (adapter-inferred from text). */
  mentions?: string[];
  /** Link back to the original record in the source system, if any. */
  sourceUrl?: string;
}

export type RecallEvent = { [K in EventType]: BaseEvent<K> }[EventType];
export type EventOf<T extends EventType> = BaseEvent<T>;

export interface Agent {
  id: string;
  name: string;
  role: string;
  color: string;
}

export interface DataSource {
  id: string;
  label: string;
  kind: 'synthetic' | 'ai-village';
  description: string;
  agents: Agent[];
  events: RecallEvent[];
  /** Provenance of the whole source: where it came from and which slice it covers. */
  meta?: {
    origin: 'synthetic' | 'huggingface' | 'file';
    dataset?: string;
    citation?: string;
    window?: { from: string; to: string };
    goal?: string;
    generatedAt?: string;
    /** Raw row counts read from each source table for this slice. */
    rows?: Record<string, number>;
    notes?: string[];
  };
  /** Optional evidence visibility experiment: records to withhold from the analysis input. */
  experiment?: { withhold: string[]; label: string; description: string };
}
