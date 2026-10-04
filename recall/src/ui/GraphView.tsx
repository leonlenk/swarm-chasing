import { useMemo } from 'react';
import { Background, Controls, MarkerType, ReactFlow, type Edge as FlowEdge } from '@xyflow/react';
import type { Agent } from '../model/types';
import type { WorldState } from '../engine/reconstruct';
import { TaskNode, type Highlight, type TaskFlowNode, type ViewMode } from './TaskNode';
import { difference } from '../engine/difference';

const nodeTypes = { task: TaskNode };

interface Props {
  ws: WorldState;
  positions: Map<string, { x: number; y: number }>;
  agents: Map<string, Agent>;
  mode: ViewMode;
  selectedTask: string | null;
  highlights: Map<string, Highlight>;
  focusTask: string | null;
  onSelectTask: (id: string | null) => void;
}

export function GraphView({ ws, positions, agents, mode, selectedTask, highlights, focusTask, onSelectTask }: Props) {
  const current = ws.visible[ws.visible.length - 1];
  const focusing = selectedTask !== null || highlights.size > 0;

  const nodes: TaskFlowNode[] = useMemo(() => [...ws.tasks.values()].map((t) => ({
    id: t.id,
    type: 'task',
    position: positions.get(t.id) ?? { x: 0, y: 0 },
    draggable: false,
    data: {
      task: t,
      agent: agents.get(t.owner),
      claims: t.claimsUsed.map((c) => ws.claims.get(c)).filter((c) => !!c),
      mode,
      selected: t.id === selectedTask,
      highlight: highlights.get(t.id),
      isFocus: t.id === focusTask,
      touchedNow: !!current && t.lastTouchedSeq === current.sequence,
      dimmed: focusing && t.id !== selectedTask && !highlights.has(t.id),
    },
  })), [ws, positions, agents, mode, selectedTask, highlights, focusTask, current, focusing]);

  const edges: FlowEdge[] = useMemo(() => ws.edges.map((e) => {
    const src = ws.tasks.get(e.from);
    const tgt = ws.tasks.get(e.to);
    const bad = mode !== 'reported' &&
      ((src && difference(src) === 'conflict') || (tgt && difference(tgt) === 'conflict'));
    const lit = focusing && (e.from === selectedTask || highlights.has(e.from)) && highlights.get(e.to) === 'reach';
    const color = lit ? 'var(--green-800)' : bad ? 'var(--contradicted)' : '#c4c8bb';
    return {
      id: e.id,
      source: e.from,
      target: e.to,
      animated: !!bad && tgt?.reportedStatus !== 'todo',
      className: [lit && 'edge-lit', focusing && !lit && 'edge-dim'].filter(Boolean).join(' '),
      style: { stroke: color, strokeWidth: lit || bad ? 2 : 1.4, strokeDasharray: e.provenance === 'inferred' ? '5 4' : undefined },
      markerEnd: { type: MarkerType.ArrowClosed, color, width: 16, height: 16 },
      label: e.provenance === 'declared' && ws.byId.get(e.eventId)?.type === 'dependency_created' ? 'gate added' : undefined,
      labelStyle: { fill: 'var(--muted)', fontSize: 11, fontFamily: 'var(--mono)' },
      labelBgStyle: { fill: 'var(--surface)' },
    };
  }), [ws, mode, focusing, selectedTask, highlights]);

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      fitView
      fitViewOptions={{ padding: 0.08, maxZoom: 1.15 }}
      minZoom={0.3}
      maxZoom={1.8}
      nodesConnectable={false}
      proOptions={{ hideAttribution: true }}
      onNodeClick={(_, n) => onSelectTask(n.id)}
      onPaneClick={() => onSelectTask(null)}
    >
      <Background gap={22} size={1} color="#e3e0d4" />
      <Controls showInteractive={false} position="bottom-right" />
    </ReactFlow>
  );
}
