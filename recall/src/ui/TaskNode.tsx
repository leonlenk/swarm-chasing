import { Handle, Position, type Node, type NodeProps } from '@xyflow/react';
import type { Agent } from '../model/types';
import type { ClaimState, TaskState } from '../engine/reconstruct';
import { AgentChip, EvidencePill, StatusPill } from './Pills';
import { difference, type Difference } from '../engine/difference';

export type ViewMode = 'reported' | 'evidence' | 'difference';

/** How a task relates to the current selection. */
export type Highlight = 'cites' | 'reach' | 'acked' | 'stale';

export type TaskNodeData = {
  task: TaskState;
  agent?: Agent;
  claims: ClaimState[];
  mode: ViewMode;
  selected: boolean;
  highlight?: Highlight;
  isFocus: boolean;
  touchedNow: boolean;
  dimmed: boolean;
};

export type TaskFlowNode = Node<TaskNodeData, 'task'>;

const HIGHLIGHT_LABEL: Record<Highlight, string> = {
  cites: 'cites claim',
  reach: 'dependency reach',
  acked: 'acknowledged',
  stale: 'still cites old claim',
};

const DIFF_LABEL: Record<Difference, string> = {
  conflict: 'Accounts conflict',
  insufficient: 'Insufficient evidence',
  agree: 'Accounts agree',
};

export function TaskNode({ data }: NodeProps<TaskFlowNode>) {
  const { task, agent, claims, mode, selected, highlight, isFocus, touchedNow, dimmed } = data;
  const withdrawn = claims.filter((c) => c.standing === 'superseded' || c.standing === 'contradicted');
  const diff = difference(task);
  const accent = mode === 'reported' ? `rs-${task.reportedStatus}` : mode === 'evidence' ? `tn-${task.evidenceStatus}` : `df-${diff}`;
  return (
    <div
      className={[
        'task-node', accent, `mode-${mode}`,
        selected && 'is-selected', highlight && `hl-${highlight}`, isFocus && 'is-focus',
        touchedNow && 'touched', dimmed && 'dimmed',
        mode === 'difference' && diff === 'agree' && 'df-quiet',
      ].filter(Boolean).join(' ')}
    >
      <Handle type="target" position={Position.Left} />
      <div className="tn-head">
        <span className="tn-id">{task.id}</span>
        <span className="tn-title">{task.title}</span>
      </div>
      <div className="tn-owner">
        <AgentChip name={agent?.name ?? task.owner} color={agent?.color ?? '#888'} />
        {agent && <span className="tn-role">{agent.role}</span>}
      </div>
      {mode === 'difference' ? (
        <div className={`tn-diff df-${diff}`}>
          <span className="tn-diff-label">{DIFF_LABEL[diff]}</span>
          <span className="tn-diff-detail">
            <StatusPill status={task.reportedStatus} />
            <span className="vs">vs</span>
            <EvidencePill status={task.evidenceStatus} />
          </span>
        </div>
      ) : (
        <div className={`tn-status emph-${mode}`}>
          <div className="tn-col reported"><label>Reported</label><StatusPill status={task.reportedStatus} /></div>
          <div className="tn-col evidence"><label>Evidence</label><EvidencePill status={task.evidenceStatus} /></div>
        </div>
      )}
      {withdrawn.length > 0 && mode !== 'reported' && (
        <div className={`tn-flag ${task.evidenceStatus === 'contradicted' ? '' : 'past'}`}>
          {task.evidenceStatus === 'contradicted' ? 'relies on' : 'previously cited'}{' '}
          {withdrawn.map((c) => `${c.id} (${c.standing})`).join(', ')}
        </div>
      )}
      {highlight && !selected && <div className={`tn-reach hl-tag-${highlight}`}>{HIGHLIGHT_LABEL[highlight]}</div>}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}
