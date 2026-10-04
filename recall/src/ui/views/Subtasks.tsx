// Subtasks (rigel/subtask-names): how a corpus's work units (pull requests, runs, sessions) cluster into subtasks
// under five independent signals, and the typed handoffs between the actors who did them. It draws the exact payload
// of `swarm-mcp render subtasks` (subtasks_html.build_subtasks), written for RECALL by `swarm-mcp render recall`.
// Link and parent rules mirror the standalone page: a link between two subtasks is a count of typed unit edges
// (duplicates excluded); a subtask's theme is the coarser subtask holding most of its units.
import { useMemo, useState } from 'react';
import { useRecall } from '../context';
import type { SubtaskEdge, SubtasksPayload } from '../../model/scope';
import { copyText, fmtN, isoDay, slotColor, useScopeFile } from '../scopeData';
import { Avatar } from '../Pills';

const EDGE_LABEL: Record<string, string> = { builds_on: 'builds on', integrates: 'integrates', tests: 'tests', fixes: 'fixes', resubmits: 'resubmits', duplicate: 'duplicate' };
const EDGE_TONE: Record<string, string> = { builds_on: '#2f6a4f', integrates: '#3b5f94', tests: '#7a5cb8', fixes: '#c9692f', resubmits: '#a2482c', duplicate: '#8a8f88' };

function verb(p: SubtasksPayload, kind: string) {
  const v: Record<string, string> = {
    builds_on: `changed ${p.artifact}s created in`, integrates: 'imported a module created in', tests: 'added tests for a module created in',
    fixes: `fixed ${p.artifact}s created in`, resubmits: `re-created the ${p.artifact}s of`, duplicate: 'duplicated',
  };
  return v[kind] ?? kind;
}

function useModel(p: SubtasksPayload | null, method: string, level: string) {
  return useMemo(() => {
    if (!p) return null;
    const clusters = p.clusters[method]?.[level] ?? [];
    const label = new Array<number>(p.units.length).fill(-1);
    clusters.forEach((c, k) => c.forEach((u) => { label[u] = k; }));
    const map = new Map<string, { src: number; dst: number; n: number; kinds: Record<string, number>; edges: number[] }>();
    p.edges.forEach((e, x) => {
      if (e[2] === 'duplicate') return;
      const a = label[e[0]], b = label[e[1]];
      if (a < 0 || b < 0 || a === b) return;
      const key = `${a}>${b}`;
      const ln = map.get(key) ?? { src: a, dst: b, n: 0, kinds: {}, edges: [] };
      ln.n++; ln.kinds[e[2]] = (ln.kinds[e[2]] ?? 0) + 1; ln.edges.push(x);
      map.set(key, ln);
    });
    const li = p.levels.indexOf(level);
    const up = li > 0 ? (() => {
      const lab = new Array<number>(p.units.length).fill(-1);
      (p.clusters[method]?.[p.levels[li - 1]] ?? []).forEach((c, k) => c.forEach((u) => { lab[u] = k; }));
      return clusters.map((c) => { const cnt = new Map<number, number>(); let best = -1, bn = 0; c.forEach((u) => { const q = lab[u]; const n = (cnt.get(q) ?? 0) + 1; cnt.set(q, n); if (n > bn) { bn = n; best = q; } }); return best; });
    })() : null;
    const rows = clusters.map((units, k) => {
      const starts = units.map((u) => p.units[u][6]).filter((t): t is number => t != null);
      const actors = new Map<number, number>();
      units.forEach((u) => { const a = p.units[u][3]; if (a >= 0) actors.set(a, (actors.get(a) ?? 0) + 1); });
      const done = units.filter((u) => p.units[u][5] === 1).length;
      return { k, units, start: starts.length ? Math.min(...starts) : null, end: starts.length ? Math.max(...units.map((u) => p.units[u][7] ?? p.units[u][6] ?? 0)) : null,
        actors: [...actors.entries()].sort((a, b) => b[1] - a[1]), done, name: p.names[method]?.[level]?.[k] ?? `Subtask ${k + 1}`,
        keywords: p.keywords[method]?.[level]?.[k] ?? '', objective: p.objectives[method]?.[level]?.[k] ?? null, theme: up ? up[k] : null };
    });
    return { rows, links: [...map.values()].sort((a, b) => b.n - a.n), label, upLevel: li > 0 ? p.levels[li - 1] : null };
  }, [p, method, level]);
}

/** Compressed time axis: active segments laid end to end with small gaps for the idle time between them. */
function useAxis(p: SubtasksPayload | null) {
  return useMemo(() => {
    const segs = p?.segments ?? [];
    const GAP = 0.012;
    const total = segs.reduce((n, [a, b]) => n + (b - a), 0) || 1;
    const usable = 1 - GAP * Math.max(0, segs.length - 1);
    const offs: number[] = []; let acc = 0;
    segs.forEach(([a, b], i) => { offs.push(acc + i * GAP); acc += ((b - a) / total) * usable; });
    return (ms: number | null) => {
      if (ms == null || !segs.length) return 0;
      for (let i = 0; i < segs.length; i++) {
        const [a, b] = segs[i];
        if (ms <= b) return Math.max(0, offs[i] + (Math.max(ms, a) - a) / total * usable);
      }
      return 1;
    };
  }, [p]);
}

function UnitDots({ p, units, at }: { p: SubtasksPayload; units: number[]; at: (ms: number | null) => number }) {
  return (
    <div className="unit-track">
      {units.map((u) => {
        const x = p.units[u];
        const state = x[5] === 1 ? 'done' : x[5] === 0 ? 'open' : 'unknown';
        return <span key={u} className={`unit-dot ${state}`} style={{ left: `${at(x[6]) * 100}%`, background: x[3] >= 0 && x[3] < p.slots ? slotColor(x[3]) : undefined }} title={`${x[1]} · ${x[2]} (${x[4]})`} />;
      })}
    </div>
  );
}

function EdgeRow({ p, e, onUnit }: { p: SubtasksPayload; e: SubtaskEdge; onUnit: (u: number) => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="edge-row">
      <span className="edge-kind" style={{ color: EDGE_TONE[e[2]], borderColor: EDGE_TONE[e[2]] }}>{EDGE_LABEL[e[2]] ?? e[2]}</span>
      <span style={{ minWidth: 0 }}>
        <b>{p.actors[e[4]] ?? '?'}</b> {verb(p, e[2])} <b>{p.actors[e[3]] ?? '?'}</b>’s work:
        {' '}<button className="link small" onClick={() => onUnit(e[1])}>{p.units[e[1]][1]}</button> ← <button className="link small" onClick={() => onUnit(e[0])}>{p.units[e[0]][1]}</button>
        {e[6].length > 0 && <span className="muted xs mono" style={{ display: 'block' }}>{e[6].join(' · ')}</span>}
      </span>
      {e[5].length > 0 && <button className="link mono xs" title={e[5].join('\n')} onClick={async () => { setCopied(await copyText(e[5].join(' '))); setTimeout(() => setCopied(false), 1400); }}>{copied ? 'copied' : `${e[5].length} ids`}</button>}
    </div>
  );
}

export function Subtasks() {
  const { scope, param, navigate } = useRecall();
  const corpora = (scope?.sources ?? []).filter((s) => s.subtasks);
  const cur = corpora.find((s) => s.source === param) ?? corpora[0] ?? null;
  const { data: p, error } = useScopeFile<SubtasksPayload>(cur?.subtasks?.file, scope?.generatedAt);
  const [method, setMethod] = useState('combined');
  const [level, setLevel] = useState('medium');
  const [order, setOrder] = useState<'time' | 'size' | 'theme'>('time');
  const [sel, setSel] = useState<number | null>(null);
  const [unit, setUnit] = useState<number | null>(null);
  const [minSize, setMinSize] = useState(2);
  const model = useModel(p, method, level);
  const at = useAxis(p);

  if (!scope || corpora.length === 0) {
    return (
      <div className="page-wide">
        <div className="hero"><div><h1>How the work split up.</h1><p className="sub">Subtasks inferred from what agents built: work units clustered by five independent signals, and the typed handoffs between agents.</p></div></div>
        <section className="card setup-card"><div className="card-head"><h2>No subtask data yet</h2></div><div className="card-body stack">
          <p className="muted">Add a repository (or a wiki) the agents built, then write RECALL's data:</p>
          <pre className="rec-output">{'git clone --bare https://github.com/ai-village-agents/rpg-game data/repos/rpg-game.git\nuv run --directory swarm_mcp swarm-mcp add data/repos/rpg-game.git\nuv run --directory swarm_mcp swarm-mcp render recall'}</pre>
        </div></section>
      </div>
    );
  }

  const rows = (model?.rows ?? []).filter((r) => r.units.length >= minSize);
  const sorted = [...rows].sort((a, b) => order === 'size' ? b.units.length - a.units.length : order === 'theme' ? (a.theme ?? 0) - (b.theme ?? 0) || (a.start ?? 0) - (b.start ?? 0) : (a.start ?? 0) - (b.start ?? 0));
  const selRow = model?.rows.find((r) => r.k === sel) ?? null;
  const into = selRow ? (model?.links ?? []).filter((l) => l.dst === selRow.k) : [];
  const outOf = selRow ? (model?.links ?? []).filter((l) => l.src === selRow.k) : [];
  const selEdges = selRow && p ? p.edges.filter((e) => selRow.units.includes(e[1]) || selRow.units.includes(e[0])) : [];
  const themeNames = model?.upLevel && p ? p.names[method]?.[model.upLevel] ?? [] : [];
  const methods = p?.methods ?? [];
  const pickUnit = (u: number) => { setUnit(u); const k = model?.label[u]; if (k != null && k >= 0) setSel(k); };

  return (
    <div className="page-wide">
      <div className="hero">
        <div>
          <span className="hero-kicker">SwarmScope · corpus <span className="mono">{cur?.source}</span></span>
          <h1>How the work split up.</h1>
          <p className="sub">{p ? <>{fmtN(p.units.length)} {p.unit}s by {p.actors.length} actors, grouped into subtasks by {methods.length - 1} independent signals and a blend. Rows are named after the {p.unit} the others built on most; handoffs are typed and cite their records.</> : 'Loading…'}</p>
        </div>
        {corpora.length > 1 && <div className="hero-actions"><div className="seg">{corpora.map((s) => <button key={s.source} className={s.source === cur?.source ? 'on' : ''} onClick={() => { setSel(null); setUnit(null); navigate('subtasks', s.source); }}>{s.source}</button>)}</div></div>}
      </div>
      {error && <div className="error-banner">{error}</div>}
      {!p || !model ? <div className="empty-state"><div className="spinner" style={{ margin: '0 auto 12px' }} />Loading subtasks…</div> : <>
        <div className="subtask-toolbar">
          <div className="seg" role="radiogroup" aria-label="Method">{methods.map((m) => <button key={m} className={m === method ? 'on' : ''} title={p.method_desc[m]} onClick={() => { setMethod(m); setSel(null); }}>{p.method_label[m] ?? m}</button>)}</div>
          <div className="seg" role="radiogroup" aria-label="Granularity">{p.levels.map((l) => <button key={l} className={l === level ? 'on' : ''} onClick={() => { setLevel(l); setSel(null); }}>{l}</button>)}</div>
          <div className="seg" aria-label="Order"><button className={order === 'time' ? 'on' : ''} onClick={() => setOrder('time')}>by start</button><button className={order === 'size' ? 'on' : ''} onClick={() => setOrder('size')}>by size</button><button className={order === 'theme' ? 'on' : ''} disabled={!model.upLevel} onClick={() => setOrder('theme')}>by theme</button></div>
          <label className="small muted row">min {p.unit}s <input className="input" type="number" min={1} max={20} value={minSize} onChange={(e) => setMinSize(Math.max(1, Number(e.target.value) || 1))} style={{ width: 56 }} /></label>
          <span className="spacer" />
          <span className="muted small">{rows.length} of {model.rows.length} subtasks · {p.method_desc[method]}</span>
        </div>

        <div className="subtasks-layout">
          <section className="card subtask-list">
            <div className="subtask-head"><span>Subtask</span><span>Timeline (idle gaps compressed)</span><span>{p.unit}s</span></div>
            {sorted.map((r, idx) => {
              const theme = order === 'theme' && r.theme != null && (idx === 0 || sorted[idx - 1].theme !== r.theme) ? themeNames[r.theme] : null;
              return (
                <div key={r.k}>
                  {theme && <div className="theme-head">{theme}</div>}
                  <button className={`subtask-row ${sel === r.k ? 'on' : ''}`} onClick={() => { setSel(sel === r.k ? null : r.k); setUnit(null); }}>
                    <span className="subtask-name"><b>{r.name}</b><span className="muted xs">{r.keywords}</span></span>
                    <UnitDots p={p} units={r.units} at={at} />
                    <span className="subtask-size"><b>{r.units.length}</b><span className="avatar-stack">{r.actors.slice(0, 3).map(([a]) => <Avatar key={a} name={p.actors[a]} color={a < p.slots ? slotColor(a) : undefined} size="sm" />)}</span></span>
                  </button>
                </div>
              );
            })}
          </section>

          <aside className="card subtask-detail">
            {!selRow ? (
              <div className="card-body stack">
                <div className="section-title">Actors</div>
                {p.actors.slice(0, 12).map((a, i) => {
                  const led = p.units.filter((u) => u[3] === i).length;
                  return <div key={a} className="spread-row"><span className="spread-name"><Avatar name={a} color={i < p.slots ? slotColor(i) : undefined} size="sm" />{a}</span><span className="spread-bar"><span style={{ width: `${(led / Math.max(1, ...p.actors.map((_, k) => p.units.filter((u) => u[3] === k).length))) * 100}%` }} /></span><b className="mono small">{led}</b></div>;
                })}
                <p className="muted small">{p.unit}s led per actor. Pick a subtask for its handoffs, links and units.</p>
                <div className="section-title" style={{ marginTop: 10 }}>Method agreement <span className="muted xs" style={{ textTransform: 'none', letterSpacing: 0 }}>adjusted Rand index, medium level</span></div>
                <table className="agree"><thead><tr><th />{methods.map((m) => <th key={m}>{p.method_label[m] ?? m}</th>)}</tr></thead>
                  <tbody>{methods.map((a) => <tr key={a}><th>{p.method_label[a] ?? a}</th>{methods.map((b) => { const v = p.agreement[a]?.[b] ?? 0; return <td key={b} style={{ background: a === b ? 'var(--surface-3)' : `rgba(31,77,58,${Math.max(0, v) * 0.8})`, color: v > 0.45 && a !== b ? '#f4f7f2' : undefined }}>{a === b ? '' : v.toFixed(2)}</td>; })}</tr>)}</tbody></table>
              </div>
            ) : (
              <div className="card-body stack">
                <div>
                  <h2 className="detail-title">{selRow.name}</h2>
                  <span className="muted small">{selRow.keywords}</span>
                  {selRow.objective && <p className="small" style={{ marginTop: 8 }}>{selRow.objective}</p>}
                </div>
                <div className="mini-kpis">
                  <div><b>{selRow.units.length}</b><span>{p.unit}s</span></div>
                  <div><b>{selRow.done}</b><span>completed</span></div>
                  <div><b>{selRow.actors.length}</b><span>actors</span></div>
                  <div><b>{selRow.start ? isoDay(selRow.start) : '—'}</b><span>first</span></div>
                </div>
                {(into.length > 0 || outOf.length > 0) && <div className="grid-2" style={{ gap: 14 }}>
                  <div><div className="section-title">Built on ↑</div>{into.length === 0 ? <span className="muted small">nothing</span> : into.slice(0, 6).map((l) => <button key={l.src} className="link small link-block" onClick={() => setSel(l.src)}>{model.rows[l.src]?.name} <span className="muted xs">({l.n})</span></button>)}</div>
                  <div><div className="section-title">Built on by ↓</div>{outOf.length === 0 ? <span className="muted small">nothing</span> : outOf.slice(0, 6).map((l) => <button key={l.dst} className="link small link-block" onClick={() => setSel(l.dst)}>{model.rows[l.dst]?.name} <span className="muted xs">({l.n})</span></button>)}</div>
                </div>}
                <div>
                  <div className="section-title">Handoffs <span className="tag">{selEdges.length}</span></div>
                  {selEdges.length === 0 ? <p className="muted small">No typed handoffs touch this subtask.</p> : selEdges.slice(0, 14).map((e, i) => <EdgeRow key={i} p={p} e={e} onUnit={pickUnit} />)}
                </div>
                <div>
                  <div className="section-title">{p.unit}s</div>
                  <div className="unit-list">
                    {selRow.units.map((u) => {
                      const x = p.units[u];
                      return (
                        <button key={u} className={`unit-item ${unit === u ? 'on' : ''}`} onClick={() => setUnit(unit === u ? null : u)}>
                          <span className="mono xs">{x[1]}</span>
                          <span className="unit-title">{x[2]}</span>
                          <span className={`pill ${x[5] === 1 ? 'ev-supported' : x[5] === 0 ? 'ev-unknown' : 'status'}`}>{x[4]}</span>
                        </button>
                      );
                    })}
                  </div>
                </div>
                {unit != null && (
                  <div className="unit-why">
                    <div className="section-title">Why {p.units[unit][1]} sits with its neighbours</div>
                    <p className="muted xs mono">{p.units[unit][0]} · by {p.actors[p.units[unit][3]] ?? '?'} · {p.units[unit][6] ? isoDay(p.units[unit][6]!) : ''} · {p.units[unit][9]} chat mentions</p>
                    {(p.nbrs[unit] ?? []).map(([j, sim, why]) => (
                      <div key={j} className="nbr">
                        <button className="link small" onClick={() => pickUnit(j)}>{p.units[j][1]} · {p.units[j][2]}</button><span className="mono xs">{sim.toFixed(2)}</span>
                        <span className="muted xs" style={{ display: 'block' }}>{Object.entries(why).filter(([, v]) => v[0] > 0).map(([m, [s, terms]]) => `${p.method_label[m] ?? m} ${s.toFixed(2)}${terms.length ? ` (${terms.slice(0, 3).map((t) => t.replace(/^[^:]+:artifact:/, '')).join(', ')})` : ''}`).join(' · ')}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </aside>
        </div>
        {p.notes.length > 0 && <details className="card notes-card"><summary>Blind spots of this corpus</summary><ul>{p.notes.map((n) => <li key={n}>{n}</li>)}</ul></details>}
      </>}
    </div>
  );
}
