import { useMemo } from 'react';
import { ReactFlowProvider } from '@xyflow/react';
import { useRecall } from '../context';
import { Avatar } from '../Pills';
import { FlowGraph } from '../FlowGraph';
import { Record } from '../Record';
import { findingAgents, findingLabel } from '../labels';
import { CountGrade } from '../CountGrade';
import { countGradeClaims } from '../../engine/triage';

export function Agents() {
  const { source, ws, param, navigate, agents, findings, openRecord, seek } = useRecall();
  const stats = useMemo(() => {
    const m = new Map<string, { records: number; claims: number; corrections: number; acks: number; tools: number; stale: number; tasks: number }>();
    for (const a of source?.agents ?? []) m.set(a.id, { records: 0, claims: 0, corrections: 0, acks: 0, tools: 0, stale: 0, tasks: 0 });
    for (const e of ws.visible) {
      const s = m.get(e.agentId);
      if (!s) continue;
      s.records++;
      if (e.type === 'claim') s.claims++;
      if (e.type === 'correction') s.corrections++;
      if (e.type === 'acknowledgement') s.acks++;
      if (e.type === 'tool_result') s.tools++;
      if (e.type === 'action' && e.payload.referencesClaims.some((c) => ws.claims.get(c)?.standing === 'superseded')) s.stale++;
    }
    ws.tasks.forEach((t) => { const s = m.get(t.owner); if (s) s.tasks++; });
    return m;
  }, [source, ws]);
  const countGrade = useMemo(() => { const m = new Map<string, number>(); for (const r of countGradeClaims(findings, ws)) r.byAgent.forEach((l, a) => m.set(a, (m.get(a) ?? 0) + l.length)); return m; }, [findings, ws]);
  const sel = param && agents.has(param) ? param : null;
  const ordered = [...(source?.agents ?? [])].sort((a, b) => (stats.get(b.id)?.records ?? 0) - (stats.get(a.id)?.records ?? 0));
  const selRecords = sel ? ws.visible.filter((e) => e.agentId === sel).slice(-40).reverse() : [];
  const selFindings = sel ? findings.filter((f) => findingAgents(f, ws).includes(sel)) : [];

  return (
    <div className="page-wide">
      <div className="hero">
        <div>
          <h1>Who said what, and on what basis.</h1>
          <p className="sub">Every agent in {source?.label}, with the records they produced up to #{ws.cursor}.</p>
        </div>
      </div>
      <div className={sel ? 'split' : ''}>
        <div className="agents-grid">
          {ordered.map((a) => {
            const s = stats.get(a.id)!;
            return (
              <button key={a.id} className={`card agent-card ${sel === a.id ? 'sel' : ''}`} onClick={() => navigate('agents', sel === a.id ? undefined : a.id)}>
                <div className="row" style={{ gap: 12 }}>
                  <Avatar name={a.name} color={a.color} />
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontWeight: 500, fontSize: 15.5 }}>{a.name}</div>
                    <div className="muted small" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{a.role}</div>
                  </div>
                  {s.records === 0 && <span className="tag" style={{ marginLeft: 'auto' }}>Not active yet</span>}
                </div>
                <div className="agent-stats">
                  <div><b>{s.records}</b><span>records</span></div>
                  <div><b>{s.tasks}</b><span>tasks</span></div>
                  <div><b>{s.claims}</b><span>claims</span></div>
                  <div><b>{s.tools}</b><span>checks</span></div>
                </div>
                {(s.stale > 0 || s.corrections > 0 || s.acks > 0 || (countGrade.get(a.id) ?? 0) > 0) && (
                  <div className="row" style={{ flexWrap: 'wrap' }}>
                    {s.corrections > 0 && <span className="tag correction">{s.corrections} correction{s.corrections > 1 ? 's' : ''}</span>}
                    {s.acks > 0 && <span className="tag real">{s.acks} acknowledged</span>}
                    {s.stale > 0 && <span className="tag stale">cited withdrawn claim ×{s.stale}</span>}
                    {(countGrade.get(a.id) ?? 0) > 0 && <span className="tag" title="Count-grade C: claims on repo / file / process subjects with no verification">{countGrade.get(a.id)} count-grade claim{countGrade.get(a.id)! > 1 ? 's' : ''}</span>}
                  </div>
                )}
              </button>
            );
          })}
        </div>
        {sel && (
          <aside className="card">
            <div className="card-head"><Avatar name={agents.get(sel)!.name} color={agents.get(sel)!.color} /><h2>{agents.get(sel)!.name}</h2><span className="spacer" /><button className="link-btn" onClick={() => navigate('agents')}>Close</button></div>
            <div className="flow-canvas" style={{ height: 280 }}>
              <ReactFlowProvider key={`agent:${sel}`}><FlowGraph focusAgent={sel} /></ReactFlowProvider>
            </div>
            <div className="card-body stack">
              {selFindings.length > 0 && (
                <div>
                  <div className="section-title">Incidents involving this agent</div>
                  {selFindings.map((f) => <button key={f.id} className="reach-row" onClick={() => navigate('incidents', f.id)}><span className="reach-title">{findingLabel(f)}</span><span className={`state-pill state-${f.state}`}>{f.state}</span></button>)}
                </div>
              )}
              <div><div className="section-title">Count-grade claims (not incidents)</div><CountGrade agent={sel} /></div>
              <div className="section-title">Latest records <span className="tag">{selRecords.length}</span></div>
              {selRecords.length === 0 ? <p className="muted small">No records yet at #{ws.cursor}.</p>
                : selRecords.map((e) => <Record key={e.id} event={e} agent={agents.get(e.agentId)} cursor={ws.cursor} refStatus={ws.refStatus} onInspect={openRecord} onJump={seek} />)}
            </div>
          </aside>
        )}
      </div>
    </div>
  );
}
