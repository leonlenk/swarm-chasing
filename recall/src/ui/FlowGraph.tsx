import { useMemo } from 'react';
import {
  BaseEdge, EdgeLabelRenderer, Handle, MarkerType, Position, ReactFlow, useInternalNode,
  type Edge, type EdgeProps, type Node, type NodeProps,
} from '@xyflow/react';
import { flowLayout, focusLayout, informationFlow, type FlowTone } from '../engine/flow';
import { useRecall } from './context';
import { initials } from './labels';
import { ICheck, IDoc, IX } from './icons';

type AgentData = { name: string; role?: string; color: string; warn: boolean; dim: boolean };
type EvData = { label: string; sub?: string; outcome?: 'pass' | 'fail'; dim: boolean };

function AgentFlowNode({ data }: NodeProps<Node<AgentData>>) {
  return (
    <div className={`agent-node ${data.warn ? 'warn' : ''} ${data.dim ? 'dim' : ''}`}>
      <Handle type="target" position={Position.Top} />
      <div className="disc" style={{ background: data.color }}>{initials(data.name)}</div>
      <div className="agent-label">
        <div className="agent-name">{data.name}</div>
        {data.role && <div className="agent-role">{data.role}</div>}
      </div>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

function EvidenceFlowNode({ data }: NodeProps<Node<EvData>>) {
  return (
    <div className={`ev-node ${data.outcome ?? ''} ${data.dim ? 'dim' : ''}`}>
      <Handle type="target" position={Position.Left} />
      <IDoc size={20} />
      <div className="txt"><b>{data.label}</b>{data.sub && <span>{data.sub}</span>}</div>
      {data.outcome && <span className={`ev-badge ${data.outcome}`} title={data.outcome === 'pass' ? 'Passed' : 'Failed'}>{data.outcome === 'pass' ? <ICheck size={11} /> : <IX size={11} />}</span>}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes = { agent: AgentFlowNode, evidence: EvidenceFlowNode };

/** Boundary point of a node toward (tx, ty): circle for agents, rectangle for evidence. */
function anchor(n: ReturnType<typeof useInternalNode>, tx: number, ty: number) {
  const p = n!.internals.positionAbsolute;
  const w = n!.measured.width ?? 0;
  const h = n!.measured.height ?? 0;
  if (n!.type === 'agent') {
    const cx = p.x + w / 2;
    const cy = p.y + 29;
    const d = Math.hypot(tx - cx, ty - cy) || 1;
    return { x: cx + ((tx - cx) / d) * 34, y: cy + ((ty - cy) / d) * 34, cx, cy };
  }
  const cx = p.x + w / 2;
  const cy = p.y + h / 2;
  const dx = tx - cx;
  const dy = ty - cy;
  const s = Math.min((w / 2 + 4) / Math.abs(dx || 1e-6), (h / 2 + 4) / Math.abs(dy || 1e-6));
  return { x: cx + dx * s, y: cy + dy * s, cx, cy };
}

/** Floating edge: boundary-to-boundary, gently curved so opposing edges don't overlap. */
function FloatingEdge({ id, source, target, style, markerEnd, label }: EdgeProps) {
  const sn = useInternalNode(source);
  const tn = useInternalNode(target);
  if (!sn || !tn) return null;
  const sc = anchor(sn, 0, 0);
  const tc = anchor(tn, 0, 0);
  const a = anchor(sn, tc.cx, tc.cy);
  const b = anchor(tn, sc.cx, sc.cy);
  const mx = (a.x + b.x) / 2;
  const my = (a.y + b.y) / 2;
  const len = Math.hypot(b.x - a.x, b.y - a.y) || 1;
  const bend = Math.min(60, len * 0.18);
  const qx = mx + (-(b.y - a.y) / len) * bend;
  const qy = my + ((b.x - a.x) / len) * bend;
  const path = `M${a.x},${a.y} Q${qx},${qy} ${b.x},${b.y}`;
  return (
    <>
      <BaseEdge id={id} path={path} style={style} markerEnd={markerEnd} />
      {label && (
        <EdgeLabelRenderer>
          <div className="edge-count" style={{ transform: `translate(-50%, -50%) translate(${(mx + qx) / 2}px, ${(my + qy) / 2}px)` }}>{label}</div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}
const edgeTypes = { floating: FloatingEdge };
const TONE_COLOR: Record<FlowTone, string> = {
  normal: '#8fb19a', unverified: 'var(--unknown)', correction: 'var(--correction)', ack: 'var(--green-800)',
};

export function FlowGraph({ focusAgent, showMentions = false, incidentsOnly = false }: { focusAgent?: string; showMentions?: boolean; incidentsOnly?: boolean }) {
  const { source, ws, agents, name, navigate, openRecord, findings, allFindings, input } = useRecall();
  const agentIds = useMemo(() => source?.agents.map((a) => a.id) ?? [], [source]);
  const globalPositions = useMemo(() => flowLayout(agentIds, source?.events ?? []), [agentIds, source]);
  // Focus layout uses subjects of findings across the full log (positions only; nodes still appear only when visible).
  const focusPositions = useMemo(() => {
    if (!incidentsOnly) return null;
    const keys = [...new Set(allFindings.map((f) => {
      const ev = input.events.find((e) => e.type === 'claim' && e.payload.claimId === f.claimId);
      return ev?.type === 'claim' && ev.payload.subject ? `${ev.payload.subject.artifact}@${ev.payload.subject.version}` : null;
    }).filter((x): x is string => !!x))];
    return keys.length ? focusLayout(source?.events ?? [], keys) : null;
  }, [incidentsOnly, allFindings, input, source]);
  const positions = focusPositions ?? globalPositions;
  const flow = useMemo(() => {
    const f = informationFlow(ws, agentIds, name);
    let edges = showMentions ? f.edges : f.edges.filter((e) => e.label !== 'mention');
    let nodes = f.nodes;
    if (incidentsOnly && findings.length) {
      // Keep the subjects behind findings, the agents linked to them, and links among those agents.
      const subjects = new Set(findings.map((x) => ws.claims.get(x.claimId)?.subject).filter(Boolean).map((s) => `ev:${s!.artifact}@${s!.version}`));
      const keepAgents = new Set(findings.flatMap((x) => [x.agentId]).map((a) => `agent:${a}`));
      edges.forEach((e) => { if (subjects.has(e.from)) keepAgents.add(e.to); if (subjects.has(e.to)) keepAgents.add(e.from); });
      const keep = new Set([...subjects, ...keepAgents]);
      edges = edges.filter((e) => keep.has(e.from) && keep.has(e.to));
      nodes = nodes.filter((n) => keep.has(n.id));
    }
    const linked = new Set(edges.flatMap((e) => [e.from, e.to]));
    return { nodes: nodes.filter((n) => linked.has(n.id) || n.kind === 'evidence'), edges };
  }, [ws, agentIds, name, showMentions, incidentsOnly, findings]);

  const warnAgents = useMemo(() => new Set(flow.edges.filter((e) => e.tone === 'unverified').map((e) => e.to.replace('agent:', ''))), [flow]);
  const linked = useMemo(() => {
    if (!focusAgent) return null;
    const id = `agent:${focusAgent}`;
    const s = new Set([id]);
    flow.edges.forEach((e) => { if (e.from === id) s.add(e.to); if (e.to === id) s.add(e.from); });
    return s;
  }, [focusAgent, flow]);

  const nodes: Node[] = useMemo(() => flow.nodes.map((n) => {
    const position = positions.get(n.id) ?? { x: 0, y: 0 };
    const dim = !!linked && !linked.has(n.id);
    if (n.kind === 'agent') {
      const a = agents.get(n.agentId!);
      return { id: n.id, type: 'agent', position, draggable: false,
        data: { name: n.label, role: a?.role, color: a?.color ?? 'var(--green-500)', warn: warnAgents.has(n.agentId!), dim } };
    }
    return { id: n.id, type: 'evidence', position, draggable: false, data: { label: n.label, sub: n.sublabel, outcome: n.outcome, dim } };
  }), [flow, positions, agents, warnAgents, linked]);

  const edges: Edge[] = useMemo(() => flow.edges.map((e) => {
    const color = TONE_COLOR[e.tone];
    const dim = !!linked && !(linked.has(e.from) && linked.has(e.to));
    return {
      id: e.id, source: e.from, target: e.to, type: 'floating',
      animated: e.tone === 'unverified',
      className: dim ? 'edge-dim' : '',
      label: e.eventIds.length > 1 ? `×${e.eventIds.length}` : undefined,
      style: { stroke: color, strokeWidth: e.tone === 'normal' ? 1.4 : 2.2, strokeDasharray: e.tone === 'correction' || e.provenance === 'inferred' ? '6 4' : undefined },
      markerEnd: { type: MarkerType.ArrowClosed, color, width: 14, height: 14 },
      data: { eventIds: e.eventIds },
    };
  }), [flow, linked]);

  const activeAgents = new Set(ws.visible.map((e) => e.agentId)).size;
  const shownAgents = flow.nodes.filter((n) => n.kind === 'agent').length;
  if (!nodes.length) {
    return <div className="empty-state"><b>No claim or evidence links yet</b>{activeAgents ? `${activeAgents} agents are active, but none has made a checkable claim or cited a record at this point.` : 'Scrub the timeline forward.'}</div>;
  }
  return (
    <>
    {activeAgents > shownAgents && (
      <div className="flow-note">{shownAgents} of {activeAgents} active agents shown · {incidentsOnly && findings.length ? 'only those linked to incident subjects' : 'others have no claim, check or reference links at this point'}</div>
    )}
    <ReactFlow
      key={`${source?.id}:${focusPositions ? 'focus' : 'all'}`}
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
      fitView
      fitViewOptions={{ padding: 0.12, maxZoom: 1.1 }}
      minZoom={0.25}
      maxZoom={1.8}
      nodesConnectable={false}
      proOptions={{ hideAttribution: true }}
      onNodeClick={(_, n) => {
        if (n.type === 'agent') navigate('agents', n.id.replace('agent:', ''));
        else {
          const ids = flow.nodes.find((x) => x.id === n.id)?.eventIds;
          if (ids?.length) openRecord(ids[ids.length - 1]);
        }
      }}
      onEdgeClick={(_, e) => {
        const ids = (e.data as { eventIds: string[] } | undefined)?.eventIds;
        if (ids?.length) openRecord(ids[ids.length - 1]);
      }}
    />
    </>
  );
}
