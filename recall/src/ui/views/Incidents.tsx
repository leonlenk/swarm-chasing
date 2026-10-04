import { useMemo, useState } from 'react';
import { useRecall } from '../context';
import type { Finding } from '../../engine/monitors';
import { MONITOR_LABEL, registry } from '../../engine/monitors';
import { Avatar, EvidencePill, StatePill, StatusPill } from '../Pills';
import { Record, UnavailableRecord } from '../Record';
import { findingAgents, findingLabel, groupFindings, plain, STATE_ORDER, subjectLabel } from '../labels';
import { eventTone } from '../format';
import { bucketOf, remediationFor, triage } from '../../engine/triage';
import { subjectIncidents, subjectOfFinding } from '../../engine/subjects';
import { SubjectCard } from '../SubjectCard';
import { IAlert, IChevronR, IExternal, ISearch, IShield, ITasks, IUsers } from '../icons';

const hm = (iso: string) => new Date(iso).toISOString().slice(11, 16);
type Tab = 'open' | 'needs' | 'patterns' | 'resolved';
const TAB_LABEL: Record<Tab, string> = { open: 'Open', needs: 'Needs evidence', patterns: 'Patterns', resolved: 'Resolved' };
const TAB_EMPTY: Record<Tab, string> = { open: 'contradicted', needs: 'unchecked', patterns: 'pattern', resolved: 'resolved' };

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
  const tri = useMemo(() => triage(findings, ws), [findings, ws]);
  const counts = { open: tri.open.length, needs: tri.needs.length, patterns: tri.patterns.length, resolved: tri.resolved.length };
  const selected = findings.find((f) => f.id === param);
  const selBucket = selected ? bucketOf(selected, ws) : undefined;
  const [tab, setTab] = useState<Tab>(selBucket && selBucket !== 'count' ? selBucket : counts.open ? 'open' : counts.needs ? 'needs' : counts.patterns ? 'patterns' : 'resolved');
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const toggleAgent = (a: string) => setCollapsed((prev) => { const n = new Set(prev); if (n.has(a)) n.delete(a); else n.add(a); return n; });
  const [q, setQ] = useState('');
  const [monitor, setMonitor] = useState<string>('all');

  const list = useMemo(() => findings
    .filter((f) => bucketOf(f, ws) === tab)
    .filter((f) => monitor === 'all' || f.monitor === monitor)
    .filter((f) => !q || `${f.title} ${f.summary} ${ws.claims.get(f.claimId)?.text ?? ''}`.toLowerCase().includes(q.toLowerCase()))
    .sort((a, b) => STATE_ORDER[a.state] - STATE_ORDER[b.state] || b.detectedAt - a.detectedAt), [findings, tab, monitor, q, ws]);
  const current = selected ?? list[0];
  // One card per SubjectIncident (owner ruling): findings on a claimed subject live inside their subject's card.
  const incidents = useMemo(() => new Map(subjectIncidents(ws, findings).map((i) => [i.subject, i])), [ws, findings]);
  const build = (fs: Finding[]) => {
    const bySubject = new Map<string, Finding[]>();
    const loose: Finding[] = [];
    for (const f of fs) { const k = subjectOfFinding(f, ws); if (k && incidents.has(k)) bySubject.set(k, [...(bySubject.get(k) ?? []), f]); else loose.push(f); }
    const cards = [...bySubject.entries()].map(([k, members]) => ({ inc: incidents.get(k)!, members }))
      .sort((a, b) => b.members.length - a.members.length || Math.max(...b.members.map((f) => f.detectedAt)) - Math.max(...a.members.map((f) => f.detectedAt)));
    return { cards, groups: groupFindings(loose, (f) => f.monitor) };
  };
  // Needs evidence: collapsible by agent, then by subject.
  const sections = useMemo(() => {
    if (tab !== 'needs') return [{ agent: '', ...build(list) }];
    const by = new Map<string, Finding[]>();
    for (const f of list) by.set(f.agentId, [...(by.get(f.agentId) ?? []), f]);
    return [...by.entries()].sort((a, b) => b[1].length - a[1].length).map(([agent, fs]) => ({ agent, ...build(fs) }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [list, ws, tab, incidents]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const toggle = (k: string) => setExpanded((prev) => { const n = new Set(prev); if (n.has(k)) n.delete(k); else n.add(k); return n; });

  const row = (f: Finding, sub = false, more = 0, isOpen = false, onMore?: () => void) => {
    const ag = findingAgents(f, ws);
    const at = ws.byId.get(f.detectedEventId);
    const subject = ws.claims.get(f.claimId)?.subject?.artifact;
    return (
      <button key={f.id} className={`inc-row ${sub ? 'inc-sub' : ''} ${current?.id === f.id ? 'sel' : ''} ${f.state}`} onClick={() => navigate('incidents', f.id)}>
        <IAlert className={`inc-ico ${f.state}`} size={sub ? 18 : 24} />
        <span className="inc-main">
          <b>{findingLabel(f)}</b>
          <span>{subject ? subjectLabel(subject) : plain(f.title, 60)} · {ag.length} agent{ag.length === 1 ? '' : 's'} · {at ? hm(at.timestamp) : ''} · #{f.detectedAt}{reviewed.has(f.id) ? ' · reviewed' : ''}</span>
        </span>
        {more > 0 && onMore && (
          <span className="inc-group-more" role="button" tabIndex={0}
            onClick={(e) => { e.stopPropagation(); onMore(); }}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); onMore(); } }}>
            {isOpen ? 'Hide' : `+${more} similar`}
          </span>
        )}
        <span className="avatar-stack">{ag.slice(0, 4).map((a) => <Avatar key={a} name={name(a)} color={agents.get(a)?.color} size="sm" />)}</span>
        <IChevronR />
      </button>
    );
  };

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
        {(['open', 'needs', 'patterns', 'resolved'] as const).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? 'on' : ''} onClick={() => setTab(t)}
            title={t === 'open' ? 'Records contradict what was claimed (A, D, E, G, J, W, Y, AO, AP, AQ, AR, AY)' : t === 'needs' ? 'Claims the records cannot confirm yet (C, Z, AM, AC, BM, BJ, BK)' : t === 'patterns' ? 'Process, swarm, session and human patterns' : undefined}>
            {TAB_LABEL[t]} <span className="count">{counts[t]}</span>
          </button>
        ))}
      </div>

      <div className="kpis boxed three">
        <div className="kpi"><span className="kpi-icon"><IUsers /></span><div><div className="kpi-label">Agents involved</div><div className="kpi-value">{affected.size}<small>/ {source?.agents.length}</small></div></div></div>
        <div className="kpi" title="Downstream tasks reachable through dependencies. Structural reach, not proven damage."><span className="kpi-icon"><ITasks /></span><div><div className="kpi-label">Tasks in dependency reach</div><div className="kpi-value">{reach.size}</div></div></div>
        <div className="kpi" title="Needs evidence: claims the records cannot confirm yet"><span className="kpi-icon"><IShield /></span><div><div className="kpi-label">Unchecked claims</div><div className="kpi-value">{counts.needs}<small> · {tri.count.length} count-grade</small></div></div></div>
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
              <b>No {TAB_EMPTY[tab]} findings at #{ws.cursor}</b>
              {findings.length ? 'Try another tab.' : 'Scrub forward, or switch to a source with detected incidents.'}
            </div>
          )}
          {sections.map((sec) => (
          <div key={sec.agent || 'all'}>
            {sec.agent && (
              <button className="agent-section" onClick={() => toggleAgent(sec.agent)} aria-expanded={!collapsed.has(sec.agent)}>
                <IChevronR className={collapsed.has(sec.agent) ? '' : 'rot'} size={16} />
                <Avatar name={name(sec.agent)} color={agents.get(sec.agent)?.color} size="sm" />
                <b>{name(sec.agent)}</b><span className="muted small">{sec.cards.reduce((n, c) => n + c.members.length, 0) + sec.groups.reduce((n, g) => n + 1 + g.rest.length, 0)} unchecked · {sec.cards.length} subject{sec.cards.length === 1 ? '' : 's'}</span>
              </button>
            )}
            {!collapsed.has(sec.agent) && (
              <>
                {sec.cards.map((c) => <SubjectCard key={c.inc.subject} inc={c.inc} members={c.members} selectedId={current?.id} renderRow={(f) => row(f, true)} />)}
                {sec.groups.map((g) => (
                  <div key={g.key}>
                    {row(g.head, false, g.rest.length, expanded.has(g.key), () => toggle(g.key))}
                    {expanded.has(g.key) && g.rest.map((f) => row(f, true))}
                  </div>
                ))}
              </>
            )}
          </div>
          ))}
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
                  {current.attributes?.length ? <div className="row" style={{ marginTop: 6, flexWrap: 'wrap' }}>{current.attributes.map((a) => <span key={a} className="tag disputed">{a}</span>)}</div> : null}
                  {(() => { const r = remediationFor(current, ws); return r ? (
                    <div className="remedy">
                      <div className="row"><b>Suggested remediation</b><span className="spacer" /><span className="muted small">rule template · owner: {r.owner}</span></div>
                      <ol>{r.steps.map((st) => <li key={st}>{st}</li>)}</ol>
                      <div className="muted small">Resolves when: {r.resolvesWith}.</div>
                    </div>) : null; })()}
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
