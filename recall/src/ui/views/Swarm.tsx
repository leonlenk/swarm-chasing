// Swarm dynamics (owner ruling 2026-10-04): rates as count pairs (never a bare percentage), checking spread as a
// sorted bar of counts, agent roles across subjects, single points of failure. Every count opens its records.
// Sparklines come from public/data/series/<part>.json (built by npm run data:build).
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { useRecall } from '../context';
import { agentRoles, CASCADE_ALERT, ROLE_LABEL, SPOF_MIN_SUBJECTS, subjectIncidents, swarmRates, type Role } from '../../engine/subjects';
import { plain, subjectLabel } from '../labels';
import { Avatar, Spark } from '../Pills';

type SeriesPoint = { seq: number; repeatsWithoutCheck: [number, number]; checks: number; checkers: number; consensusWithoutCheck: number; duplicateGoals: number;
  directiveUptake: [number, number]; correctionReach: [number, number]; cascadesWithoutCheck: number; singlePoints: number };

function Rate({ k, label, hint, value, of, sp, body, open, onToggle }: { k: string; label: string; hint: string; value: number; of?: number; sp: ReactNode; body: ReactNode; open: boolean; onToggle: (k: string) => void }) {
  return (
    <div className="rate">
      <button className="rate-head" onClick={() => onToggle(k)} aria-expanded={open} title={hint}>
        <span className="kpi-label">{label}</span>
        <span className="rate-value"><b>{value}</b>{of !== undefined && <small> of {of}</small>}</span>
        {sp}
      </button>
      <p className="muted small">{hint}</p>
      {open && body}
    </div>
  );
}

const ROLES: Role[] = ['announcer', 'repeater', 'adopter-without-check', 'verifier', 'corrector'];

export function Swarm() {
  const { source, ws, findings, name, agents, openRecord, navigate } = useRecall();
  const incidents = useMemo(() => subjectIncidents(ws, findings), [ws, findings]);
  const rates = useMemo(() => swarmRates(ws, findings, incidents), [ws, findings, incidents]);
  const roles = useMemo(() => agentRoles(incidents), [incidents]);
  const [open, setOpen] = useState<string | null>(null);
  const [series, setSeries] = useState<SeriesPoint[] | null>(null);
  useEffect(() => {
    let off = false;
    if (!source) return;
    fetch(`${import.meta.env.BASE_URL}data/series/${source.id.replace(/^[a-z-]+:/, "")}.json`, { cache: 'no-cache' })
      .then((r) => (r.ok ? r.json() : null)).then((j) => { if (!off) setSeries(j?.points ?? null); }).catch(() => { if (!off) setSeries(null); });
    return () => { off = true; };
  }, [source]);
  const upTo = (series ?? []).filter((p) => p.seq <= ws.cursor);
  const spark = (f: (p: SeriesPoint) => number) => (upTo.length > 1 ? <Spark width={110} values={upTo.map(f)} /> : <span className="muted small">{series ? '' : 'no series'}</span>);
  const toggle = (k: string) => setOpen(open === k ? null : k);
  const recs = (ids: string[]) => (
    <div className="count-list">{ids.length === 0 ? <span className="muted small">None.</span> : ids.slice(0, 80).map((id) => {
      const e = ws.byId.get(id);
      return <button key={id} className="link small" onClick={() => openRecord(id)}>#{e?.sequence} · {e ? name(e.agentId) : ''}: {plain(e?.text ?? id, 90)}</button>;
    })}</div>
  );
  const subjects = (keys: string[]) => (
    <div className="count-list">{keys.length === 0 ? <span className="muted small">None.</span> : keys.map((k) => {
      const inc = incidents.find((i) => i.subject === k);
      const f = inc?.findings[0];
      return <button key={k} className="link small" onClick={() => (f ? navigate('incidents', f.id) : inc && openRecord(inc.firstClaim.id))}>{subjectLabel(k.split('@')[0])} · {inc ? `${inc.repeats.length + 1} claims, ${inc.checks.length} checks` : ''}</button>;
    })}</div>
  );
  const findingsList = (ids: string[]) => (
    <div className="count-list">{ids.length === 0 ? <span className="muted small">None.</span> : ids.map((id) => {
      const f = findings.find((x) => x.id === id);
      return <button key={id} className="link small" onClick={() => navigate('incidents', id)}>{f?.summary.slice(0, 110) ?? id}</button>;
    })}</div>
  );
  const cascades = incidents.filter((i) => i.cascade.n >= CASCADE_ALERT && i.standing === 'unchecked');
  const maxChecks = Math.max(1, ...rates.checksPerAgent.map((c) => c.n));
  const spof = new Set(rates.singlePoints.map((s) => s.agent));
  const agentList = [...roles.keys()].sort((a, b) => [...(roles.get(b)?.values() ?? [])].flat().length - [...(roles.get(a)?.values() ?? [])].flat().length);

  return (
    <div className="page-wide">
      <div className="hero">
        <div>
          <h1>How the swarm checks itself.</h1>
          <p className="sub">Counts over {incidents.length} claimed subjects in {source?.label}, as of #{ws.cursor}. Every number opens the records behind it; nothing here is a score.</p>
        </div>
      </div>

      <div className="rates">
        <Rate k="rep" open={open === "rep"} onToggle={toggle} label="Repeats without an own check" hint="Later claims of a subject by another agent who had not checked it first."
          value={rates.repeatsWithoutCheck.n} of={rates.repeatsWithoutCheck.of} sp={spark((p) => p.repeatsWithoutCheck[0])} body={recs(rates.repeatsWithoutCheck.records)} />
        <Rate k="cons" open={open === "cons"} onToggle={toggle} label="Consensus without any check" hint="Subjects claimed by ≥ 3 agents with no verification by anyone (BP)."
          value={rates.consensusWithoutCheck.n} sp={spark((p) => p.consensusWithoutCheck)} body={subjects(rates.consensusWithoutCheck.subjects)} />
        <Rate k="casc" open={open === "casc"} onToggle={toggle} label={`Cascades without a check`} hint={`Subjects with ≥ ${CASCADE_ALERT} repeaters and zero checks (alert threshold).`}
          value={cascades.length} sp={spark((p) => p.cascadesWithoutCheck)} body={subjects(cascades.map((c) => c.subject))} />
        <Rate k="reach" open={open === "reach"} onToggle={toggle} label="Correction reach" hint="Agents who claimed the subject again after a correction or failure report, of the agents who had claimed it before."
          value={rates.correctionReach.n} of={rates.correctionReach.of} sp={spark((p) => p.correctionReach[0])} body={recs(rates.correctionReach.records)} />
        <Rate k="dir" open={open === "dir"} onToggle={toggle} label="Directive uptake" hint="Directives followed by a matching session or acknowledgement (AC resolved), of all directives."
          value={rates.directiveUptake.n} of={rates.directiveUptake.of} sp={spark((p) => p.directiveUptake[0])} body={findingsList(rates.directiveUptake.findings)} />
        <Rate k="dup" open={open === "dup"} onToggle={toggle} label="Duplicate goals" hint="Concurrent sessions by different agents with the same goal (V)."
          value={rates.duplicateGoals.n} sp={spark((p) => p.duplicateGoals)} body={findingsList(rates.duplicateGoals.findings)} />
      </div>

      <div className="grid-2">
        <section className="card">
          <div className="card-head"><h2>Checking spread</h2><span className="spacer" /><span className="muted small">verification records per agent</span></div>
          <div className="card-body">
            {rates.checksPerAgent.length === 0 ? <p className="muted small">No verification records at #{ws.cursor}.</p> : rates.checksPerAgent.map((c) => (
              <div key={c.agent} className="spread-row">
                <span className="spread-name"><Avatar name={name(c.agent)} color={agents.get(c.agent)?.color} size="sm" />{name(c.agent)}</span>
                <span className="spread-bar"><span style={{ width: `${(c.n / maxChecks) * 100}%` }} /></span>
                <b>{c.n}</b>
              </div>
            ))}
            <p className="muted small" style={{ marginTop: 8 }}>{rates.checksPerAgent.length} of {source?.agents.length ?? 0} agents ran any check{rates.checksPerAgent[0] ? `; the top checker ran ${rates.checksPerAgent[0].n} of ${rates.checksPerAgent.reduce((n, x) => n + x.n, 0)}` : ''}.</p>
          </div>
        </section>

        <section className="card">
          <div className="card-head"><h2>Single points of failure</h2><span className="spacer" /><span className="muted small">sole verifier for ≥ {SPOF_MIN_SUBJECTS} subjects</span></div>
          <div className="card-body stack">
            {rates.singlePoints.length === 0 ? <p className="muted small">No agent is the only verifier of {SPOF_MIN_SUBJECTS} or more subjects.</p> : rates.singlePoints.map((s) => (
              <div key={s.agent}>
                <button className="count-row" onClick={() => toggle(`spof:${s.agent}`)}><b>{s.subjects.length}</b><span>{name(s.agent)} is the only agent who checked these subjects</span></button>
                {open === `spof:${s.agent}` && subjects(s.subjects)}
              </div>
            ))}
          </div>
        </section>
      </div>

      <section className="card">
        <div className="card-head"><h2>Agent roles across subjects</h2><span className="spacer" /><span className="muted small">click a count to see its subjects</span></div>
        <div className="card-body roles-table" role="table">
          <div className="roles-row head" role="row"><span>Agent</span>{ROLES.map((r) => <span key={r}>{ROLE_LABEL[r]}</span>)}</div>
          {agentList.map((a) => (
            <div key={a}>
              <div className="roles-row" role="row">
                <span className="spread-name"><Avatar name={name(a)} color={agents.get(a)?.color} size="sm" />{name(a)}{spof.has(a) && <span className="tag disputed">single point</span>}</span>
                {ROLES.map((r) => {
                  const list = roles.get(a)?.get(r) ?? [];
                  return <span key={r}>{list.length ? <button className="link" onClick={() => toggle(`role:${a}:${r}`)}>{list.length}</button> : <span className="muted">·</span>}</span>;
                })}
              </div>
              {ROLES.map((r) => open === `role:${a}:${r}` ? <div key={r}>{subjects(roles.get(a)?.get(r) ?? [])}</div> : null)}
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
