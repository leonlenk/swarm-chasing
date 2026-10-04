import { useMemo, useState } from 'react';
import { useRecall } from '../context';
import type { Finding } from '../../engine/monitors';
import { MONITOR_LABEL, registry } from '../../engine/monitors';
import { Avatar, EvidencePill, StatePill, StatusPill } from '../Pills';
import { Record, UnavailableRecord } from '../Record';
import { findingAgents, findingLabel, groupFindings, plain, STATE_ORDER, subjectLabel } from '../labels';
import { eventTone } from '../format';
import { IAlert, IChevronR, IExternal, ISearch, IShield, ITasks, IUsers } from '../icons';

const hm = (iso: string) => new Date(iso).toISOString().slice(11, 16);
type Tab = 'active' | 'insufficient' | 'resolved';

function roleOf(f: Finding, agentId: string, ws: ReturnType<typeof useRecall>['ws']): string {
  const roles = f.evidence.filter((e) => ws.byId.get(e.eventId)?.agentId === agentId).map((e) => e.role.toLowerCase());
  if (roles.some((r) => r.includes('correction'))) return 'Corrected';
  if (roles.some((r) => r.includes('acknowledged'))) return 'Acknowledged';
  if (roles.some((r) => r.includes('action citing'))) return 'Reused claim';
  if (roles.some((r) => r.includes('claim'))) return 'Introduced claim';
  if (roles.some((r) => r.includes('verification') || r.includes('re-run'))) return 'Ran check';
  if (roles.some((r) => r.includes('recovery'))) return 'Recovered';
  return 'Involved';
}

export function Incidents() {
  const { findings, ws, param, navigate, name, agents, reviewed, toggleReviewed, openRecord, seek, source } = useRecall();
  const counts = { active: 0, insufficient: 0, resolved: 0 };
  findings.forEach((f) => counts[f.state]++);
  const selected = findings.find((f) => f.id === param);
  const [tab, setTab] = useState<Tab>(selected?.state ?? (counts.active ? 'active' : counts.insufficient ? 'insufficient' : 'resolved'));
  const [q, setQ] = useState('');
  const [monitor, setMonitor] = useState<string>('all');

  const list = useMemo(() => findings
    .filter((f) => f.state === tab)
    .filter((f) => monitor === 'all' || f.monitor === monitor)
    .filter((f) => !q || `${f.title} ${f.summary} ${ws.claims.get(f.claimId)?.text ?? ''}`.toLowerCase().includes(q.toLowerCase()))
    .sort((a, b) => STATE_ORDER[a.state] - STATE_ORDER[b.state] || b.detectedAt - a.detectedAt), [findings, tab, monitor, q, ws]);
  const current = selected ?? list[0];
  const groups = useMemo(() => groupFindings(list, (f) => ws.claims.get(f.claimId)?.subject?.artifact ?? ''), [list, ws]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const toggle = (k: string) => setExpanded((prev) => { const n = new Set(prev); if (n.has(k)) n.delete(k); else n.add(k); return n; });

  const affected = new Set(findings.flatMap((f) => findingAgents(f, ws)));
  const reach = new Set(findings.flatMap((f) => f.reach));

  return (
    <div className="page-wide">
      <div className="hero">
        <div>
          <h1>Signals worth your attention.</h1>
          <p className="sub">Evidence-backed incidents in {source?.label}. Each one links to the exact records that produced it.</p>
        </div>
        <div className="hero-actions">
          <span className="chip">As of #{ws.cursor}</span>
          <button className="btn btn-primary" onClick={() => navigate('monitors')}><IShield size={18} /> Monitor rules</button>
        </div>
      </div>

      <div className="tabs" role="tablist">
        {(['active', 'insufficient', 'resolved'] as const).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? 'on' : ''} onClick={() => setTab(t)}>
            {t === 'active' ? 'Open' : t === 'insufficient' ? 'Needs evidence' : 'Resolved'} <span className="count">{counts[t]}</span>
          </button>
        ))}
      </div>

      <div className="kpis boxed three">
        <div className="kpi"><span className="kpi-icon"><IUsers /></span><div><div className="kpi-label">Agents involved</div><div className="kpi-value">{affected.size}<small>/ {source?.agents.length}</small></div></div></div>
        <div className="kpi" title="Downstream tasks reachable through dependencies. Structural reach, not proven damage."><span className="kpi-icon"><ITasks /></span><div><div className="kpi-label">Tasks in dependency reach</div><div className="kpi-value">{reach.size}</div></div></div>
        <div className="kpi"><span className="kpi-icon"><IShield /></span><div><div className="kpi-label">Monitors active</div><div className="kpi-value">2</div></div></div>
      </div>

      <div className="split">
        <section className="card" aria-label="Incident list">
          <div className="inc-toolbar">
            <label className="input"><ISearch size={18} /><input placeholder="Search incidents…" value={q} onChange={(e) => setQ(e.target.value)} /></label>
            <select className="select" value={monitor} onChange={(e) => setMonitor(e.target.value)} aria-label="Monitor">
              <option value="all">All monitors</option>
              {registry.map((m) => <option key={m.id} value={m.id}>{m.id} · {m.title}</option>)}
            </select>
          </div>
          {list.length === 0 && (
            <div className="empty-state">
              <b>No {tab === 'active' ? 'open' : tab === 'insufficient' ? 'insufficient-evidence' : 'resolved'} incidents at #{ws.cursor}</b>
              {findings.length ? 'Try another tab.' : 'Scrub forward, or switch to a source with detected incidents.'}
            </div>
          )}
          {groups.map((g) => {
            const open = expanded.has(g.key);
            const row = (f: Finding, sub = false) => {
              const ag = findingAgents(f, ws);
              const at = ws.byId.get(f.detectedEventId);
              const subject = ws.claims.get(f.claimId)?.subject?.artifact;
              return (
                <button key={f.id} className={`inc-row ${sub ? 'inc-sub' : ''} ${current?.id === f.id ? 'sel' : ''} ${f.state}`} onClick={() => navigate('incidents', f.id)}>
                  <IAlert className={`inc-ico ${f.state}`} size={sub ? 18 : 24} />
                  <span className="inc-main">
                    <b>{sub ? `Earlier: ${plain(ws.claims.get(f.claimId)?.text ?? f.title, 70)}` : findingLabel(f)}</b>
                    <span>{subject ? subjectLabel(subject) : f.claimId} · {ag.length} agent{ag.length === 1 ? '' : 's'} · {at ? hm(at.timestamp) : ''} · #{f.detectedAt}{reviewed.has(f.id) ? ' · reviewed' : ''}</span>
                  </span>
                  {!sub && g.rest.length > 0 && (
                    <span className="inc-group-more" role="button" tabIndex={0}
                      onClick={(e) => { e.stopPropagation(); toggle(g.key); }}
                      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); toggle(g.key); } }}>
                      {open ? 'Hide' : `+${g.rest.length} similar`}
                    </span>
                  )}
                  <span className="avatar-stack">{ag.slice(0, 4).map((a) => <Avatar key={a} name={name(a)} color={agents.get(a)?.color} size="sm" />)}</span>
                  <IChevronR />
                </button>
              );
            };
            return (
              <div key={g.key}>
                {row(g.head)}
                {open && g.rest.map((f) => row(f, true))}
              </div>
            );
          })}
        </section>

        <aside className="card" aria-label="Incident detail">
          {!current ? (
            <div className="empty-state"><b>Select an incident</b>Details and source records appear here.</div>
          ) : (
            <div className="card-body stack" style={{ gap: 18 }}>
              <div className="row" style={{ alignItems: 'flex-start' }}>
                <div style={{ minWidth: 0 }}>
                  <div className="muted small">{MONITOR_LABEL[current.monitor]} · detected #{current.detectedAt}</div>
                  <h2 style={{ fontSize: 22, fontWeight: 500, letterSpacing: '-0.01em', marginTop: 4 }}>{findingLabel(current)}</h2>
                  <div className="muted small" style={{ marginTop: 4, wordBreak: 'break-all' }}>{current.title}</div>
                </div>
                <span className="spacer" />
                <StatePill state={current.state} reviewed={reviewed.has(current.id)} />
              </div>

              <div className="chain">
                {findingAgents(current, ws).map((a) => (
                  <button key={a} className="chain-node" onClick={() => navigate('agents', a)}>
                    <Avatar name={name(a)} color={agents.get(a)?.color} size="lg" />
                    <span>{name(a)}</span><small>{roleOf(current, a, ws)}</small>
                  </button>
                ))}
              </div>

              <p style={{ fontStyle: 'italic', fontSize: 15.5, lineHeight: 1.55 }}>{current.summary}</p>

              <div className="mini-tl">
                {current.evidence.map((ev) => {
                  const e = ws.byId.get(ev.eventId);
                  return (
                    <button key={ev.eventId + ev.role} onClick={() => { if (e) seek(e.sequence); openRecord(ev.eventId); }}>
                      <span className={`mdot ${e ? `tone-${eventTone(e)}` : ''}`} />
                      <b>{e ? hm(e.timestamp) : '—'}</b>
                      <span>{ev.role}</span>
                    </button>
                  );
                })}
              </div>

              {ws.claims.get(current.claimId) && (
                <div>
                  <div className="section-title">Evidence</div>
                  <div className="ev-quote">
                    <span className="qmark">“</span>
                    <p>{plain(ws.claims.get(current.claimId)!.text, 420)}</p>
                    <span className="src"><span className="evt-id">{ws.claims.get(current.claimId)!.eventId}</span>
                      <button className="link small" onClick={() => openRecord(ws.claims.get(current.claimId)!.eventId)}>Source event <IExternal /></button></span>
                  </div>
                </div>
              )}

              <p className="note">{current.explanation}</p>
              {current.resolution && <p className="note" style={{ color: 'var(--supported)' }}>✓ {current.resolution.text}</p>}

              <div className="row">
                <button className="btn btn-primary" style={{ flex: 1, justifyContent: 'center' }} onClick={() => navigate('propagation', current.claimId)}>Open investigation</button>
                <button className="btn btn-secondary" style={{ flex: 1, justifyContent: 'center' }} onClick={() => toggleReviewed(current.id)}>
                  {reviewed.has(current.id) ? 'Mark unreviewed' : 'Mark reviewed'}
                </button>
              </div>
              <p className="muted xs" style={{ marginTop: -8 }}>Review state is saved in this browser only.</p>

              <div>
                <div className="section-title">Source records <span className="tag">{current.evidence.length}</span></div>
                {current.evidence.map((ev) => {
                  const e = ws.byId.get(ev.eventId);
                  return e
                    ? <Record key={ev.eventId + ev.role} event={e} agent={agents.get(e.agentId)} role={ev.role} cursor={ws.cursor} active={ev.eventId === current.detectedEventId} refStatus={ws.refStatus} onInspect={openRecord} onJump={seek} />
                    : <UnavailableRecord key={ev.eventId + ev.role} id={ev.eventId} status={ws.refStatus(ev.eventId)} role={ev.role} />;
                })}
              </div>

              <div>
                <div className="section-title">Dependency reach <span className="tag">{current.reach.length}</span></div>
                <p className="note" style={{ marginBottom: 8 }}>Downstream of {current.taskId} in the dependency graph at #{ws.cursor}. Structural reach, not proven damage.</p>
                {current.reach.length === 0 ? <p className="muted small">No downstream tasks.</p> : current.reach.map((id) => {
                  const t = ws.tasks.get(id);
                  return t && (
                    <button key={id} className="reach-row" onClick={() => navigate('tasks', id)}>
                      <span className="mono small">{id}</span><span className="reach-title">{t.title}</span>
                      <StatusPill status={t.reportedStatus} /><EvidencePill status={t.evidenceStatus} />
                    </button>
                  );
                })}
              </div>

              {current.missing.length > 0 && (
                <div>
                  <div className="section-title">Missing evidence <span className="tag">{current.missing.length}</span></div>
                  <ul className="missing">{current.missing.map((m) => <li key={m}>{m}</li>)}</ul>
                </div>
              )}
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}
