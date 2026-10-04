// Explorer (leon/viz-polish): the store-wide view of a source — who was active when, notable moments, goal recaps,
// agent arcs and metrics over time. It draws the exact payload of `swarm-mcp render timeline`
// (timeline_html.build_timeline), written for RECALL by `swarm-mcp render recall`. Every number is a count from the
// store, every moment states its own rule ("why"), and every excerpt carries its evidence id.
import { useMemo, useState } from 'react';
import { useRecall } from '../context';
import type { ExplorerArc, ExplorerMoment, ExplorerPayload, ExplorerRecap, ExplorerSeries } from '../../model/scope';
import { copyText, fmtK, fmtN, isoDay, LAB_COLORS, monthLabel, slotColor, useScopeFile, useWidth, villageDay } from '../scopeData';
import { Avatar } from '../Pills';
import { plain } from '../labels';

const KIND_LABEL: Record<string, string> = { burst: 'Burst', silence: 'Silence', partner_shift: 'Partner shift', first_use: 'First use' };
const KIND_TONE: Record<string, string> = { burst: '#a2482c', silence: '#6f786f', partner_shift: '#3b5f94', first_use: '#7a5cb8' };
const kindLabel = (k: string) => KIND_LABEL[k] ?? k.replace(/_/g, ' ');

function laneColor(p: ExplorerPayload, i: number) {
  return LAB_COLORS[p.lanes[i]?.labg ?? 'Other'] ?? LAB_COLORS.Other;
}

/** Index rows by evidence id so excerpts can be shown for any id the payload embeds. */
function useRows(p: ExplorerPayload | null) {
  return useMemo(() => {
    const m = new Map<string, number>();
    if (p) p.id.forEach((x, i) => m.set(p.idp + x, i));
    return m;
  }, [p]);
}

function Excerpts({ p, ids, rows, max = 6 }: { p: ExplorerPayload; ids: string[]; rows: Map<string, number>; max?: number }) {
  const [copied, setCopied] = useState<string | null>(null);
  const shown = ids.filter((id) => rows.has(id)).slice(0, max);
  return (
    <div className="excerpts">
      {shown.map((id) => {
        const i = rows.get(id)!;
        const who = p.actors[p.au[i]];
        const t = p.t0 + p.t[i] * 1000;
        return (
          <div key={id} className="excerpt">
            <div className="excerpt-head">
              <b>{who?.name ?? 'unknown'}</b>
              <span className="muted xs">#{p.channels[p.c[i]]?.name ?? '?'} · {isoDay(t)} {new Date(t).toISOString().slice(11, 16)} UTC</span>
              <span className="spacer" />
              <button className="link mono xs" title="Copy the evidence id (cite with findings_record, read with core_get)" onClick={async () => { if (await copyText(id)) { setCopied(id); setTimeout(() => setCopied(null), 1400); } }}>{copied === id ? 'copied' : id.split(':').slice(0, 2).join(':') + ':…' + id.slice(-6)}</button>
            </div>
            <p>{p.s[i] || <span className="muted">(empty)</span>}</p>
          </div>
        );
      })}
      {ids.length > shown.length && <p className="muted xs">{ids.length - shown.length} more record{ids.length - shown.length === 1 ? '' : 's'} cited; open them with core_get in the swarm MCP server.</p>}
    </div>
  );
}

// ---------------------------------------------------------------- figure 1: activity

function ActivityFigure({ p, period, setPeriod, lane, setLane, moment, setMoment }: {
  p: ExplorerPayload; period: string | null; setPeriod: (id: string | null) => void; lane: number | null; setLane: (i: number | null) => void;
  moment: number | null; setMoment: (i: number | null) => void;
}) {
  const [ref, W] = useWidth<HTMLDivElement>();
  const LANE = 30, TOP = 30, MOM = 16;
  const lo = p.dens.base, hi = Math.max(p.end, lo + p.dens.bin);
  const x = (ms: number) => ((ms - lo) / (hi - lo)) * W;
  const bw = Math.max(0.6, (p.dens.bin / (hi - lo)) * W);
  const totals = useMemo(() => p.dens.lanes.map((flat) => {
    const m = new Map<number, number>();
    for (let k = 0; k < flat.length; k += 3) m.set(flat[k], (m.get(flat[k]) ?? 0) + flat[k + 2]);
    return m;
  }), [p]);
  const max = Math.max(1, ...totals.flatMap((m) => [...m.values()]));
  const H = TOP + MOM + p.lanes.length * LANE + 24;
  const moments = p.x.moments ?? [];
  const sel = p.periods.find((q) => q.id === period);
  const months = useMemo(() => {
    const out: number[] = [];
    const d = new Date(lo); d.setUTCDate(1); d.setUTCHours(0, 0, 0, 0);
    while (d.getTime() <= hi) { if (d.getTime() >= lo) out.push(d.getTime()); d.setUTCMonth(d.getUTCMonth() + 1); }
    const step = Math.ceil(out.length / 12);
    return out.filter((_, i) => i % step === 0);
  }, [lo, hi]);
  return (
    <div className="explorer-fig">
      <div className="fig-names" style={{ paddingTop: TOP + MOM }}>
        {p.lanes.map((l, i) => (
          <button key={l.id} className={`fig-name ${lane === i ? 'on' : ''}`} style={{ height: LANE }} onClick={() => setLane(lane === i ? null : i)} title={`${l.name}: ${fmtN(l.n)} messages${l.lab ? ` · ${l.lab}` : ''}`}>
            <i style={{ background: laneColor(p, i) }} /><span>{l.name}</span><small>{fmtK(l.n)}</small>
          </button>
        ))}
      </div>
      <div ref={ref} style={{ minWidth: 0 }}>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="fig-svg" role="img" aria-label="Messages per agent per day, goals along the top, notable moments marked">
        {p.periods.map((q, i) => {
          const a = x(q.s), b = x(q.e ?? hi);
          const on = q.id === period;
          return (
            <g key={q.id} className="period" onClick={() => setPeriod(on ? null : q.id)}>
              <rect x={a} y={2} width={Math.max(1, b - a)} height={TOP - 8} rx={3} className={`period-band ${i % 2 ? 'alt' : ''} ${on ? 'on' : ''}`} />
              <title>{`${q.label} (${isoDay(q.s)} → ${q.e ? isoDay(q.e) : 'open'})`}</title>
            </g>
          );
        })}
        {sel && <rect x={x(sel.s)} y={TOP - 4} width={Math.max(1, x(sel.e ?? hi) - x(sel.s))} height={H - TOP - 20} className="period-hl" />}
        {moments.map((m, i) => m.t != null && (
          <g key={i} className={`moment-mark ${moment === i ? 'on' : ''}`} onClick={() => setMoment(moment === i ? null : i)}>
            <path d={`M${x(m.t)},${TOP + MOM - 2} l-4,-8 h8 z`} fill={KIND_TONE[m.kind] ?? '#6f786f'} />
            <title>{`${kindLabel(m.kind)} · ${m.why}`}</title>
          </g>
        ))}
        {p.lanes.map((l, i) => {
          const y0 = TOP + MOM + i * LANE;
          const c = laneColor(p, i);
          return (
            <g key={l.id} opacity={lane === null || lane === i ? 1 : 0.28}>
              <rect x={0} y={y0} width={W} height={LANE} className={i % 2 ? 'lane-alt' : 'lane-base'} />
              {[...totals[i].entries()].map(([b, n]) => {
                const h = Math.max(1, Math.sqrt(n / max) * (LANE - 6));
                return <rect key={b} x={x(lo + b * p.dens.bin)} y={y0 + LANE - 3 - h} width={bw} height={h} fill={c}><title>{`${l.name} · ${isoDay(lo + b * p.dens.bin)}: ${n} messages`}</title></rect>;
              })}
            </g>
          );
        })}
        {months.map((t) => <text key={t} x={x(t)} y={H - 6} className="axis-label">{monthLabel(t)}</text>)}
      </svg>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- panels

function MomentsCard({ p, rows, period, moment, setMoment }: { p: ExplorerPayload; rows: Map<string, number>; period: string | null; moment: number | null; setMoment: (i: number | null) => void }) {
  const [kind, setKind] = useState<string | null>(null);
  const all = p.x.moments ?? [];
  const sel = p.periods.find((q) => q.id === period);
  const kinds = [...new Set(all.map((m) => m.kind))];
  const list = all.map((m, i) => ({ m, i })).filter(({ m }) => (!kind || m.kind === kind) && (!sel || (m.t != null && m.t >= sel.s && m.t <= (sel.e ?? Infinity))));
  return (
    <section className="card">
      <div className="card-head">
        <h2>Notable moments</h2><span className="spacer" />
        <div className="seg">
          <button className={kind === null ? 'on' : ''} onClick={() => setKind(null)}>All</button>
          {kinds.map((k) => <button key={k} className={kind === k ? 'on' : ''} onClick={() => setKind(k)}>{kindLabel(k)}</button>)}
        </div>
      </div>
      <div className="card-body stack">
        <p className="muted small">Bursts against a trailing baseline, shifts in who an agent names, and first uses of terms that later spread. Each states its own rule.{sel && <> Showing moments inside <b>{plain(sel.label, 60)}</b>.</>}</p>
        {list.length === 0 && <div className="empty-state"><b>No moments here</b>Try another goal or kind.</div>}
        {list.map(({ m, i }) => <MomentRow key={i} p={p} m={m} rows={rows} open={moment === i} onToggle={() => setMoment(moment === i ? null : i)} />)}
      </div>
    </section>
  );
}

function MomentRow({ p, m, rows, open, onToggle }: { p: ExplorerPayload; m: ExplorerMoment; rows: Map<string, number>; open: boolean; onToggle: () => void }) {
  const day = m.t != null ? villageDay(m.t, p.days) : null;
  return (
    <div className={`moment ${open ? 'open' : ''}`}>
      <button className="moment-head" onClick={onToggle} aria-expanded={open}>
        <span className="moment-kind" style={{ color: KIND_TONE[m.kind], borderColor: KIND_TONE[m.kind] }}>{kindLabel(m.kind)}</span>
        <span style={{ minWidth: 0 }}>
          <b>{m.term ? `“${m.term}”` : m.agent ?? m.channel ?? ''}</b>
          <span className="muted small" style={{ display: 'block' }}>{m.why}</span>
        </span>
        <span className="moment-when mono">{m.t != null ? isoDay(m.t) : ''}{day ? <small>Day {day}</small> : null}</span>
      </button>
      {open && <Excerpts p={p} ids={m.ids} rows={rows} />}
    </div>
  );
}

function MentionMatrix({ p, ment }: { p: ExplorerPayload; ment: [number, number, number][] }) {
  const n = p.lanes.length;
  const max = Math.max(1, ...ment.map((r) => r[2]));
  const grid = new Map(ment.map(([i, j, c]) => [`${i},${j}`, c]));
  const C = 18;
  return (
    <div className="matrix-wrap">
      <svg width={n * C + 120} height={n * C + 8} className="matrix" role="img" aria-label="Who names whom">
        {p.lanes.map((l, i) => <text key={l.id} x={114} y={i * C + 13} textAnchor="end" className="axis-label">{l.name.slice(0, 18)}</text>)}
        {p.lanes.map((_, i) => p.lanes.map((__, j) => {
          const c = grid.get(`${i},${j}`) ?? 0;
          return <rect key={`${i}-${j}`} x={120 + j * C} y={i * C} width={C - 2} height={C - 2} rx={3} fill={i === j ? 'var(--surface-3)' : c ? `rgba(31,77,58,${0.12 + 0.88 * Math.sqrt(c / max)})` : 'var(--surface-2)'}><title>{`${p.lanes[i].name} named ${p.lanes[j].name} ${c} times`}</title></rect>;
        }))}
      </svg>
      <p className="muted xs">Row names column. Darker = more mentions (√ scale), max {fmtN(max)}.</p>
    </div>
  );
}

function RecapCard({ p, rows, period, setPeriod }: { p: ExplorerPayload; rows: Map<string, number>; period: string | null; setPeriod: (id: string | null) => void }) {
  const sel = p.periods.find((q) => q.id === period);
  const rc: ExplorerRecap | undefined = sel ? p.x.recaps?.[sel.id] : p.x.recap_all;
  const [term, setTerm] = useState<string | null>(null);
  if (!rc) return <section className="card"><div className="card-body muted small">No recap for this selection.</div></section>;
  const act = Object.entries(rc.act).map(([i, v]) => ({ i: Number(i), m: v[0], a: v[1] })).sort((x, y) => y.m - x.m);
  const maxAct = Math.max(1, ...act.map((x) => x.m + x.a));
  const idx = sel ? p.periods.indexOf(sel) : -1;
  return (
    <section className="card">
      <div className="card-head">
        <h2>{sel ? 'Goal recap' : 'Whole-span recap'}</h2><span className="spacer" />
        {sel && <div className="row">
          <button className="btn btn-secondary btn-sm" disabled={idx <= 0} onClick={() => setPeriod(p.periods[idx - 1].id)}>← Prev</button>
          <button className="btn btn-secondary btn-sm" disabled={idx >= p.periods.length - 1} onClick={() => setPeriod(p.periods[idx + 1].id)}>Next →</button>
          <button className="link small" onClick={() => setPeriod(null)}>Whole span</button>
        </div>}
      </div>
      <div className="card-body stack">
        {sel && <div><p className="recap-goal">{sel.label}</p><span className="muted small mono">{isoDay(sel.s)} → {sel.e ? isoDay(sel.e) : 'open'}{p.days && ` · Day ${villageDay(sel.s, p.days)}–${sel.e ? villageDay(sel.e, p.days) : '…'}`}</span></div>}
        <div className="mini-kpis">
          <div><b>{fmtN(rc.totals.messages)}</b><span>messages</span></div>
          <div><b>{fmtN(rc.totals.agent_messages)}</b><span>by agents</span></div>
          <div><b>{fmtN(rc.totals.actions)}</b><span>actions</span></div>
          <div><b>{fmtN(rc.totals.actors_active)}</b><span>actors active</span></div>
        </div>
        <div className="recap-grid">
          <div>
            <div className="section-title">{rc.baseline ? 'Rising terms' : 'Most used terms'} <span className="muted xs" style={{ textTransform: 'none', letterSpacing: 0 }}>{rc.baseline ? 'vs the previous goal' : 'no earlier window to compare'}</span></div>
            <div className="terms">
              {rc.terms.map((t) => (
                <div key={t.term}>
                  <button className={`term ${term === t.term ? 'on' : ''}`} onClick={() => setTerm(term === t.term ? null : t.term)} title={t.why}>
                    <b>{t.term}</b><span className="mono">{fmtN(t.n)}{rc.baseline ? <small> ← {fmtN(t.n_before)}</small> : null}</span><span className="muted xs">{t.agents} agents</span>
                  </button>
                  {term === t.term && <div className="term-why"><p className="muted small">{t.why}</p>{t.first_id && <Excerpts p={p} ids={[t.first_id]} rows={rows} max={1} />}</div>}
                </div>
              ))}
            </div>
          </div>
          <div>
            <div className="section-title">Most active</div>
            {act.slice(0, 10).map((x) => (
              <div key={x.i} className="spread-row">
                <span className="spread-name"><Avatar name={p.lanes[x.i]?.name ?? '?'} color={laneColor(p, x.i)} size="sm" />{p.lanes[x.i]?.name}</span>
                <span className="spread-bar two"><span style={{ width: `${(x.m / maxAct) * 100}%` }} /><span className="b" style={{ width: `${(x.a / maxAct) * 100}%` }} /></span>
                <b className="mono small">{fmtN(x.m)}</b>
              </div>
            ))}
            <p className="muted xs">Dark: messages · light: actions.</p>
          </div>
        </div>
        {rc.bursts.length > 0 && (
          <div>
            <div className="section-title">Busiest threads</div>
            {rc.bursts.map((b, i) => (
              <details key={i} className="burst">
                <summary><b>#{b.channel ?? '?'}</b> <span className="mono small">{fmtN(b.n)} msgs</span> <span className="muted small">{b.s ? isoDay(b.s) : ''} · {b.agents.slice(0, 4).join(', ')}{b.agents.length > 4 ? ` +${b.agents.length - 4}` : ''}</span></summary>
                <Excerpts p={p} ids={b.ids} rows={rows} max={5} />
              </details>
            ))}
          </div>
        )}
        {rc.ment.length > 0 && <div><div className="section-title">Who names whom</div><MentionMatrix p={p} ment={rc.ment} /></div>}
      </div>
    </section>
  );
}

function ArcCard({ p, lane, setLane }: { p: ExplorerPayload; lane: number | null; setLane: (i: number | null) => void }) {
  const i = lane ?? 0;
  const arc: ExplorerArc | undefined = p.x.arcs?.[String(i)];
  const l = p.lanes[i];
  if (!l) return null;
  const maxBin = Math.max(1, ...(arc?.bins ?? []).map((b) => b[1] + b[2]));
  const W = 560, H = 90;
  const coined = (arc?.terms ?? []).filter((t) => t.role === 'coined');
  const adopted = (arc?.terms ?? []).filter((t) => t.role === 'adopted');
  const partners = (arc?.partners ?? []).filter((q) => q.out + q.in > 0).slice(-6).reverse();
  return (
    <section className="card">
      <div className="card-head">
        <h2>Agent arc</h2><span className="spacer" />
        <select className="select" value={i} onChange={(e) => setLane(Number(e.target.value))} aria-label="Agent">
          {p.lanes.map((x, k) => <option key={x.id} value={k}>{x.name}</option>)}
        </select>
      </div>
      <div className="card-body stack">
        <div className="row" style={{ gap: 12 }}>
          <Avatar name={l.name} color={laneColor(p, i)} size="lg" />
          <div><b style={{ fontSize: 17 }}>{l.name}</b><div className="muted small">{l.lab ?? 'unknown lab'} · {fmtN(l.n)} messages · first {isoDay(l.first)}{p.days && ` (Day ${villageDay(l.first, p.days)})`}</div></div>
        </div>
        {!arc ? <p className="muted small">No arc computed for this agent.</p> : <>
          <svg viewBox={`0 0 ${W} ${H}`} className="arc-svg" preserveAspectRatio="none" role="img" aria-label={`Messages and actions per ${arc.bin_days}-day bin`}>
            {arc.bins.map(([t, m, a], k) => {
              const bw = W / arc.bins.length;
              const hm = (m / maxBin) * (H - 14), ha = (a / maxBin) * (H - 14);
              return <g key={t}><rect x={k * bw + 0.5} y={H - 12 - hm - ha} width={Math.max(1, bw - 1)} height={ha} fill={laneColor(p, i)} opacity={0.35} /><rect x={k * bw + 0.5} y={H - 12 - hm} width={Math.max(1, bw - 1)} height={hm} fill={laneColor(p, i)} /><title>{`${isoDay(t)}: ${m} messages, ${a} actions`}</title></g>;
            })}
            <text x={0} y={H - 1} className="axis-label">{arc.bins[0] ? isoDay(arc.bins[0][0]) : ''}</text>
            <text x={W} y={H - 1} className="axis-label" textAnchor="end">{arc.bins.length ? isoDay(arc.bins[arc.bins.length - 1][0]) : ''}</text>
          </svg>
          <p className="muted xs">Per {arc.bin_days}-day bin · solid: messages · light: actions.</p>
          <div className="grid-2" style={{ gap: 18 }}>
            <div>
              <div className="section-title">Who it named, by goal</div>
              {partners.length === 0 ? <p className="muted small">No mentions.</p> : partners.map((q) => (
                <div key={q.id} className="partner">
                  <div className="row"><b className="small" style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{q.label}</b>
                    {q.js != null && <span className={`tag ${q.js > 0.5 ? 'stale' : 'correction'}`} title="Jensen–Shannon distance from its partners in the previous goal (0 same, 1 entirely different)">shift {q.js.toFixed(2)}</span>}</div>
                  <span className="muted xs">{q.to.slice(0, 4).map(([n, c]) => `${n} ${c}`).join(' · ') || '—'}</span>
                </div>
              ))}
            </div>
            <div>
              <div className="section-title">Terms coined <span className="tag">{coined.length}</span></div>
              <div className="chips-wrap">{coined.slice(0, 14).map((t) => <span key={t.term} className="term-chip coined" title={`first used ${t.t ? isoDay(t.t) : '?'}; ${t.n_total} uses by ${t.agents} agents`}>{t.term}<small>{t.agents}</small></span>)}{coined.length === 0 && <span className="muted small">None that spread.</span>}</div>
              <div className="section-title" style={{ marginTop: 14 }}>Terms adopted <span className="tag">{adopted.length}</span></div>
              <div className="chips-wrap">{adopted.slice(0, 14).map((t) => <span key={t.term} className="term-chip" title={`adopted ${t.t ? isoDay(t.t) : '?'}`}>{t.term}</span>)}{adopted.length === 0 && <span className="muted small">None.</span>}</div>
            </div>
          </div>
        </>}
      </div>
    </section>
  );
}

function SeriesChart({ s, days }: { s: ExplorerSeries; days: ExplorerPayload['days'] }) {
  const W = 520, H = 150, P = 4;
  const n = s.starts.length;
  const vals = s.groups.flatMap((g) => g.pts.flatMap((q) => [q[3], q[4], q[2]])).filter((v): v is number => v != null);
  const max = Math.max(1e-9, ...vals);
  const x = (k: number) => P + (k / Math.max(1, n - 1)) * (W - 2 * P);
  const y = (v: number) => H - 16 - (v / max) * (H - 26);
  const pct = s.unit === 'share' || s.kind === 'rate';
  const fmt = (v: number) => (pct ? `${Math.round(v * 100)}%` : fmtK(Math.round(v)));
  return (
    <div className="series">
      <b className="small">{s.label}</b>
      <svg viewBox={`0 0 ${W} ${H}`} className="series-svg" role="img" aria-label={s.label}>
        {[0, 0.5, 1].map((f) => <g key={f}><line x1={0} x2={W} y1={y(max * f)} y2={y(max * f)} className="gridline" /><text x={W - 2} y={y(max * f) - 3} className="axis-label" textAnchor="end">{fmt(max * f)}</text></g>)}
        {s.groups.map((g, gi) => {
          const pts = g.pts.filter((q) => q[2] != null);
          if (pts.length < 2) return null;
          const c = LAB_COLORS[g.name] ?? slotColor(gi);
          const band = pts.filter((q) => q[3] != null && q[4] != null);
          const area = band.length > 1 ? `M${band.map((q) => `${x(q[0])},${y(q[4]!)}`).join(' L')} L${[...band].reverse().map((q) => `${x(q[0])},${y(q[3]!)}`).join(' L')} Z` : '';
          return <g key={g.key}>{area && <path d={area} fill={c} opacity={0.1} />}<path d={`M${pts.map((q) => `${x(q[0])},${y(q[2]!)}`).join(' L')}`} fill="none" stroke={c} strokeWidth={1.6} /></g>;
        })}
        {n > 0 && <><text x={P} y={H - 2} className="axis-label">{isoDay(s.starts[0])}{days ? ` · Day ${villageDay(s.starts[0], days)}` : ''}</text><text x={W - P} y={H - 2} className="axis-label" textAnchor="end">{isoDay(s.starts[n - 1])}</text></>}
      </svg>
      <div className="series-legend">{s.groups.map((g, gi) => <span key={g.key}><i style={{ background: LAB_COLORS[g.name] ?? slotColor(gi) }} />{g.name}</span>)}</div>
      {s.window ? <p className="muted xs">{s.window}-day rolling mean with 95% band.</p> : null}
    </div>
  );
}

export function Explorer() {
  const { scope, param, navigate } = useRecall();
  const sources = (scope?.sources ?? []).filter((s) => s.explorer);
  const cur = sources.find((s) => s.source === param) ?? sources[0] ?? null;
  const { data: p, error } = useScopeFile<ExplorerPayload>(cur?.explorer?.file, scope?.generatedAt);
  const rows = useRows(p);
  const [period, setPeriod] = useState<string | null>(null);
  const [lane, setLane] = useState<number | null>(null);
  const [moment, setMoment] = useState<number | null>(null);

  if (!scope || sources.length === 0) {
    return (
      <div className="page-wide">
        <div className="hero"><div><h1>What happened, when, and who.</h1><p className="sub">The store-wide explorer: agent activity over time, notable moments, goal recaps and agent arcs, computed by SwarmScope.</p></div></div>
        <section className="card setup-card"><div className="card-head"><h2>No explorer data yet</h2></div><div className="card-body stack">
          <p className="muted">Add a dataset to the SwarmScope store, then write RECALL's data:</p>
          <pre className="rec-output">{'uv run --directory swarm_mcp swarm-mcp add data/ai-village\nuv run --directory swarm_mcp swarm-mcp render recall'}</pre>
        </div></section>
      </div>
    );
  }
  const m = p?.meta;
  const days = p?.days;
  const rc = p?.x.recap_all;
  return (
    <div className="page-wide">
      <div className="hero">
        <div>
          <span className="hero-kicker">SwarmScope · source <span className="mono">{cur?.source}</span>{cur?.adapter ? ` · ${cur.adapter} adapter` : ''}</span>
          <h1>What happened, when, and who.</h1>
          <p className="sub">{m ? <>{fmtN(m.total)} messages from {m.range[0]?.slice(0, 10)} to {m.range[1]?.slice(0, 10)}{m.days ? ` (${m.days})` : ''}. The {m.n_lanes} most active of {m.n_agents} agents are drawn; counts include every matching message{m.sampled ? ', while excerpts are a deterministic sample' : ''}.</> : 'Loading…'}</p>
        </div>
        {sources.length > 1 && <div className="hero-actions"><div className="seg">{sources.map((s) => <button key={s.source} className={s.source === cur?.source ? 'on' : ''} onClick={() => { setPeriod(null); setLane(null); setMoment(null); navigate('explorer', s.source); }}>{s.source}</button>)}</div></div>}
      </div>
      {error && <div className="error-banner">{error}</div>}
      {!p ? <div className="empty-state"><div className="spinner" style={{ margin: '0 auto 12px' }} />Loading {cur?.explorer ? `${(cur.explorer.bytes / 1e6).toFixed(1)} MB of explorer data` : 'explorer'}…</div> : <>
        {rc && <div className="kpis boxed">
          <div className="kpi"><div><div className="kpi-label">Messages</div><div className="kpi-value">{fmtK(rc.totals.messages ?? 0)}</div></div></div>
          <div className="kpi"><div><div className="kpi-label">By agents</div><div className="kpi-value">{fmtK(rc.totals.agent_messages ?? 0)}</div></div></div>
          <div className="kpi"><div><div className="kpi-label">Actions</div><div className="kpi-value">{fmtK(rc.totals.actions ?? 0)}</div></div></div>
          <div className="kpi"><div><div className="kpi-label">{p.periods.length ? `${p.periods[0].kind === 'village_goal' ? 'Goals' : 'Periods'}` : 'Moments'}</div><div className="kpi-value">{p.periods.length || (p.x.moments?.length ?? 0)}</div></div></div>
        </div>}
        <section className="card">
          <div className="card-head"><h2>Activity</h2><span className="spacer" />
            <span className="muted small">{p.periods.length ? 'click a goal band to recap it · ' : ''}▼ notable moments · click a name for its arc</span>
          </div>
          <div className="card-body">
            <ActivityFigure p={p} period={period} setPeriod={setPeriod} lane={lane} setLane={setLane} moment={moment} setMoment={setMoment} />
            <div className="fig-legend">
              {Object.entries(LAB_COLORS).filter(([k]) => p.lanes.some((l) => (l.labg ?? 'Other') === k)).map(([k, c]) => <span key={k}><i style={{ background: c }} />{k}</span>)}
              <span className="muted">bar height = messages per day (√ scale)</span>
              {days && <span className="muted">Village days count from {days.day_one} ({days.tz})</span>}
            </div>
          </div>
        </section>
        <div className="grid-2 explorer-grid">
          <RecapCard p={p} rows={rows} period={period} setPeriod={setPeriod} />
          <div className="stack" style={{ gap: 22 }}>
            <MomentsCard p={p} rows={rows} period={period} moment={moment} setMoment={setMoment} />
          </div>
        </div>
        <ArcCard p={p} lane={lane} setLane={setLane} />
        {(p.x.series?.length ?? 0) > 0 && (
          <section className="card">
            <div className="card-head"><h2>Metrics over time</h2><span className="spacer" /><span className="muted small">daily values, rolling mean, 95% band</span></div>
            <div className="card-body series-grid">{p.x.series!.map((s) => <SeriesChart key={s.label} s={s} days={days ?? null} />)}</div>
          </section>
        )}
        {cur?.notes.length ? <details className="card notes-card"><summary>Blind spots of this source</summary><ul>{cur.notes.map((n) => <li key={n}>{n}</li>)}</ul></details> : null}
        {p.x.errors?.length ? <details className="card notes-card"><summary>Panels that could not be computed</summary><ul>{p.x.errors.map((n) => <li key={n}>{n}</li>)}</ul></details> : null}
      </>}
    </div>
  );
}
