import { useEffect, useMemo, useState } from 'react';
import { useRecall } from '../context';
import { isInvariantFailure, needsMet, registry } from '../../engine/monitors';
import { CountGrade } from '../CountGrade';
import { BE_MIN_TURNS, goalChurn, longSessionNoVerdict, unverifiableByConstruction, verifyGoalNoClaim } from '../../engine/meta';
import { TYPE_LABEL } from '../format';
import type { EventType } from '../../model/types';

const FAMILY_LABEL: Record<string, string> = {
  'claim-evidence': 'Claim–evidence', propagation: 'Propagation', belief: 'Belief & memory', session: 'Session & task',
  process: 'Process & tool', swarm: 'Swarm & coordination', human: 'Human intervention', meta: 'Meta',
};
const NEED_LABEL = (n: string) => (n === 'subject' ? 'claim/check subjects' : n === 'dependency' ? 'task dependencies' : (TYPE_LABEL as Record<string, string>)[n] ?? n);

type Liveness = { generatedAt: string; monitors: Record<string, { fixtures: number; passed: boolean; live: boolean; failures: string[] }> };

export function Monitors() {
  const { source, ws, findings, allFindings, navigate, experimentOn, setExperimentOn, sourceEntry } = useRecall();
  // Monitor liveness (AK): the last `npm run check` result, written to public/data/check.json. Absent → no tag.
  const [liveness, setLiveness] = useState<Liveness | null>(null);
  useEffect(() => {
    let off = false;
    fetch(`${import.meta.env.BASE_URL}data/check.json`, { cache: 'no-cache' }).then((r) => (r.ok ? r.json() : null)).then((j) => { if (!off) setLiveness(j); }).catch(() => {});
    return () => { off = true; };
  }, []);
  const byType = useMemo(() => {
    const m = new Map<EventType, number>();
    source?.events.forEach((e) => m.set(e.type, (m.get(e.type) ?? 0) + 1));
    return [...m].sort((a, b) => b[1] - a[1]);
  }, [source]);
  const byProv = useMemo(() => {
    const m = new Map<string, number>();
    source?.events.forEach((e) => m.set(e.provenance, (m.get(e.provenance) ?? 0) + 1));
    return [...m];
  }, [source]);
  const byRule = useMemo(() => {
    const m = new Map<string, number>();
    source?.events.forEach((e) => { if (e.type === 'tool_result') m.set(e.payload.rule ?? e.payload.tool, (m.get(e.payload.rule ?? e.payload.tool) ?? 0) + 1); });
    return [...m];
  }, [source]);
  const total = source?.events.length || 1;
  const typeCount = useMemo(() => { const m = new Map<string, number>(); source?.events.forEach((e) => m.set(e.type, (m.get(e.type) ?? 0) + 1)); return m; }, [source]);
  const churn = useMemo(() => goalChurn(ws), [ws]);
  const bcNoClaim = useMemo(() => verifyGoalNoClaim(ws), [ws]);
  const bt = useMemo(() => unverifiableByConstruction(ws), [ws]);
  const be = useMemo(() => longSessionNoVerdict(ws), [ws]);
  const meta = source?.meta;

  return (
    <div className="page-wide">
      <div className="hero">
        <div>
          <h1>Rules, not guesses.</h1>
          <p className="sub">Every monitor is a deterministic function over the records visible at the cursor: no confidence scores, no causality from timing, and an agent only counts as having seen a correction if it acknowledged it.</p>
        </div>
      </div>

      <div className="grid-2">
        {registry.map((m) => {
          const now = findings.filter((f) => f.monitor === m.id);
          const all = allFindings.filter((f) => f.monitor === m.id);
          const last = [...now].sort((a, b) => b.detectedAt - a.detectedAt)[0];
          const app = needsMet(m, source?.events ?? []);
          const invariant = all.filter(isInvariantFailure);
          return (
            <section key={m.id} className={`card monitor-card ${app.met ? '' : 'na'}`}>
              <div className="row">
                <span className="tag real">Monitor {m.id}</span>
                <span className="tag">{FAMILY_LABEL[m.family] ?? m.family}</span>
                {liveness?.monitors[m.id] && (
                  <span className={`tag ${liveness.monitors[m.id].passed && liveness.monitors[m.id].live ? 'real' : 'disputed'}`}
                    title={`npm run check · ${liveness.generatedAt.slice(0, 16).replace('T', ' ')} UTC · ${liveness.monitors[m.id].fixtures} fixture assertions${liveness.monitors[m.id].failures.length ? ` · failing: ${liveness.monitors[m.id].failures.join('; ')}` : ''}`}>
                    {liveness.monitors[m.id].passed ? (liveness.monitors[m.id].live ? 'fixture live' : 'vacuous') : 'fixture failing'}
                  </span>
                )}
                <span className="spacer" />
                {app.met
                  ? <span className="muted small">{now.length} at #{ws.cursor} · {all.length} in full log</span>
                  : <span className="tag disputed" title={`Needs: ${app.unmet.map(NEED_LABEL).join(', ')}`}>Not applicable to this source</span>}
              </div>
              <h3>{m.title}</h3>
              {invariant.length > 0 && (
                <button className="tag stale" style={{ alignSelf: 'flex-start' }} onClick={() => navigate('incidents', invariant[0].id)}
                  title="Findings that broke a monitor invariant were converted to insufficient instead of being dropped">
                  {invariant.length} finding{invariant.length > 1 ? 's' : ''} failed an invariant → shown under Needs evidence
                </button>
              )}
              <pre className="rule" style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{m.rule}</pre>
              {m.ruling && <p className="ruling"><b>Owner ruling (2026-10-03):</b> {m.ruling}</p>}
              {m.id === 'C' && <div><div className="section-title">Count-grade claims (not incidents)</div><CountGrade /></div>}
              <div className="row" style={{ flexWrap: 'wrap', gap: 6 }}>
                <span className="muted xs">Needs</span>
                {m.needs.map((n) => <span key={n} className={`filter ${app.unmet.includes(n) ? '' : 'on'}`} style={{ height: 24, fontSize: 12 }}>{NEED_LABEL(n)}</span>)}
                <span className="spacer" />
                <span className="mono xs muted" title="Sabotage fixture run by npm run check">{m.fixture}</span>
              </div>
              {!app.met
                ? <span className="muted small">This source has no {app.unmet.map(NEED_LABEL).join(' or ')}, so the monitor cannot apply. Zero findings here is not a clean bill.</span>
                : all.length === 0 ? (
                  <span className="small"><span className="tag">0 on this source</span> <span className="muted">Coverage: this source records {[...new Set([...m.needs, ...(m.reads ?? [])])].filter((n) => n !== 'subject' && n !== 'dependency').map((n) => `${typeCount.get(n) ?? 0} ${NEED_LABEL(n).toLowerCase()}`).join(' · ')}.</span></span>
                )
                : last ? <button className="link" onClick={() => navigate('incidents', last.id)}>Latest: {last.title} (#{last.detectedAt}) →</button>
                  : <span className="muted small">Not fired at this point in time.</span>}
            </section>
          );
        })}
      </div>

      <section className="card monitor-card">
        <div className="row"><h3>Meta counts</h3><span className="spacer" /><span className="muted small">Shown here, not as incidents · at #{ws.cursor}</span></div>
        <div className="meta-grid">
          <div><span className="tag">BG</span> <b>Goal churn</b><p className="muted small">Agents with ≥ 5 sessions whose goals are pairwise distinct (token Jaccard &lt; 0.3), and zero verdicts and zero claims.</p>
            <p className="meta-count">{churn.length}</p>{churn.slice(0, 6).map((r) => <button key={r.agentId} className="link small" onClick={() => navigate('agents', r.agentId)}>{ws.agentName(r.agentId)} · {r.sessionIds.length} sessions</button>)}</div>
          <div><span className="tag">BC</span> <b>Verify-goal sessions without a claim</b><p className="muted small">Ended with zero verification verdicts and no claim (BC is an incident only when the session claimed something).</p>
            <p className="meta-count">{bcNoClaim.length}</p>{bcNoClaim.slice(0, 6).map((r) => <button key={r.taskId} className="link small" onClick={() => navigate('tasks', r.taskId)}>{r.taskId}</button>)}</div>
          <div><span className="tag">BT</span> <b>Unverifiable by construction</b><p className="muted small">Claims whose subject type no verdict rule produces in this source.</p>
            <p className="meta-count">{bt.reduce((n, r) => n + r.claimIds.length, 0)}</p>{bt.slice(0, 6).map((r) => <button key={r.agentId} className="link small" onClick={() => navigate('agents', r.agentId)}>{ws.agentName(r.agentId)} · {r.claimIds.length}</button>)}</div>
          <div><span className="tag">BE</span> <b>Long session, no verdict</b><p className="muted small">Ended sessions with ≥ {BE_MIN_TURNS} turns, zero verdicts and zero claims: coverage, not an accusation.</p>
            <p className="meta-count">{be.length}</p>{be.slice(0, 6).map((r) => <button key={r.taskId} className="link small" onClick={() => navigate('tasks', r.taskId)}>{ws.agentName(r.agentId)} · {r.turns} turns</button>)}</div>
        </div>
      </section>

      <section className="card monitor-card">
        <div className="row">
          <h3>Evidence visibility experiment</h3><span className="spacer" />
          {source?.experiment ? (
            <label className="switch-row">
              <span className="small">{experimentOn ? 'On' : 'Off'}</span>
              <input type="checkbox" checked={experimentOn} onChange={(e) => setExperimentOn(e.target.checked)} />
              <span className="switch" aria-hidden />
            </label>
          ) : <span className="muted small">Not available for this source</span>}
        </div>
        <p className="note">
          {source?.experiment
            ? <>{source.experiment.description} Withheld records are removed from the analysis input before reconstruction, so monitors must report <b>insufficient evidence</b> rather than confirm or clear findings that depended on them. Withheld ≠ missing: a <span className="ref ref-withheld">withheld</span> record exists in the source; a <span className="ref ref-missing">not in dataset</span> reference does not.</>
            : 'This source does not define records to withhold. The synthetic demo includes one (the decisive failed test run).'}
        </p>
      </section>

      <div className="grid-2">
        <section className="card monitor-card">
          <h3>Source</h3>
          <dl className="kv">
            <dt>Label</dt><dd>{source?.label}</dd>
            <dt>Origin</dt><dd>{meta?.origin === 'huggingface' ? 'Hugging Face dataset (real records)' : meta?.origin === 'file' ? 'Imported file' : 'Synthetic demonstration'}</dd>
            {meta?.dataset && <><dt>Dataset</dt><dd><a className="link" href={`https://huggingface.co/datasets/${meta.dataset}`} target="_blank" rel="noreferrer">{meta.dataset}</a></dd></>}
            {meta?.goal && <><dt>Village goal</dt><dd>{meta.goal}</dd></>}
            {(meta?.window ?? sourceEntry?.window) && <><dt>Window</dt><dd className="mono">{(meta?.window ?? sourceEntry!.window)!.from} → {(meta?.window ?? sourceEntry!.window)!.to}</dd></>}
            {meta?.generatedAt && <><dt>Extracted</dt><dd className="mono">{meta.generatedAt}</dd></>}
            {meta?.rows && <><dt>Rows read</dt><dd>{Object.entries(meta.rows).map(([k, v]) => `${k}: ${v}`).join(' · ')}</dd></>}
            {meta?.citation && <><dt>Citation</dt><dd>{meta.citation}</dd></>}
          </dl>
          {meta?.notes?.length ? <ul className="missing">{meta.notes.map((n) => <li key={n}>{n}</li>)}</ul> : null}
          <p className="muted small">{source?.description}</p>
        </section>
        <section className="card monitor-card">
          <h3>What the analysis sees</h3>
          <div className="section-title" style={{ margin: 0 }}>Records by type</div>
          <div>{byType.map(([t, n]) => <div key={t} className="bar-row"><span>{TYPE_LABEL[t]}</span><span className="bar"><i style={{ width: `${(n / total) * 100}%` }} /></span><span className="mono small">{n}</span></div>)}</div>
          <div className="section-title" style={{ margin: '6px 0 0' }}>Relationship provenance</div>
          <div>{byProv.map(([p, n]) => <div key={p} className="bar-row"><span className={`prov prov-${p}`} style={{ justifySelf: 'start' }}>{p}</span><span className="bar"><i style={{ width: `${(n / total) * 100}%` }} /></span><span className="mono small">{n}</span></div>)}</div>
          {byRule.length > 0 && <>
            <div className="section-title" style={{ margin: '6px 0 0' }}>Tool-result verdict rules</div>
            <div>{byRule.map(([p, n]) => <div key={p} className="bar-row"><span className="mono small">{p}</span><span className="bar"><i style={{ width: `${(n / Math.max(...byRule.map((x) => x[1]))) * 100}%` }} /></span><span className="mono small">{n}</span></div>)}</div>
          </>}
        </section>
      </div>
    </div>
  );
}
