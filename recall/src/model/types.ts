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
  | 'action'
  /** Chat that reports a failure of a named subject (rule 'correction-other' / 'human-negative'); never a same-agent correction. */
  | 'quote'
  /** Chat that @mentions an agent with an imperative (rule 'directive'). */
  | 'directive';

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

/** What a claim asserts (brief: a small vocabulary so monitors can be exact). */
export type Asserts = 'complete' | 'verification_passed' | 'live' | 'deployed' | 'fixed' | 'merged' | 'exists' | 'running' | 'reviewed';

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
    /** Adapter rule 'names-url': normalized URLs the message names (subject artifacts, version 'live'). */
    names?: string[];
    /** Adapter rule 'negative-lexicon': the message reports a failure (NEGATIVE_RE). */
    negative?: boolean;
    /** Adapter rule 'external-blame': blames the environment (bug | broken | not working | site is down). */
    blame?: boolean;
    /** Adapter rule 'own-error': names the agent's own error (my mistake, I forgot, typo, …). */
    ownError?: boolean;
    /** Adapter rule 'convention-token': #tags and [BRACKET] tags used in the message. */
    conventions?: string[];
  /** Rule 'question-to': agents @mentioned in a sentence containing '?' (messages with <= 2 @mentions). */
    questionTo?: string[];
  };
  quote: {
    rule: 'correction-other' | 'human-negative';
    subject: ArtifactRef;
    isHuman?: boolean;
    isQuestion?: boolean;
    /** The speaker's own most recent failing check of the subject, when one exists (adapter, by subject). */
    quotesEventId?: string;
  };
  directive: {
    rule: 'directive';
    to: string[];
    /** Content tokens of the directive (lowercase words, stopwords removed), for uptake matching. */
    tokens: string[];
    isQuestion?: boolean;
    questionTo?: string[];
  };
  claim: {
    claimId: string;
    asserts: Asserts;
    subject?: ArtifactRef;
    /** Adapter claim rule that produced this claim ('claim-sentence', 'claim-bare-url', 'tests-pass', …). Required (parseRecallDocument). */
    rule?: string;
    /** Rule 'hedged': the claim's sentence carries a hedge (may take, should be, I think, probably, pending). */
    hedged?: boolean;
    /** Rule 'repeat': identical subject + asserts by the same agent earlier in the window. */
    repeatOf?: string;
    /** Rule 'claim-number': a number stated next to a pass word ("47 tests pass", "200 OK"). */
    number?: { value: number; kind: 'tests-passed' | 'http-status' };
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
    /** Numbers parsed from the output (claim-number comparison): tests passed/failed, HTTP status. */
    observed?: { passed?: number; failed?: number; status?: number };
    exitCode?: number;
    /** 'inconclusive' = empty-output verdict: counts as neither supported nor contradicted. */
    outcome: 'pass' | 'fail' | 'inconclusive';
    output: string;
  };
  correction: { supersedes: string; addressedTo?: string[]; rule?: string };
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
    /** Rule 'write-action': artifacts the command writes ('file:<path>', or 'repo:*' for a git commit). */
    writes?: string[];
    /** Rule 'destructive-lexicon': the destructive idiom the command uses (rm -rf, push --force, reset --hard, …). */
    destructive?: string;
    /** Rule 'browser-nav': normalized URL a computer action navigated to. */
    navigates?: string;
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
  /** SwarmScope evidence id of the underlying record (e.g. `claude-code:msg:…`), citable with findings_record. */
  storeId?: string;
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
  kind: 'synthetic' | 'ai-village' | 'claude-code';
  description: string;
  agents: Agent[];
  events: RecallEvent[];
  /** Provenance of the whole source: where it came from and which slice it covers. */
  meta?: {
    /** 'live': a Claude Code session recorded by the swarm-live hooks (via `swarm-mcp render recall`). */
    origin: 'synthetic' | 'huggingface' | 'file' | 'live';
    dataset?: string;
    citation?: string;
    window?: { from: string; to: string };
    goal?: string;
    generatedAt?: string;
    /** Raw row counts read from each source table for this slice. */
    rows?: Record<string, number>;
    notes?: string[];
    /** Set when a window was split to respect the event cap. */
    part?: { parent: string; parentLabel: string; index: number; count: number; carried: number; carriedActions?: number; inRange: number };
    /** Live sources: the recorded session, whether it is still running, and whether it is a synthetic demo recording. */
    live?: { session: string; status: 'running' | 'stopped'; updated: string | null; synthetic: boolean; folder: string | null };
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
