import { useMemo, useState } from 'react';
import { MarkerType, ReactFlow, ReactFlowProvider, Handle, Position, type Edge, type Node, type NodeProps } from '@xyflow/react';
import { useRecall } from '../context';
import { claimLineage, rankedClaims, type LineageStep, type StepKind } from '../../engine/lineage';
import { claimImpact } from '../../engine/reconstruct';
import { computeLayout } from '../../engine/layout';
import { GraphView } from '../GraphView';
import { Timeline } from '../Timeline';
import { Avatar } from '../Pills';
import type { Highlight } from '../TaskNode';
import { ICheck, IClock, IExport, IExternal, IUsers } from '../icons';
import { plain, subjectLabel } from '../labels';

const hm = (iso: string) => new Date(iso).toISOString().slice(11, 16);
const hms = (iso: string) => new Date(iso).toISOString().slice(11, 19);

const KIND_LABEL: Record<StepKind, string> = {
  evidence: 'Cited evidence', introduced: 'Introduced claim', used: 'Acted on claim', repeated: 'Repeated the claim', checked: 'Checked the subject', correction: 'Correction shared',
  acknowledged: 'Acknowledged correction', used_after: 'Used after correction', changed: 'Changed course',
};
const COLUMN: Record<StepKind, number> = { evidence: -1, introduced: 0, used: 1, repeated: 1, checked: 1, correction: 2, acknowledged: 3, used_after: 3, changed: 3 };
const PHASE: Record<number, string> = { 0: 'Claim introduced', 1: 'Repeated, used & checked', 2: 'Correction received', 3: 'After correction' };
const COL_W = 246;

type CardData = { step: LineageStep; name: string; color?: string; selected: boolean; outcome?: 'pass' | 'fail' | 'inconclusive'; cites?: { id: string; label: string; outcome: 'pass' | 'fail' | 'inconclusive' }[]; count?: number; passed?: number; failed?: number };
type HeadData = { label: string; phase: string };

function LineageCard({ data }: NodeProps<Node<CardData>>) {
  const { step, name, color, selected, outcome, cites, count = 1, passed = 0, failed = 0 } = data;
  const tag = step.kind === 'correction' ? <span className="tag correction">Correction</span>
    : step.kind === 'used_after' ? <span className="tag stale">After correction</span>
      : (step.kind === 'evidence' || step.kind === 'checked') && outcome === 'fail' ? <span className="tag stale">Check failed</span>
        : step.kind === 'evidence' || step.kind === 'checked' ? <span className="tag real">Check passed</span>
          : step.kind === 'repeated' ? <span className="tag disputed" title="Linked by identical subject, not an explicit reference">Same subject</span> : null;
  return (
    <div className={`lin-card k-${step.kind} ${outcome ?? ''} ${selected ? 'sel' : ''}`}>
      <Handle type="target" position={Position.Left} />
      <div className="lin-top">
        <Avatar name={name} color={color} />
        <div><b>{name}</b><span>{step.kind === 'repeated' && count > 1 ? `Repeated the claim ${count}×` : step.kind === 'checked' && count > 1 ? `Checked ${count}× · ${passed} passed · ${failed} failed` : KIND_LABEL[step.kind]}</span></div>
      </div>
      <div className="lin-quote">“{plain(step.text, 260)}”</div>
      {cites && cites.length > 0 && (
        <div className="lin-cites">
          {cites.map((c) => <span key={c.id} className={`lin-cite ${c.outcome}`}>cites {c.label} · {c.outcome === 'pass' ? 'passed' : 'failed'}</span>)}
        </div>
      )}
      <div className="lin-foot"><span className="evt-id">{count > 1 ? `latest · ${hm(step.timestamp)}` : `${step.eventId.length > 14 ? `${step.eventId.slice(0, 12)}…` : step.eventId} · ${hm(step.timestamp)}`}</span>{tag}</div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}
function ColumnHead({ data }: NodeProps<Node<HeadData>>) {
  return <div className="lin-col-head">{data.label}<small>{data.phase}</small></div>;
}
const nodeTypes = { card: LineageCard, head: ColumnHead };

export function Propagation() {
  const { ws, param, navigate, agents, name, openRecord, seek, source } = useRecall();
  const ranked = useMemo(() => rankedClaims(ws), [ws]);
  // One chip per subject (earliest claim represents the group); subject-less claims stay individual.
  const chips = useMemo(() => {
    const groups = new Map<string, typeof ranked>();
    for (const c of ranked) {
      const k = c.subject ? `${c.subject.artifact}@${c.subject.version}` : c.id;
      groups.set(k, [...(groups.get(k) ?? []), c]);
    }
    return [...groups.values()].map((g) => {
      const first = [...g].sort((a, b) => (ws.byId.get(a.eventId)?.sequence ?? 0) - (ws.byId.get(b.eventId)?.sequence ?? 0))[0];
      return { claim: first, count: g.length, ids: new Set(g.map((x) => x.id)) };
    });
  }, [ranked, ws]);
  const claimId = param && ws.claims.has(param) ? param : ranked[0]?.id;
  const lineage = useMemo(() => (claimId ? claimLineage(ws, claimId) : null), [ws, claimId]);
  const [mode, setMode] = useState<'transmission' | 'impact'>('transmission');
  const [tab, setTab] = useState<'source' | 'context'>('source');
  const [sel, setSel] = useState<string | null>(null);
  const positions = useMemo(() => computeLayout(source?.events ?? []), [source]);

  const { nodes, edges } = useMemo(() => {
    if (!lineage) return { nodes: [] as Node[], edges: [] as Edge[] };
    const colOf = (s: LineageStep) => (lineage.correction && (s.kind === 'repeated' || s.kind === 'checked') && s.sequence > lineage.correction.sequence ? 3 : COLUMN[s.kind]);
    const shown = lineage.steps.filter((s) => s.kind !== 'evidence');
    const cols = [...new Set(shown.map(colOf))].sort((a, b) => a - b);
    const colIndex = new Map(cols.map((c, i) => [c, i]));
    const rows = new Map<number, number>();
    const n: Node[] = [];
    for (const c of cols) {
      const inCol = shown.filter((s) => colOf(s) === c);
      const t0 = hm(inCol[0].timestamp);
      const t1 = hm(inCol[inCol.length - 1].timestamp);
      n.push({ id: `head-${c}`, type: 'head', position: { x: colIndex.get(c)! * COL_W, y: -56 }, draggable: false, selectable: false, data: { label: t0 === t1 ? t0 : `${t0}–${t1}`, phase: PHASE[c] } });
    }
    const cites = lineage.steps.filter((s) => s.kind === 'evidence').map((s) => {
      const ev = ws.byId.get(s.eventId);
      return ev?.type === 'tool_result' ? { id: ev.id, label: ev.payload.runId, outcome: ev.payload.outcome } : null;
    }).filter((x): x is { id: string; label: string; outcome: 'pass' | 'fail' | 'inconclusive' } => !!x);
    // Progressive disclosure: same-subject repeats collapse per agent, checks collapse per column.
    const groups = new Map<string, LineageStep[]>();
    for (const st of shown) {
      const key = st.kind === 'repeated' ? `rep:${st.agentId}:${colOf(st)}` : st.kind === 'checked' ? `chk:${colOf(st)}` : st.eventId;
      groups.set(key, [...(groups.get(key) ?? []), st]);
    }
    const items = [...groups.values()].map((g) => ({ head: g[g.length - 1], all: g, id: g[g.length - 1].eventId }));
    const itemOf = new Map<string, string>();
    items.forEach((it) => it.all.forEach((st) => itemOf.set(st.eventId, it.id)));
    for (const it of items) {
      const st = it.head;
      const c = colOf(st);
      const row = rows.get(c) ?? 0;
      rows.set(c, row + 1);
      const ev = ws.byId.get(st.eventId);
      const outcomes = it.all.map((x) => ws.byId.get(x.eventId)).map((x) => (x?.type === 'tool_result' ? x.payload.outcome : null));
      n.push({
        id: it.id, type: 'card', draggable: false, position: { x: colIndex.get(c)! * COL_W, y: row * 232 },
        data: { step: st, name: name(st.agentId), color: agents.get(st.agentId)?.color,
          selected: it.all.some((x) => x.eventId === (sel ?? lineage.claim.eventId)),
          outcome: ev?.type === 'tool_result' ? ev.payload.outcome : undefined, cites: st.kind === 'introduced' ? cites : undefined,
          count: it.all.length, passed: outcomes.filter((o) => o === 'pass').length, failed: outcomes.filter((o) => o === 'fail').length },
      });
    }
    const e: Edge[] = [];
    const seen = new Set<string>();
    for (const it of items) {
      const s = it.head;
      for (const f of [...new Set(it.all.flatMap((x) => x.from).map((x) => itemOf.get(x)).filter((x): x is string => !!x))]) {
        if (seen.has(`${f}->${it.id}`)) continue;
        seen.add(`${f}->${it.id}`);
        const missed = s.kind === 'used_after';
        e.push({
          id: `${f}->${it.id}`, source: f, target: it.id, type: 'smoothstep',
          style: { stroke: missed ? 'var(--contradicted)' : s.bySubject ? 'var(--unknown)' : 'var(--green-800)', strokeWidth: 1.6, strokeDasharray: s.kind === 'acknowledged' || s.kind === 'changed' || s.bySubject ? '5 4' : undefined },
          markerEnd: { type: MarkerType.ArrowClosed, color: missed ? 'var(--contradicted)' : 'var(--green-800)', width: 14, height: 14 },
        });
      }
    }
    // Make the missed correction explicit: correction → later use of the withdrawn claim.
    if (lineage.correction) {
      for (const s of lineage.steps.filter((x) => x.kind === 'used_after')) {
        e.push({ id: `${lineage.correction.id}~>${s.eventId}`, source: lineage.correction.id, target: s.eventId, type: 'smoothstep',
          label: 'no acknowledgement observed', labelStyle: { fill: 'var(--contradicted)', fontSize: 11 }, labelBgStyle: { fill: 'var(--surface)' },
          style: { stroke: 'var(--contradicted)', strokeWidth: 1.4, strokeDasharray: '3 5' } });
      }
    }
    return { nodes: n, edges: e };
  }, [lineage, ws, name, agents, sel]);

  const impact = useMemo(() => {
    const h = new Map<string, Highlight>();
    if (!claimId) return h;
    const ci = claimImpact(ws, claimId);
    ci.reach.forEach((t) => h.set(t, 'reach'));
    ci.referencing.forEach((r) => h.set(r.taskId, 'cites'));
    return h;
  }, [ws, claimId]);

  if (!lineage) {
    return (
      <div className="page-wide">
        <div className="hero"><div><h1>Follow a claim.</h1><p className="sub">No claims have been made yet at #{ws.cursor}. Scrub the timeline forward.</p></div></div>
        <section className="card"><Timeline /></section>
      </div>
    );
  }

  const { claim, audience, correction } = lineage;
  const retellings = lineage.steps.filter((s) => s.kind !== 'evidence' && s.kind !== 'checked').length + lineage.hiddenRepeats;
  const selected = lineage.steps.find((s) => s.eventId === (sel ?? claim.eventId)) ?? lineage.steps[0];
  const selIdx = lineage.steps.indexOf(selected);
  const followUp = lineage.steps[selIdx + 1];
  const acked = audience.filter((a) => a.ackEventId).length;
  const changed = audience.filter((a) => a.changedEventId).length;
  const standingChip = claim.standing === 'supported' ? 'green' : claim.standing === 'unknown' ? 'amber' : 'red';
  const standingText = { supported: 'Supported', unknown: 'Unverified', contradicted: 'Contradicted', superseded: 'Withdrawn' }[claim.standing];
  const lastStep = lineage.steps[lineage.steps.length - 1];
  const words = ['Zero', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten'];

  const exportLineage = () => {
    const blob = new Blob([JSON.stringify({ source: source?.id, cursor: ws.cursor, claim, steps: lineage.steps, audience,
      records: lineage.steps.map((s) => ws.byId.get(s.eventId)) }, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `recall-lineage-${claim.id}-at-${ws.cursor}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <div className="page-wide">
      <div className="hero">
        <div style={{ minWidth: 0 }}>
          <h1>One claim. {words[retellings] ?? retellings} retelling{retellings === 1 ? '' : 's'}.</h1>
          <p className="sub">Trace the evidence behind “{plain(claim.text, 160)}”</p>
          <div className="hero-chips">
            <span className={`chip ${standingChip}`}><span className="dot" />{standingText}</span>
            <span className="chip"><IUsers size={18} />{lineage.agentsReached.length} agent{lineage.agentsReached.length === 1 ? '' : 's'} reached</span>
            <span className="chip"><IClock size={18} />First observed {hm(lineage.firstSeen)}</span>
            <span className="chip mono">{claim.id}</span>
          </div>
        </div>
        <div className="hero-actions">
          <button className="btn btn-secondary" onClick={exportLineage} title="Download this lineage and its source records as JSON"><IExport /> Export</button>
          <button className="btn btn-primary" disabled={!correction} onClick={() => { seek(lastStep.sequence); setSel(lastStep.eventId); }}
            title={correction ? 'Move to the latest step after the correction' : 'This claim has no correction yet'}>Compare after correction</button>
        </div>
      </div>

      {chips.length > 1 && (
        <div className="claim-picker" aria-label="Choose a claim">
          {chips.slice(0, 12).map(({ claim: c, count, ids }) => (
            <button key={c.id} className={`filter ${claimId && ids.has(claimId) ? 'on' : ''}`} onClick={() => { setSel(null); navigate('propagation', c.id); }} title={plain(c.text, 300)}>
              <span>{c.subject ? subjectLabel(c.subject.artifact) : plain(c.text, 36)}</span>
              <span className={`pill ev-${c.standing === 'supported' ? 'supported' : c.standing === 'unknown' ? 'unknown' : 'contradicted'}`} style={{ height: 20 }}>
                {c.standing === 'superseded' ? 'Withdrawn' : c.standing}
              </span>
              {count > 1 && <span className="n">×{count}</span>}
            </button>
          ))}
          {chips.length > 12 && <span className="muted small" style={{ alignSelf: 'center' }}>+{chips.length - 12} more subjects in ⌘K</span>}
        </div>
      )}

      <div className="split">
        <section className="card">
          <div className="card-head">
            <h2>Claim lineage</h2>
            <span className="spacer" />
            <div className="seg">
              <button className={mode === 'transmission' ? 'on' : ''} onClick={() => setMode('transmission')}>Transmission</button>
              <button className={mode === 'impact' ? 'on' : ''} onClick={() => setMode('impact')}>Task impact</button>
            </div>
          </div>
          <div className="flow-canvas" style={{ height: 520 }}>
            <ReactFlowProvider key={`${claimId}:${mode}:${source?.id}`}>
              {mode === 'transmission' ? (
                <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView fitViewOptions={{ padding: 0.06, maxZoom: 1 }}
                  minZoom={0.3} nodesConnectable={false} proOptions={{ hideAttribution: true }}
                  onNodeClick={(_, n) => { if (n.type === 'card') { setSel(n.id); setTab('source'); } }} />
              ) : (
                <GraphView ws={ws} positions={positions} agents={agents} mode="evidence" selectedTask={null} highlights={impact} focusTask={null}
                  onSelectTask={(id) => id && navigate('tasks', id)} />
              )}
            </ReactFlowProvider>
          </div>
          <Timeline compact />
        </section>

        <aside className="card">
          <div className="card-head"><h2>Source evidence</h2></div>
          <div className="card-body stack">
            <div className="row" style={{ flexWrap: 'wrap' }}>
              <span className="tag">Event</span><span className="mono">{selected.eventId}</span>
              <span className="muted small">{hms(selected.timestamp)} · {selected.timestamp.slice(0, 10)} · {name(selected.agentId)}</span>
            </div>
            <div className="tabs" style={{ gap: 22 }}>
              <button className={tab === 'source' ? 'on' : ''} onClick={() => setTab('source')}>Source</button>
              <button className={tab === 'context' ? 'on' : ''} onClick={() => setTab('context')}>Context</button>
            </div>
            {tab === 'source' ? (
              <>
                <div className="big-quote">
                  <span className="qmark">“</span>
                  <div><p>{plain(selected.text, 600)}</p><div className="by">— {name(selected.agentId)} · {hms(selected.timestamp)}</div></div>
                </div>
                {followUp && (
                  <div>
                    <div className="row" style={{ justifyContent: 'space-between' }}><span className="section-title" style={{ margin: 0 }}>Observed follow-up</span><span className="mono muted small">{hms(followUp.timestamp)}</span></div>
                    <button className="big-quote" style={{ marginTop: 8, width: '100%', textAlign: 'left' }} onClick={() => setSel(followUp.eventId)}>
                      <div><p style={{ fontSize: 15 }}>“{plain(followUp.text, 220)}”</p><div className="by">— {name(followUp.agentId)} · {KIND_LABEL[followUp.kind]}</div></div>
                    </button>
                  </div>
                )}
                <button className="link" onClick={() => openRecord(selected.eventId)}>Open original record <IExternal /></button>
              </>
            ) : (
              <>
                <p className="note">{claim.reason}</p>
                {lineage.steps.some((x) => x.bySubject) && (
                  <div>
                    <div className="section-title">Same-subject records <span className="tag">{lineage.steps.filter((x) => x.bySubject).length + lineage.hiddenRepeats}</span></div>
                    <p className="muted xs" style={{ marginBottom: 6 }}>Linked by identical subject ({claim.subject && subjectLabel(claim.subject.artifact)}), not by an explicit reference.</p>
                    {lineage.steps.filter((x) => x.bySubject).map((x) => (
                      <button key={x.eventId} className="outcome" style={{ width: '100%' }} onClick={() => openRecord(x.eventId)}>
                        <span className="mono small">{hm(x.timestamp)}</span><span className="small">{name(x.agentId)}</span>
                        <span className="muted small" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{KIND_LABEL[x.kind]} — {plain(x.text, 80)}</span>
                      </button>
                    ))}
                    {lineage.hiddenRepeats > 0 && <p className="muted xs">+{lineage.hiddenRepeats} more repeats (see Evidence, filtered by claim).</p>}
                  </div>
                )}
                {claim.evidence.length === 0 && <p className="muted small">No verification of this claim's subject is visible at #{ws.cursor}.</p>}
                {claim.evidence.map((id) => {
                  const ev = ws.byId.get(id);
                  return ev && ev.type === 'tool_result' && (
                    <button key={id} className="outcome" onClick={() => openRecord(id)}>
                      <span className={`ev-badge ${ev.payload.outcome}`}>{ev.payload.outcome === 'pass' ? '✓' : '✕'}</span>
                      <span className="mono small">{ev.payload.runId}</span><span className="muted small">#{ev.sequence} · {name(ev.agentId)}</span>
                    </button>
                  );
                })}
              </>
            )}

            <div className="after-box">
              <h4>After the correction {correction && <ICheck />}</h4>
              {!correction ? (
                <p className="note">No correction of {claim.id} is recorded at #{ws.cursor}.</p>
              ) : (
                <>
                  <div className="after-row"><IUsers size={18} />Acknowledged<b>{acked} of {audience.length} agent{audience.length === 1 ? '' : 's'}</b></div>
                  <div className="after-row"><IExport size={18} style={{ transform: 'rotate(45deg)' }} />Behavior changed<b>{changed} of {audience.length} agent{audience.length === 1 ? '' : 's'}</b></div>
                  <div>
                    {audience.map((a) => (
                      <div key={a.agentId} className="outcome">
                        <Avatar name={name(a.agentId)} color={agents.get(a.agentId)?.color} size="sm" />
                        {name(a.agentId)}
                        <span className="state">
                          {a.ackEventId ? <button className="ack-yes" onClick={() => openRecord(a.ackEventId!)}>✓ Acknowledged</button> : <span className="ack-no" title="No acknowledgement is recorded. This does not mean the agent ignored it.">Not observed</span>}
                          {a.staleEventIds.length > 0 && <button className="tag stale" onClick={() => openRecord(a.staleEventIds[0])}>Reused ×{a.staleEventIds.length}</button>}
                        </span>
                      </div>
                    ))}
                  </div>
                  <p className="muted xs">“Not observed” means no acknowledgement is recorded up to #{ws.cursor}. It is not evidence the agent ignored the correction.</p>
                </>
              )}
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
