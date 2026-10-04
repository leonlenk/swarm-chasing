import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useRecall, type View } from './context';
import {
  IBranch, IChevron, IDb, IDoc, IExternal, IGrid, IHome, ILive, IMap, IPlug, IPulse, ISearch, IShield, ITasks, IUpload, IUsers, IX, Logo,
} from './icons';
import { copyText } from './scopeData';
import { Record, RefLink, UnavailableRecord } from './Record';
import { findingLabel, VIEW_LABEL } from './labels';
import { TYPE_LABEL } from './format';
import { triage } from '../engine/triage';

const NAV: { group: string; items: { view: View; label: string; icon: ReactNode }[] }[] = [
  { group: 'Observe', items: [
    { view: 'sessions', label: 'Live sessions', icon: <ILive /> },
    { view: 'overview', label: 'Overview', icon: <IHome /> },
    { view: 'propagation', label: 'Propagation', icon: <IBranch /> },
    { view: 'tasks', label: 'Tasks', icon: <ITasks /> },
    { view: 'agents', label: 'Agents', icon: <IUsers /> },
  ] },
  { group: 'Investigate', items: [
    { view: 'incidents', label: 'Incidents', icon: <IShield /> },
    { view: 'swarm', label: 'Swarm', icon: <IUsers /> },
    { view: 'monitors', label: 'Monitors', icon: <IPulse /> },
    { view: 'evidence', label: 'Evidence', icon: <IDoc /> },
  ] },
  { group: 'SwarmScope store', items: [
    { view: 'explorer', label: 'Explorer', icon: <IMap /> },
    { view: 'subtasks', label: 'Subtasks', icon: <IGrid /> },
  ] },
  { group: 'Integrate', items: [
    { view: 'connect', label: 'Plugin & MCP', icon: <IPlug /> },
  ] },
];

const ORIGIN_LABEL = { huggingface: 'Real records · Hugging Face', file: 'Imported file', synthetic: 'Synthetic demonstration', live: 'Claude Code · swarm-live' } as const;
export function Sidebar() {
  const { view, navigate, findings, source, sourceEntry, ws, scope } = useRecall();
  const recording = (scope?.live.sessions ?? []).filter((s) => s.status === 'running').length;
  const tri = triage(findings, ws);
  const active = tri.open.length;
  const insufficient = tri.needs.length;
  const real = source?.meta?.origin === 'huggingface' || (source?.meta?.origin === 'live' && !source.meta.live?.synthetic);
  return (
    <aside className="sidebar">
      <div className="logo"><Logo /><span className="logo-word">RECALL</span></div>
      {NAV.map((g) => (
        <nav className="nav-group" key={g.group} aria-label={g.group}>
          <div className="nav-label">{g.group}</div>
          {g.items.map((it) => (
            <button key={it.view} className={`nav-item ${view === it.view ? 'active' : ''}`} onClick={() => navigate(it.view)} aria-current={view === it.view ? 'page' : undefined}>
              {it.icon}<span className="nav-text">{it.label}</span>
              {it.view === 'incidents' && active > 0 && <span className="nav-badge" title={`${active} open (contradicted)`}>{active}</span>}
              {it.view === 'incidents' && !active && insufficient > 0 && <span className="nav-badge warn" title={`${insufficient} unchecked claims`}>{insufficient}</span>}
              {it.view === 'sessions' && recording > 0 && <span className="nav-badge live" title={`${recording} session${recording === 1 ? '' : 's'} recording now`}><span className="live-dot on" />{recording}</span>}
              {it.view === 'sessions' && !recording && (scope?.live.sessions.length ?? 0) > 0 && <span className="nav-count">{scope!.live.sessions.length}</span>}
            </button>
          ))}
        </nav>
      ))}
      <div className="sidebar-foot">
        <button className="source-health" onClick={() => navigate('monitors')} title="Source and monitor details">
          <span className={`health-dot ${real ? '' : 'demo'} ${source?.meta?.live?.status === 'running' ? 'pulse' : ''}`} />
          <div>
            <b>{source?.meta?.live?.synthetic ? 'Demo recording · swarm-live' : ORIGIN_LABEL[source?.meta?.origin ?? 'synthetic']}</b>
            <span>{source ? `${source.events.length} events · ${source.agents.length} agents` : 'Loading…'}</span>
            {sourceEntry?.window && <span>{sourceEntry.window.from.slice(0, 10)}</span>}
            {ws.withheld.length > 0 && <span style={{ color: 'var(--withheld)' }}>{ws.withheld.length} record withheld</span>}
          </div>
        </button>
      </div>
    </aside>
  );
}

function SourceMenu() {
  const { sources, source, selectSource, importFile } = useRecall();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    window.addEventListener('mousedown', close);
    window.addEventListener('keydown', esc);
    return () => { window.removeEventListener('mousedown', close); window.removeEventListener('keydown', esc); };
  }, [open]);
  const groups = [...new Set(sources.map((s) => s.group))];
  const cur = sources.find((s) => s.id === source?.id);
  const kind = source?.meta?.origin === 'huggingface' ? 'real' : source?.meta?.origin === 'file' ? 'file' : source?.meta?.origin === 'live' ? (source.meta.live?.synthetic ? 'demo' : 'live') : 'demo';
  return (
    <div className="menu-wrap" ref={ref}>
      <button className="source-btn" onClick={() => setOpen((o) => !o)} aria-haspopup="listbox" aria-expanded={open}>
        <IDb />
        <span className={`tag ${kind === 'real' || kind === 'live' ? 'real' : 'demo'}`}>{kind === 'real' ? 'Real' : kind === 'live' ? 'Live' : kind === 'file' ? 'File' : 'Demo'}</span>
        <span className="lbl">{cur?.label ?? source?.label ?? 'Choose data source'}</span>
        <IChevron />
      </button>
      {open && (
        <div className="menu" role="listbox">
          {groups.map((g) => (
            <div key={g}>
              <div className="menu-group">{g}</div>
              {sources.filter((s) => s.group === g).map((s) => (
                <button key={s.id} role="option" aria-selected={s.id === source?.id} className={`menu-item ${s.id === source?.id ? 'on' : ''}`}
                  onClick={() => { selectSource(s.id); setOpen(false); }}>
                  <b>{s.live && <span className={`live-dot ${s.live.status === 'running' ? 'on' : ''}`} />}{s.label}{s.counts?.findingsActive ? <span className="state-pill state-active">{s.counts.findingsActive} active</span> : null}</b>
                  <span>
                    {[s.window ? `${s.window.from.slice(0, 16).replace('T', ' ')} → ${s.window.to.slice(11, 16)} UTC` : null,
                      s.counts?.events ? `${s.counts.events} events` : null,
                      s.counts?.agents ? `${s.counts.agents} agents` : null].filter(Boolean).join(' · ')}
                  </span>
                  {s.highlight && <span>{s.highlight}</span>}
                </button>
              ))}
            </div>
          ))}
          <div className="menu-sep" />
          <button className="menu-item" onClick={() => fileRef.current?.click()}>
            <b><IUpload /> Import file…</b>
            <span>AI Village .jsonl / .jsonl.gz / .json export, or a RECALL event document</span>
          </button>
          <input ref={fileRef} type="file" accept=".json,.jsonl,.gz" hidden
            onChange={(e) => { const f = e.target.files?.[0]; if (f) { importFile(f); setOpen(false); } e.target.value = ''; }} />
        </div>
      )}
    </div>
  );
}

/** States the slice's approximation: in-range vs carried records, so no view implies more than the records support. */
function ContextHeader() {
  const { source, ws } = useRecall();
  const part = source?.meta?.part;
  if (!source) return null;
  const inRange = part?.inRange ?? source.events.length;
  return (
    <span className="context-header mono" title="Records in this part's time range, plus window facts (claims, checks, corrections, session boundaries) carried from earlier in the 4-hour window with their original sequence numbers. Timestamps are parsed as UTC without zone.">
      {part && part.count > 1 && <>part {part.index}/{part.count} · </>}
      {inRange.toLocaleString()} in-range · {(part?.carried ?? 0).toLocaleString()} carried
      {ws.withheld.length > 0 && <> · {ws.withheld.length} withheld</>}
    </span>
  );
}

export function TopBar({ crumb }: { crumb?: string }) {
  const { view, navigate, setPaletteOpen } = useRecall();
  return (
    <header className="topbar">
      <div className="crumbs">
        <button onClick={() => navigate('overview')}>RECALL</button>
        <span className="sep">›</span>
        {crumb ? <button onClick={() => navigate(view)}>{VIEW_LABEL[view]}</button> : <span className="current">{VIEW_LABEL[view]}</span>}
        {crumb && <><span className="sep">›</span><span className="current mono">{crumb}</span></>}
      </div>
      <SourceMenu />
      <ContextHeader />
      <div className="topbar-right">
        <button className="search-btn" onClick={() => setPaletteOpen(true)} aria-label="Search or run a command">
          <ISearch size={17} /><span className="kbd">⌘K</span><span>Search or run a command…</span>
        </button>
      </div>
    </header>
  );
}

interface PaletteItem { kind: string; label: string; run: () => void }

export function Palette() {
  const { paletteOpen } = useRecall();
  return paletteOpen ? <PaletteDialog /> : null;
}

function PaletteDialog() {
  const r = useRecall();
  const { paletteOpen, setPaletteOpen } = r;
  const [q, setQ] = useState('');
  const [k, setK] = useState(0);

  const items: PaletteItem[] = useMemo(() => {
    if (!paletteOpen) return [];
    const out: PaletteItem[] = [];
    (Object.keys(VIEW_LABEL) as View[]).forEach((v) => out.push({ kind: 'Go to', label: VIEW_LABEL[v], run: () => r.navigate(v) }));
    out.push({ kind: 'Action', label: r.playing ? 'Pause replay' : 'Replay from start', run: () => (r.playing ? r.togglePlay() : r.replay()) });
    const first = [...r.allFindings].sort((a, b) => a.detectedAt - b.detectedAt)[0];
    if (first) out.push({ kind: 'Action', label: 'Jump to first discrepancy', run: () => { r.seek(first.detectedAt); r.navigate('incidents', first.id); } });
    out.push({ kind: 'Action', label: 'Jump to end of log', run: () => r.seek(r.maxSeq) });
    if (r.source?.experiment) out.push({ kind: 'Action', label: `${r.experimentOn ? 'Disable' : 'Enable'} evidence visibility experiment`, run: () => r.setExperimentOn(!r.experimentOn) });
    r.sources.forEach((s) => out.push({ kind: 'Source', label: s.label, run: () => r.selectSource(s.id) }));
    r.findings.forEach((f) => out.push({ kind: 'Incident', label: `${findingLabel(f)} — ${f.title}`, run: () => r.navigate('incidents', f.id) }));
    r.ws.claims.forEach((c) => out.push({ kind: 'Claim', label: `${c.id} — ${c.text}`, run: () => r.navigate('propagation', c.id) }));
    r.ws.tasks.forEach((t) => out.push({ kind: 'Task', label: `${t.id} — ${t.title}`, run: () => r.navigate('tasks', t.id) }));
    r.source?.agents.forEach((a) => out.push({ kind: 'Agent', label: a.name, run: () => r.navigate('agents', a.id) }));
    r.ws.visible.forEach((e) => out.push({ kind: TYPE_LABEL[e.type], label: `#${e.sequence} ${r.name(e.agentId)}: ${e.text}`, run: () => r.openRecord(e.id) }));
    return out;
  }, [paletteOpen, r]);

  const filtered = useMemo(() => {
    const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
    const res = terms.length ? items.filter((it) => terms.every((t) => `${it.kind} ${it.label}`.toLowerCase().includes(t))) : items.slice(0, 14);
    return res.slice(0, 60);
  }, [items, q]);

  const choose = (it?: PaletteItem) => { if (it) { setPaletteOpen(false); it.run(); } };
  return (
    <div className="palette-scrim" onMouseDown={() => setPaletteOpen(false)}>
      <div className="palette" role="dialog" aria-label="Command palette" onMouseDown={(e) => e.stopPropagation()}>
        <div className="palette-input">
          <ISearch />
          <input autoFocus value={q} placeholder="Search incidents, claims, tasks, agents, records…"
            onChange={(e) => { setQ(e.target.value); setK(0); }}
            onKeyDown={(e) => {
              if (e.key === 'ArrowDown') { e.preventDefault(); setK((x) => Math.min(filtered.length - 1, x + 1)); }
              else if (e.key === 'ArrowUp') { e.preventDefault(); setK((x) => Math.max(0, x - 1)); }
              else if (e.key === 'Enter') { e.preventDefault(); choose(filtered[k]); }
              else if (e.key === 'Escape') setPaletteOpen(false);
            }} />
          <span className="kbd">esc</span>
        </div>
        <div className="palette-list">
          {filtered.length === 0 && <div className="empty-state"><b>No matches</b>Try a claim id, agent name, URL or word from a message.</div>}
          {filtered.map((it, i) => (
            <button key={i} className={`palette-item ${i === k ? 'kb' : ''}`} onMouseEnter={() => setK(i)} onClick={() => choose(it)}>
              <span className="kind">{it.kind}</span><span className="txt">{it.label}</span>
            </button>
          ))}
        </div>
        <div className="palette-foot"><span><span className="kbd">↑↓</span> navigate</span><span><span className="kbd">↵</span> open</span><span>Searches records visible at #{r.cursor}</span></div>
      </div>
    </div>
  );
}

/** The SwarmScope evidence id behind a RECALL record: explicit for live sources; AI Village chat ids map 1:1. */
function storeIdOf(e: { id: string; storeId?: string }): string | null {
  if (e.storeId) return e.storeId;
  const m = /^chat\/([0-9a-f-]{36})(?:#\d+)?$/.exec(e.id);
  return m ? `village:msg:${m[1]}` : null;
}

function StoreIdRow({ id }: { id: string | null }) {
  const [copied, setCopied] = useState(false);
  if (!id) return null;
  return (
    <>
      <dt>Store id</dt>
      <dd>
        <button className="link mono small" title="Copy: cite with findings_record, read with core_get (swarm MCP server)"
          onClick={async () => { setCopied(await copyText(id)); setTimeout(() => setCopied(false), 1400); }}>{id}</button>
        {copied && <span className="muted xs"> copied</span>}
      </dd>
    </>
  );
}

/** Source-record drawer: the exact record, its references, and who cites it. */
export function Drawer() {
  const { drawer, openRecord, ws, agents, name, navigate, seek, source } = useRecall();
  useEffect(() => {
    if (!drawer) return;
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') openRecord(null); };
    window.addEventListener('keydown', esc);
    return () => window.removeEventListener('keydown', esc);
  }, [drawer, openRecord]);
  if (!drawer) return null;
  const e = ws.byId.get(drawer);
  const status = ws.refStatus(drawer);
  const citedBy = e ? ws.visible.filter((x) => x.evidenceRefs.includes(e.id) ||
    (x.type === 'acknowledgement' && x.payload.acknowledges === e.id) ||
    (e.type === 'claim' && x.type === 'action' && x.payload.referencesClaims.includes(e.payload.claimId))) : [];
  return (
    <>
      <div className="drawer-scrim" onClick={() => openRecord(null)} />
      <aside className="drawer" role="dialog" aria-label="Source record">
        <div className="drawer-head">
          <IDoc />
          <h3>Source record <span className="evt-id">{drawer}</span></h3>
          <span className="spacer" />
          <button className="icon-btn" onClick={() => openRecord(null)} aria-label="Close"><IX /></button>
        </div>
        <div className="drawer-body">
          {!e ? (
            status === 'future'
              ? <div className="empty-state"><b>Not yet recorded</b>This record is after the current timeline position.</div>
              : <UnavailableRecord id={drawer} status={status} />
          ) : (
            <>
              <Record event={e} agent={agents.get(e.agentId)} cursor={ws.cursor} active refStatus={ws.refStatus} onInspect={openRecord} onJump={seek} />
              <dl className="kv">
                <dt>Type</dt><dd>{TYPE_LABEL[e.type]}</dd>
                <dt>Agent</dt><dd><button className="link" onClick={() => { openRecord(null); navigate('agents', e.agentId); }}>{name(e.agentId)}</button></dd>
                <dt>Sequence</dt><dd className="mono">#{e.sequence}</dd>
                <dt>Timestamp</dt><dd className="mono">{e.timestamp}</dd>
                {e.taskId && <><dt>Task</dt><dd><button className="link" onClick={() => { openRecord(null); navigate('tasks', e.taskId!); }}>{ws.tasks.get(e.taskId)?.title ?? e.taskId}</button></dd></>}
                <dt>Provenance</dt><dd>{e.provenance}</dd>
                {e.type === 'tool_result' && e.payload.rule && <><dt>Verdict rule</dt><dd className="mono">{e.payload.rule}</dd></>}
                {e.mentions?.length ? <><dt>Mentions</dt><dd>{e.mentions.map(name).join(', ')}</dd></> : null}
                {e.evidenceRefs.length > 0 && <><dt>Cites</dt><dd className="refs">{e.evidenceRefs.map((r) => <RefLink key={r} id={r} status={ws.refStatus(r)} onInspect={openRecord} />)}</dd></>}
                {source?.meta?.dataset && <><dt>Dataset</dt><dd>{source.meta.dataset}</dd></>}
                {e.sourceUrl && <><dt>Original</dt><dd><a href={e.sourceUrl} target="_blank" rel="noreferrer" className="link">Open in AI Village <IExternal /></a></dd></>}
                <StoreIdRow id={storeIdOf(e)} />
              </dl>
              {e.type === 'claim' && <button className="btn btn-secondary btn-sm" onClick={() => { openRecord(null); navigate('propagation', e.payload.claimId); }}>Trace claim lineage →</button>}
              {e.type === 'correction' && <button className="btn btn-secondary btn-sm" onClick={() => { openRecord(null); navigate('propagation', e.payload.supersedes); }}>See who acknowledged this correction →</button>}
              <div>
                <div className="section-title">Cited by <span className="tag">{citedBy.length}</span></div>
                {citedBy.length === 0 ? <p className="muted small">No visible record cites this one at #{ws.cursor}.</p>
                  : citedBy.map((x) => <Record key={x.id} event={x} agent={agents.get(x.agentId)} cursor={ws.cursor} refStatus={ws.refStatus} onInspect={openRecord} onJump={seek} />)}
              </div>
            </>
          )}
        </div>
      </aside>
    </>
  );
}
