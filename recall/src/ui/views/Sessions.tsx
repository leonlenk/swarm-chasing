// Live sessions (anand/live-plugin): Claude Code sessions recorded by the swarm-live hooks, read through
// `swarm-mcp render recall [--watch]`. One session = the main agent plus the subagents it spawned. The tree and the
// run timeline are drawn straight from the recorded rows; findings come from running every monitor over the
// session's adapted events (adapters/claudeCode.ts), the same as any other source.
import { useMemo, useState } from 'react';
import { useRecall } from '../context';
import type { LiveSession, LiveSessionEntry } from '../../model/scope';
import { adaptClaudeCodeSession } from '../../adapters/claudeCode';
import { reconstruct } from '../../engine/reconstruct';
import { runMonitors, type Finding } from '../../engine/monitors';
import { ago, copyText, fmtN, toolFamily, useNow, useScopeFile, useWidth } from '../scopeData';
import { findingLabel, STATE_ORDER } from '../labels';
import { Avatar, StatePill } from '../Pills';
import { IArrowR, IPlay } from '../icons';

type Doc = LiveSession;
type DocAgent = Doc['agents'][number];
type DocAction = Doc['actions'][number];

const FAMILIES = ['shell', 'read', 'write', 'delegate', 'mcp', 'web', 'other'] as const;

function StatusDot({ status }: { status: 'running' | 'stopped' | string | null }) {
  return <span className={`live-dot ${status === 'running' ? 'on' : ''}`} aria-hidden />;
}

function SetupCard({ compact }: { compact?: boolean }) {
  const [copied, setCopied] = useState<string | null>(null);
  const cmds = [
    ['Record your own sessions', 'claude --plugin-dir ~/swarm-chasing   # from any project'],
    ['Stream them into RECALL', 'uv run --directory swarm_mcp swarm-mcp render recall --watch'],
    ['Or replay a synthetic demo live', 'uv run --directory swarm_mcp python examples/live_demo.py data/demo.db --drip 1.5'],
  ];
  return (
    <section className={`card ${compact ? '' : 'setup-card'}`}>
      <div className="card-head"><h2>{compact ? 'Record more sessions' : 'No recorded sessions yet'}</h2></div>
      <div className="card-body stack">
        {!compact && <p className="muted">The swarm-live hooks record every Claude Code session and subagent on this machine into a local SQLite file. <span className="mono">render recall</span> turns the recordings into RECALL sources; with <span className="mono">--watch</span> this view follows them as they run.</p>}
        {cmds.map(([label, cmd]) => (
          <div key={cmd} className="cmd-row">
            <span className="muted small">{label}</span>
            <button className="cmd" onClick={async () => { if (await copyText(cmd)) { setCopied(cmd); setTimeout(() => setCopied(null), 1500); } }} title="Copy">
              <code>{cmd}</code><span className="tag">{copied === cmd ? 'copied' : 'copy'}</span>
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}

function SessionList({ sessions, selected, onSelect, now }: { sessions: LiveSessionEntry[]; selected: string | null; onSelect: (id: string) => void; now: number }) {
  return (
    <div className="session-list" role="listbox" aria-label="Recorded sessions">
      {sessions.map((s) => (
        <button key={s.id} role="option" aria-selected={s.id === selected} className={`session-item ${s.id === selected ? 'on' : ''}`} onClick={() => onSelect(s.id)}>
          <span className="session-top">
            <StatusDot status={s.status} />
            <b>{s.label}</b>
          </span>
          <span className="session-meta">
            {s.folder && <span className="mono">{s.folder}</span>}
            <span>{s.status === 'running' ? `recording · ${ago(s.updated, now)}` : `ended ${ago(s.updated, now)}`}</span>
            {s.synthetic && <span className="tag demo">demo</span>}
          </span>
          <span className="session-counts">
            <span>{s.subagents} subagent{s.subagents === 1 ? '' : 's'}</span>
            <span>{fmtN(s.actions)} tool calls</span>
            {s.errors > 0 && <span className="bad">{s.errors} failed</span>}
          </span>
        </button>
      ))}
    </div>
  );
}

function ToolStrip({ actions, onPick }: { actions: DocAction[]; onPick: (a: DocAction) => void }) {
  if (!actions.length) return <span className="muted xs">no tool calls</span>;
  return (
    <div className="tool-strip" aria-label={`${actions.length} tool calls`}>
      {actions.map((a) => {
        const f = toolFamily(a.tool);
        return <button key={a.id} className={`tool-cell ${a.status === 'error' ? 'err' : ''} ${a.status === 'pending' ? 'pending' : ''}`} style={{ background: f.color }}
          title={`${a.tool}${a.status === 'error' ? ' (failed)' : a.status === 'pending' ? ' (running)' : ''}: ${a.text}`} onClick={() => onPick(a)} />;
      })}
    </div>
  );
}

function AgentNode({ a, doc, color, onPick, depth }: { a: DocAgent; doc: Doc; color: string; onPick: (x: DocAction) => void; depth: number }) {
  const acts = doc.actions.filter((x) => x.agent === a.id);
  const errors = acts.filter((x) => x.status === 'error').length;
  const final = [...doc.messages].reverse().find((m) => m.author === a.id && (m.type === 'final' || m.type === 'transcript'));
  const kids = doc.agents.filter((k) => k.parent === a.id);
  const tools = new Map<string, number>();
  acts.forEach((x) => tools.set(x.tool, (tools.get(x.tool) ?? 0) + 1));
  return (
    <div className="tree-node" style={{ ['--depth' as string]: depth }}>
      <div className={`agent-tile ${a.kind}`}>
        <div className="agent-tile-head">
          <Avatar name={a.name} color={color} size="sm" />
          <div style={{ minWidth: 0, flex: 1 }}>
            <b className="agent-tile-name">{a.name}</b>
            <span className="muted xs">{a.kind === 'main' ? 'main agent' : `subagent · ${a.agentType ?? 'general'}`}{a.first && ` · ${a.first.slice(11, 19)} UTC`}</span>
          </div>
          <span className={`pill ${a.status === 'running' ? 'ev-unknown' : 'status'}`}><StatusDot status={a.status} />{a.status === 'running' ? 'Running' : 'Stopped'}</span>
        </div>
        {a.task && <p className="agent-task">{a.task}</p>}
        <ToolStrip actions={acts} onPick={onPick} />
        <div className="agent-tile-foot">
          <span>{acts.length} call{acts.length === 1 ? '' : 's'}</span>
          {[...tools.entries()].sort((x, y) => y[1] - x[1]).slice(0, 4).map(([t, n]) => <span key={t} className="mono">{t} {n}</span>)}
          {errors > 0 && <span className="bad">{errors} failed</span>}
        </div>
        {final && <blockquote className="agent-final">{final.text}</blockquote>}
      </div>
      {kids.length > 0 && <div className="tree-kids">{kids.map((k) => <AgentNode key={k.id} a={k} doc={doc} color={colorFor(doc, k.id)} onPick={onPick} depth={depth + 1} />)}</div>}
    </div>
  );
}

const AGENT_COLORS = ['#163e2e', '#7a5cb8', '#3b5f94', '#c9692f', '#227777', '#8e3b5c', '#5a6f22', '#9a5b22'];
function colorFor(doc: Doc, id: string) { return AGENT_COLORS[Math.max(0, doc.agents.findIndex((a) => a.id === id)) % AGENT_COLORS.length]; }

/** One lane per agent over the session's span: tool calls as ticks (failures marked), text as dots, spawns as links. */
function RunTimeline({ doc, onPick }: { doc: Doc; onPick: (x: DocAction) => void }) {
  const [ref, W] = useWidth<HTMLDivElement>();
  const ts = [...doc.actions.flatMap((x) => [x.t, x.end]), ...doc.messages.map((m) => m.t)].filter((t): t is string => !!t).map((t) => Date.parse(t));
  if (ts.length < 2) return <p className="muted small">Not enough recorded steps for a timeline yet.</p>;
  const lo = Math.min(...ts), hi = Math.max(...ts), span = Math.max(hi - lo, 1000);
  const LANE = 34, GUT = 0;
  const x = (iso: string) => GUT + ((Date.parse(iso) - lo) / span) * (W - GUT - 8) + 4;
  const lane = new Map(doc.agents.map((a, i) => [a.id, i]));
  const H = doc.agents.length * LANE + 26;
  const ticks = Array.from({ length: 6 }, (_, i) => lo + (span * i) / 5);
  return (
    <div className="run-tl">
      <div className="run-tl-names">{doc.agents.map((a) => <div key={a.id} style={{ height: LANE }}><Avatar name={a.name} color={colorFor(doc, a.id)} size="sm" /><span>{a.name}</span></div>)}</div>
      <div ref={ref} style={{ minWidth: 0 }}>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="run-tl-svg" role="img" aria-label="Run timeline">
        {doc.agents.map((a, i) => <rect key={a.id} x={0} y={i * LANE} width={W} height={LANE} className={i % 2 ? 'lane-alt' : 'lane-base'} />)}
        {doc.agents.map((a) => {
          const p = doc.periods.find((q) => q.agent === a.id);
          if (!p?.start) return null;
          const i = lane.get(a.id)!;
          const end = p.end ?? new Date(hi).toISOString();
          return <rect key={`run-${a.id}`} x={x(p.start)} y={i * LANE + LANE / 2 - 3} width={Math.max(2, x(end) - x(p.start))} height={6} rx={3} fill={colorFor(doc, a.id)} opacity={0.18} />;
        })}
        {doc.actions.filter((s) => s.spawned && s.t).map((s) => {
          const from = lane.get(s.agent), to = lane.get(s.spawned!);
          if (from === undefined || to === undefined) return null;
          return <path key={`sp-${s.id}`} d={`M${x(s.t!)},${from * LANE + LANE / 2} L${x(s.t!)},${to * LANE + LANE / 2}`} className="spawn-link" />;
        })}
        {doc.messages.filter((m) => m.t && lane.has(m.author)).map((m) => (
          <circle key={m.id} cx={x(m.t!)} cy={lane.get(m.author)! * LANE + LANE / 2} r={3.4} className={`msg-dot ${m.type}`}><title>{`${m.type}: ${m.text.slice(0, 160)}`}</title></circle>
        ))}
        {doc.actions.filter((a) => a.t && lane.has(a.agent)).map((a) => {
          const f = toolFamily(a.tool);
          const cx = x(a.t!), cy = lane.get(a.agent)! * LANE + LANE / 2;
          return (
            <g key={a.id} className="tool-tick" onClick={() => onPick(a)}>
              <rect x={cx - 2} y={cy - 9} width={4} height={18} rx={1.5} fill={f.color} />
              {a.status === 'error' && <circle cx={cx} cy={cy - 12} r={3.2} fill="var(--contradicted)" />}
              <title>{`${a.tool}${a.status === 'error' ? ' (failed)' : ''}: ${a.text.slice(0, 160)}`}</title>
            </g>
          );
        })}
        {ticks.map((t, k) => <text key={t} x={GUT + ((t - lo) / span) * (W - GUT - 8) + 4} y={H - 8} className="axis-label" textAnchor={k === 0 ? 'start' : k === ticks.length - 1 ? 'end' : 'middle'}>{new Date(t).toISOString().slice(11, 19)}</text>)}
      </svg>
      </div>
    </div>
  );
}

function FindingsCard({ doc, findings, isLoaded, openInReplay }: { doc: Doc; findings: Finding[]; isLoaded: boolean; openInReplay: (view?: 'incidents', id?: string) => void }) {
  const active = findings.filter((f) => f.state === 'active').length;
  return (
    <section className="card">
      <div className="card-head"><h2>What the monitors see</h2><span className="spacer" />
        <span className={`chip ${active ? 'red' : findings.length ? 'amber' : 'green'}`}><span className="dot" />{active} open · {findings.length} total</span>
      </div>
      <div className="card-body stack">
        {findings.length === 0 ? <p className="muted small">No monitor finds anything in this session. Monitors read claims in agent text and verdicts from Bash output (HTTP checks, test summaries, git pushes…).</p>
          : [...findings].sort((x, y) => STATE_ORDER[x.state] - STATE_ORDER[y.state]).slice(0, 8).map((f) => (
            <button key={f.id} className="finding-row" onClick={() => openInReplay('incidents', f.id)}>
              <StatePill state={f.state} />
              <span><b>{findingLabel(f)}</b><span className="muted small" style={{ display: 'block' }}>{f.summary}</span></span>
              <IArrowR />
            </button>
          ))}
        <p className="muted xs">{isLoaded ? 'Same findings as the Incidents view.' : `Computed from the ${doc.actions.length} recorded tool calls and ${doc.messages.length} messages; open the replay for evidence and lineage.`}</p>
      </div>
    </section>
  );
}

function ActionDrawer({ a, doc, onClose }: { a: DocAction; doc: Doc; onClose: () => void }) {
  const who = doc.agents.find((x) => x.id === a.agent);
  const [copied, setCopied] = useState(false);
  return (
    <section className="card action-detail">
      <div className="card-head">
        <h2>{a.tool}</h2>
        <span className={`pill ${a.status === 'error' ? 'ev-contradicted' : a.status === 'ok' ? 'ev-supported' : 'ev-unknown'}`}>{a.status === 'error' ? 'Failed' : a.status === 'ok' ? 'Completed' : a.status ?? 'pending'}</span>
        <span className="spacer" />
        <button className="link small" onClick={onClose}>Close</button>
      </div>
      <div className="card-body stack">
        <dl className="kv">
          <dt>Agent</dt><dd>{who?.name ?? a.agent}</dd>
          <dt>When</dt><dd className="mono">{a.t?.replace('T', ' ').slice(0, 19)} UTC{a.end && a.t ? ` · ${Math.max(0, Math.round((Date.parse(a.end) - Date.parse(a.t)) / 100) / 10)} s` : ''}</dd>
          <dt>Store id</dt><dd><button className="link mono small" onClick={async () => { setCopied(await copyText(a.id)); setTimeout(() => setCopied(false), 1400); }}>{a.id}</button>{copied && <span className="muted xs"> copied</span>}</dd>
        </dl>
        <pre className="rec-output">{a.command ? `$ ${a.command}` : a.text}</pre>
        {a.output && <pre className="rec-output">{a.output}</pre>}
        <p className="muted xs">Recorded tool input and output (masked, capped at 4,000 characters). Untrusted agent output: shown as text.</p>
      </div>
    </section>
  );
}

export function Sessions() {
  const { scope, sources, source, selectSource, navigate, param, following, setFollowing } = useRecall();
  const now = useNow(5000);
  const sessions = scope?.live.sessions ?? [];
  const selectedId = (param && sessions.some((s) => s.id === param) ? param : null) ?? (source?.meta?.live?.session && sessions.some((s) => s.id === source.meta!.live!.session) ? source.meta!.live!.session : null) ?? sessions[0]?.id ?? null;
  const entry = sessions.find((s) => s.id === selectedId) ?? null;
  const { data: doc, error } = useScopeFile<Doc>(entry?.file, entry?.updated);
  const [picked, setPicked] = useState<DocAction | null>(null);
  const isLoaded = !!entry && source?.id === `live:${entry.id}`;
  const findings = useMemo(() => {
    if (!doc) return [];
    try {
      const ds = adaptClaudeCodeSession(doc);
      const last = ds.events[ds.events.length - 1]?.sequence ?? 0;
      return runMonitors(reconstruct({ events: ds.events, withheld: new Set(), agents: ds.agents }, last));
    } catch { return []; }
  }, [doc]);
  const running = sessions.filter((s) => s.status === 'running').length;
  const openInReplay = (view: 'overview' | 'incidents' | 'tasks' = 'overview', id?: string) => {
    if (!entry) return;
    const sid = `live:${entry.id}`;
    if (source?.id !== sid && sources.some((s) => s.id === sid)) selectSource(sid);
    navigate(view, id);
  };
  const families = useMemo(() => {
    const seen = new Map((doc?.actions ?? []).map((a) => { const f = toolFamily(a.tool); return [f.key, f] as const; }));
    return FAMILIES.flatMap((k) => (seen.has(k) ? [seen.get(k)!] : []));
  }, [doc]);

  return (
    <div className="page-wide">
      <div className="hero">
        <div>
          <span className="hero-kicker"><StatusDot status={running ? 'running' : 'stopped'} /> {running ? `${running} session${running === 1 ? '' : 's'} recording now` : sessions.length ? `last activity ${ago(scope?.live.updatedAt ?? null, now)}` : 'swarm-live recordings'}</span>
          <h1>Your agents, as they work.</h1>
          <p className="sub">Claude Code sessions recorded by the swarm-live hooks: each is the main agent and every subagent it spawned, with each tool call they made. Open one to replay it step by step, with every monitor running.</p>
        </div>
        {entry && (
          <div className="hero-actions">
            {entry.status === 'running' && isLoaded && (
              <label className="switch-row small"><input type="checkbox" checked={following} onChange={(e) => setFollowing(e.target.checked)} /><span className="switch" />Follow live</label>
            )}
            <button className="btn btn-secondary" onClick={() => openInReplay('tasks')}>Delegation graph</button>
            <button className="btn btn-primary" onClick={() => openInReplay('overview')}><IPlay size={15} /> Open in replay</button>
          </div>
        )}
      </div>

      {sessions.length === 0 ? <SetupCard /> : (
        <div className="sessions-layout">
          <aside className="stack">
            <div className="section-title">Sessions <span className="tag">{sessions.length}</span></div>
            <SessionList sessions={sessions} selected={selectedId} onSelect={(id) => { setPicked(null); navigate('sessions', id); }} now={now} />
            <SetupCard compact />
          </aside>
          <div className="stack" style={{ gap: 22, minWidth: 0 }}>
            {error && <div className="error-banner">{error}</div>}
            {!doc ? <div className="empty-state"><div className="spinner" style={{ margin: '0 auto 12px' }} />Loading session…</div> : (
              <>
                {doc.synthetic && <div className="demo-banner"><span className="tag demo">Synthetic demo recording</span>Written by examples/live_demo.py through the real recorder. Nothing in it ran.</div>}
                <div className="kpis boxed five">
                  <div className="kpi"><div><div className="kpi-label">Agents</div><div className="kpi-value">{doc.agents.length}<small>{doc.agents.length - 1} sub</small></div></div></div>
                  <div className="kpi"><div><div className="kpi-label">Tool calls</div><div className="kpi-value">{fmtN(doc.actions.length)}</div></div></div>
                  <div className="kpi"><div><div className="kpi-label">Failed calls</div><div className="kpi-value" style={{ color: doc.actions.some((a) => a.status === 'error') ? 'var(--contradicted)' : undefined }}>{doc.actions.filter((a) => a.status === 'error').length}</div></div></div>
                  <div className="kpi"><div><div className="kpi-label">Messages</div><div className="kpi-value">{doc.messages.length}</div></div></div>
                  <div className="kpi"><div><div className="kpi-label">Duration</div><div className="kpi-value">{doc.start && doc.updated ? Math.max(1, Math.round((Date.parse(doc.updated) - Date.parse(doc.start)) / 60000)) : '—'}<small>min</small></div></div></div>
                </div>

                <div className="grid-2 sessions-grid">
                  <section className="card">
                    <div className="card-head"><h2>Delegation tree</h2><span className="spacer" />
                      <span className="tool-legend">{families.map((f) => <span key={f.key}><i style={{ background: f.color }} />{f.label}</span>)}<span><i className="err-dot" />failed</span></span>
                    </div>
                    <div className="card-body tree">
                      {doc.agents.filter((a) => !a.parent || !doc.agents.some((b) => b.id === a.parent)).map((a) => <AgentNode key={a.id} a={a} doc={doc} color={colorFor(doc, a.id)} onPick={setPicked} depth={0} />)}
                    </div>
                  </section>
                  <div className="stack" style={{ gap: 22 }}>
                    {picked ? <ActionDrawer a={picked} doc={doc} onClose={() => setPicked(null)} /> : null}
                    <FindingsCard doc={doc} findings={findings} isLoaded={isLoaded} openInReplay={(v, id) => openInReplay(v ?? 'overview', id)} />
                  </div>
                </div>

                <section className="card">
                  <div className="card-head"><h2>Run timeline</h2><span className="spacer" /><span className="muted small">bars = tool calls (click one) · dots = messages · vertical links = subagent spawned</span></div>
                  <div className="card-body"><RunTimeline doc={doc} onPick={setPicked} /></div>
                </section>

                <details className="card notes-card">
                  <summary>What the recorder can and cannot see</summary>
                  <ul>{doc.notes.map((n) => <li key={n}>{n}</li>)}</ul>
                </details>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
