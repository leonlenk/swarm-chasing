import { useMemo, useState } from 'react';
import { ReactFlowProvider } from '@xyflow/react';
import { useRecall } from '../context';
import { reconstruct } from '../../engine/reconstruct';
import { runMonitors, type Finding } from '../../engine/monitors';
import { computeLayout } from '../../engine/layout';
import { FlowGraph } from '../FlowGraph';
import { GraphView } from '../GraphView';
import { TaskLanes } from '../TaskLanes';
import { Timeline } from '../Timeline';
import { Spark, StatePill } from '../Pills';
import { findingAgents, findingLabel, headline, plain, STATE_ORDER, timeAgo } from '../labels';
import { eventTone, TYPE_LABEL } from '../format';
import { bucketOf, type Bucket } from '../../engine/triage';
import { IAlert, IArrowR, IChat, IChevron, IClock, IDoc, IFork, IPie, IPlay, IUsers } from '../icons';

const hms = (iso: string) => new Date(iso).toISOString().slice(11, 19);

function useSeries() {
  const { source, input, cursor, minSeq } = useRecall();
  return useMemo(() => {
    if (!source) return null;
    const n = Math.min(24, Math.max(2, cursor - minSeq + 1));
    const pts = Array.from({ length: n }, (_, i) => Math.round(minSeq + ((cursor - minSeq) * i) / (n - 1)));
    const rows = pts.map((seq) => {
      const w = reconstruct(input, seq);
      const f = runMonitors(w);
      const progressed = [...w.tasks.values()].filter((t) => t.reportedStatus !== 'todo');
      return {
        agents: new Set(w.visible.map((e) => e.agentId)).size,
        claims: w.claims.size,
        open: f.filter((x) => bucketOf(x, w) === 'open').length,
        needs: f.filter((x) => bucketOf(x, w) === 'needs').length,
        coverage: progressed.length ? Math.round((progressed.filter((t) => t.evidenceStatus === 'supported').length / progressed.length) * 100) : 0,
      };
    });
    return { rows, last: rows[rows.length - 1] };
  }, [source, input, cursor, minSeq]);
}

const RANK: Record<Bucket, number> = { open: 0, needs: 1, patterns: 2, resolved: 3, count: 9 };

function NeedsAttention() {
  const { findings, ws, name, navigate, reviewed, source } = useRecall();
  // Contradicted first, then unchecked, then patterns; count-grade claims are counts, never attention items.
  const sorted = useMemo(() => findings.filter((f) => bucketOf(f, ws) !== 'count').sort((a, b) => RANK[bucketOf(a, ws)] - RANK[bucketOf(b, ws)] || STATE_ORDER[a.state] - STATE_ORDER[b.state] || b.detectedAt - a.detectedAt), [findings, ws]);
  const [openId, setOpenId] = useState<string | null>(null);
  const open = openId ?? sorted[0]?.id ?? null;
  const now = ws.visible[ws.visible.length - 1]?.timestamp ?? '';
  return (
    <section className="card attention" aria-label="Needs attention">
      <div className="card-head"><h2>Needs attention</h2><span className="spacer" /><span className="muted small">{sorted.length} finding{sorted.length === 1 ? '' : 's'}</span></div>
      <div className="attention-list">
        {sorted.length === 0 && (
          <div className="empty-state">
            <b>Nothing flagged at #{ws.cursor}</b>
            {source?.meta?.origin === 'huggingface'
              ? 'Monitors only fire on explicit, checkable links (a claim naming a URL and an observed check of that URL).'
              : 'Monitors only see events up to the cursor. Scrub forward or jump to the first discrepancy.'}
          </div>
        )}
        {sorted.map((f: Finding) => {
          const isOpen = open === f.id;
          const claim = ws.claims.get(f.claimId);
          const agentsN = findingAgents(f, ws).length;
          const detected = ws.byId.get(f.detectedEventId);
          return (
            <div key={f.id} className={`att-item tone-${f.state} ${isOpen ? 'open' : ''}`}>
              <button className="att-head" onClick={() => setOpenId(isOpen ? '' : f.id)} aria-expanded={isOpen}>
                <span className={`att-dot ${f.state}`} />
                <span style={{ minWidth: 0 }}>
                  <span className="att-title">{findingLabel(f)}</span>
                  <span className="att-meta" style={{ display: 'block' }}>
                    {agentsN} agent{agentsN === 1 ? '' : 's'} · {detected ? timeAgo(detected.timestamp, now) : ''} · #{f.detectedAt}
                    {reviewed.has(f.id) && ' · reviewed'}
                  </span>
                </span>
                <IChevron className="att-chev" />
              </button>
              {isOpen && (
                <div className="att-body">
                  <span className="att-label">Claim {f.claimId} · {name(f.agentId)}</span>
                  {claim && <p className="quote">“{plain(claim.text, 220)}”</p>}
                  <p className="att-expl">{f.summary}</p>
                  <div className="row" style={{ justifyContent: 'space-between' }}>
                    <button className={`link ${f.state === 'active' ? '' : 'amber'}`} onClick={() => navigate('incidents', f.id)}>Inspect evidence <IArrowR /></button>
                    <StatePill state={f.state} reviewed={reviewed.has(f.id)} />
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function RecentActivity() {
  const { ws, name, openRecord } = useRecall();
  const recent = ws.visible.filter((e) => e.type !== 'status_updated').slice(-3).reverse();
  const icon = (t: string) => (t === 'tool_result' ? <IDoc /> : t === 'action' ? <IArrowR /> : t === 'claim' ? <IChat /> : t === 'correction' ? <IAlert size={18} /> : <IFork />);
  return (
    <section className="card">
      <div className="card-head"><h2>Recent activity</h2><span className="spacer" /><span className="muted small">as of #{ws.cursor}</span></div>
      <div className="activity">
        {recent.length === 0 && <div className="empty-state">No activity yet.</div>}
        {recent.map((e) => (
          <button key={e.id} className="act" onClick={() => openRecord(e.id)}>
            <span className={`act-icon tone-${eventTone(e)}`}>{icon(e.type)}</span>
            <span className="act-time">{hms(e.timestamp)}</span>
            <span className="act-text">
              <b>{name(e.agentId)}</b> <span className="muted">· {TYPE_LABEL[e.type]}</span>
              <span className="act-sub">{plain(e.text, 200)}</span>
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}

export function Overview() {
  const r = useRecall();
  const { source, sourceEntry, ws, findings, cursor, navigate, replay } = r;
  const events = useMemo(() => source?.events ?? [], [source]);
  const series = useSeries();
  const [mode, setMode] = useState<'claims' | 'tasks'>('claims');
  const [mentions, setMentions] = useState(false);
  const [scope, setScope] = useState<'incidents' | 'all' | null>(null);
  const effectiveScope = scope ?? (findings.length && (source?.agents.length ?? 0) > 6 ? 'incidents' : 'all');
  const hasMentions = useMemo(() => events.some((e) => e.mentions?.length), [events]);
  const positions = useMemo(() => computeLayout(events), [events]);
  // Headline: contradicted first, then unchecked, then patterns; count-grade claims never headline.
  const h = headline([...findings].filter((f) => bucketOf(f, ws) !== 'count').sort((a, b) => RANK[bucketOf(a, ws)] - RANK[bucketOf(b, ws)] || b.detectedAt - a.detectedAt).slice(0, 1).concat(findings.filter((f) => bucketOf(f, ws) !== 'count').slice(1)), cursor);
  const now = ws.visible[ws.visible.length - 1];
  const s = series?.rows ?? [];

  return (
    <div className="page-wide">
      <div className="hero">
        <div>
          <h1>Understand the swarm.</h1>
          <p className="sub">Follow a claim. Find its source. See what changed.
            {source?.meta?.goal && <> Village goal: <b style={{ fontWeight: 500 }}>{source.meta.goal}</b>.</>}
          </p>
        </div>
        <div className="hero-actions">
          <span className="chip"><IClock size={18} />{now ? `As of ${hms(now.timestamp)} UTC` : 'Start'}{sourceEntry?.window && ` · ${sourceEntry.window.from.slice(0, 10)}`}</span>
          <button className="btn btn-primary" onClick={replay}><IPlay size={16} /> Replay session</button>
        </div>
      </div>

      <div className="kpis five">
        <div className="kpi"><span className="kpi-icon"><IUsers /></span><div><div className="kpi-label">Active agents</div><div className="kpi-value">{series?.last.agents ?? 0}<small>/ {source?.agents.length ?? 0}</small></div></div><Spark width={104} values={s.map((x) => x.agents)} /></div>
        <div className="kpi"><span className="kpi-icon"><IChat /></span><div><div className="kpi-label">Tracked claims</div><div className="kpi-value">{series?.last.claims ?? 0}</div></div><Spark width={104} values={s.map((x) => x.claims)} /></div>
        <div className="kpi" title="Contradicted-class findings: the records contradict what was claimed (A, D, E, G, J, W, Y, AO, AP, AQ, AR, AY)."><span className="kpi-icon"><IAlert /></span><div><div className="kpi-label">Open incidents</div><div className="kpi-value">{series?.last.open ?? 0}</div></div><Spark width={104} values={s.map((x) => x.open)} tone={series?.last.open ? 'amber' : 'green'} /></div>
        <div className="kpi" title="Needs evidence: claims the records cannot confirm yet (C on external subjects, Z, AM, AC, BM, BJ, BK)."><span className="kpi-icon"><IChat /></span><div><div className="kpi-label">Unchecked claims</div><div className="kpi-value">{series?.last.needs ?? 0}</div></div><Spark width={104} values={s.map((x) => x.needs)} /></div>
        <div className="kpi" title="Share of tasks with reported progress whose status is backed by a record (tool output or observed system event)."><span className="kpi-icon"><IPie /></span><div><div className="kpi-label">Evidence coverage</div><div className="kpi-value">{series?.last.coverage ?? 0}<small>%</small></div></div><Spark width={104} values={s.map((x) => x.coverage)} /></div>
      </div>

      <div className="overview-grid">
        <section className="card flow-card">
          <div className="card-head">
            <h2>Information flow</h2>
            <div className="seg" role="tablist">
              <button className={mode === 'claims' ? 'on' : ''} onClick={() => setMode('claims')} role="tab" aria-selected={mode === 'claims'}>Claims</button>
              <button className={mode === 'tasks' ? 'on' : ''} onClick={() => setMode('tasks')} role="tab" aria-selected={mode === 'tasks'}>Tasks</button>
            </div>
            {mode === 'claims' ? (
              <div className="legend">{findings.length > 0 && (
                <div className="seg" style={{ padding: 2 }}>
                  <button className={effectiveScope === 'incidents' ? 'on' : ''} onClick={() => setScope('incidents')} title="Only subjects behind findings and agents linked to them">Incident links</button>
                  <button className={effectiveScope === 'all' ? 'on' : ''} onClick={() => setScope('all')}>All links</button>
                </div>
              )}{hasMentions && <button className={`filter ${mentions ? 'on' : ''}`} onClick={() => setMentions((m) => !m)} title="Show inferred @-mention edges between agents">@-mentions</button>}<span><i className="lg-agent" />Agent</span><span><i className="lg-arrow" />Transmission</span><span><i className="lg-arrow amber" />Withdrawn / contradicted claim</span><span><i className="lg-ev" />Evidence</span></div>
            ) : (
              <div className="legend"><span>Dependency arrows · evidence status on each task</span></div>
            )}
          </div>
          <div className={`headline tone-${h.tone}`}>
            <span className="hdot" />
            <span>{h.text}</span>
            {h.finding && <button className="link" onClick={() => navigate('incidents', h.finding!.id)}>Investigate <IArrowR /></button>}
          </div>
          <div className="flow-canvas">
            <ReactFlowProvider key={`${source?.id}:${mode}`}>
              {mode === 'claims'
                ? <FlowGraph showMentions={mentions} incidentsOnly={effectiveScope === 'incidents'} />
                : !events.some((e) => e.type === 'dependency_created' || (e.type === 'task_created' && e.payload.tasks.some((t) => t.dependsOn?.length)))
                  ? <div style={{ height: '100%', padding: '8px 10px' }}><TaskLanes mode="difference" onSelect={(id) => navigate('tasks', id)} /></div>
                  : <GraphView ws={ws} positions={positions} agents={r.agents} mode="evidence" selectedTask={null} highlights={new Map()} focusTask={now?.taskId ?? null}
                    onSelectTask={(id) => id && navigate('tasks', id)} />}
            </ReactFlowProvider>
          </div>
          <Timeline />
        </section>
        <NeedsAttention />
      </div>
      <RecentActivity />
    </div>
  );
}
