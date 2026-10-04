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
  /** Adapter rule 'goal-key': normalized short goal (lowercase alphanumerics), for exact goal matching. */
  goalKey?: string;
  /** Adapter rule 'verify-goal': short goal contains verify / test / check / confirm / validate. */
  verifyGoal?: boolean;
}

export interface Payloads {
  task_created: { tasks: TaskSpec[] };
  task_assigned: { assignee: string; previousOwner?: string };
  dependency_created: { from: string; to: string; reason?: string };
  status_updated: {
    status: TaskStatus;
    /** Session ends (AI Village): STOP_USING_COMPUTER, CONSOLIDATE, or the agent's next START. */
    endReason?: 'stop' | 'consolidate' | 'next-start';
  };
  message: {
    /** Adapter rule 'human-speaker': written by a human participant, not an agent. */
    isHuman?: boolean;
    /** Adapter rule 'question-mark': the message ends with a question mark. */
    isQuestion?: boolean;
  };
  claim: {
    claimId: string;
    asserts: 'verification_passed' | 'complete';
    subject?: ArtifactRef;
    /** Adapter claim rule that produced this claim ('claim-sentence', 'claim-bare-url'). */
    rule?: string;
  };
  tool_result: {
    tool: string;
    runId: string;
    /** Only 'verification' results can support or contradict a pass/complete claim. */
    category: 'build' | 'verification' | 'execution';
    /** Deterministic rule that produced the verdict (adapters only), e.g. 'http-status'. */
    rule?: string;
    /** http-status: URL after redirects, when observable. */
    finalUrl?: string;
    /** test-summary: whether the full suite ran. */
    scope?: 'full' | 'partial';
    /** The command suppressed errors (|| true, 2>/dev/null, …), so it could not fail. */
    suppressed?: true;
    /** No stdout or stderr; such a verdict is never 'pass'. */
    emptyOutput?: true;
    subject?: ArtifactRef;
    /** 'inconclusive' = empty-output verdict: counts as neither supported nor contradicted. */
    outcome: 'pass' | 'fail' | 'inconclusive';
    output: string;
  };
  correction: { supersedes: string; addressedTo?: string[] };
  acknowledgement: { acknowledges: string };
  action: {
    action: string; referencesClaims: string[];
    /** Fixture/demo actions are simulated records; real session turns omit this. */
    simulated?: true;
    /** Session-complete actions (AI Village turns): the command or computer action, never the output body. */
    command?: string; computerAction?: string; turnId?: string;
    /** Hash of the FULL command (the displayed command is clipped), so repetition is judged on the whole command. */
    commandHash?: string;
    kind?: 'command' | 'computer' | 'none';
    /** Short hash of stdout+stderr, so identical outputs can be compared without storing them. */
    outputHash?: string; emptyOutput?: true;
  };
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
  /** Carried into a window part from earlier in the same window, as context for records in the part. */
  carried?: true;
  /** Chat room the record was posted in (chat-derived records). */
  room?: string;
  /** Adapter rule 'refs': URLs and file paths named in the record's text, command or output (normalized). */
  refs?: string[];
}

export type RecallEvent = { [K in EventType]: BaseEvent<K> }[EventType];
export type EventOf<T extends EventType> = BaseEvent<T>;

export interface Agent {
  id: string;
  name: string;
  role: string;
  color: string;
}

export interface ReferenceSeen {
  ref: string;
  sourceEventId: string;
  sequence: number;
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
    /** Set when a window was split to respect the event cap. */
    part?: { parent: string; parentLabel: string; index: number; count: number; carried: number; inRange: number };
  };
  /**
   * Lightweight reference_seen records carried into a window part: every URL/path named in a message or
   * output earlier in the whole window (and its 24 h lookback). `sequence` is the record's original window
   * sequence (parts keep window numbering); 0 means the lookback, before the window opens.
   */
  referencesSeen?: ReferenceSeen[];
  /** Optional evidence visibility experiment: records to withhold from the analysis input. */
  experiment?: { withhold: string[]; label: string; description: string };
}
