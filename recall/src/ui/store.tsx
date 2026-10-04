import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import type { DataSource } from '../model/types';
import { Ctx, VIEWS, type HfIndex, type RecallState, type SourceEntry, type View } from './context';
import { loadSyntheticRelease, parseRecallDocument } from '../adapters/syntheticAdapter';
import { adaptAiVillage, readRows } from '../adapters/aiVillageAdapter';
import { reconstruct, type AnalysisInput } from '../engine/reconstruct';
import { runMonitors } from '../engine/monitors';

const synthetic = loadSyntheticRelease();
const SYNTHETIC_ENTRY: SourceEntry = {
  id: synthetic.id, label: 'Synthetic release demo', group: 'Demo', origin: 'synthetic', description: synthetic.description,
  counts: { events: synthetic.events.length }, load: async () => synthetic,
};
const DATA_BASE = `${import.meta.env.BASE_URL}data/`;
const PLAY_MS = 1400;
const NO_WITHHELD: ReadonlySet<string> = new Set();

// ---------- routing: #/view/param?src=…&t=… ----------
interface Route { view: View; param?: string; src?: string; t?: number }

function parseHash(): Route {
  const [path, query = ''] = window.location.hash.replace(/^#\/?/, '').split('?');
  const [v, ...rest] = path.split('/');
  const q = new URLSearchParams(query);
  return {
    view: (VIEWS as string[]).includes(v) ? (v as View) : 'overview',
    param: rest.length ? decodeURIComponent(rest.join('/')) : undefined,
    src: q.get('src') ?? undefined,
    t: q.get('t') ? Number(q.get('t')) : undefined,
  };
}

function buildHash(r: Route) {
  const q = new URLSearchParams();
  if (r.src) q.set('src', r.src);
  if (r.t !== undefined) q.set('t', String(r.t));
  return `#/${r.view}${r.param ? `/${encodeURIComponent(r.param)}` : ''}?${q}`;
}

function readLocal<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(key);
    return v ? (JSON.parse(v) as T) : fallback;
  } catch {
    return fallback;
  }
}
function writeLocal(key: string, v: unknown) {
  try { localStorage.setItem(key, JSON.stringify(v)); } catch { /* storage unavailable */ }
}

const EMPTY_WS = reconstruct({ events: [], withheld: NO_WITHHELD }, 0);

export function RecallProvider({ children }: { children: ReactNode }) {
  const [initial] = useState<Route>(parseHash);
  const [route, setRoute] = useState<Route>(initial);
  const [sources, setSources] = useState<SourceEntry[]>([SYNTHETIC_ENTRY]);
  const [source, setSource] = useState<DataSource | null>(null);
  const [loading, setLoading] = useState<string | null>('Loading data sources…');
  const [error, setError] = useState<string | null>(null);
  const [cursor, setCursor] = useState(initial.t ?? 1);
  const [playing, setPlaying] = useState(false);
  const [experimentOn, setExperimentOn] = useState(false);
  const [drawer, setDrawer] = useState<string | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [reviewed, setReviewed] = useState<Set<string>>(() => new Set(readLocal<string[]>('recall.reviewed', [])));
  const pendingT = useRef<number | undefined>(initial.t);

  const loadEntry = useCallback(async (entry: SourceEntry, keepCursor = false) => {
    setLoading(`Loading ${entry.label}…`);
    setError(null);
    try {
      const s = await entry.load();
      setSource(s);
      setPlaying(false);
      setExperimentOn(false);
      setDrawer(null);
      const first = s.events[0]?.sequence ?? 0;
      const last = s.events[s.events.length - 1]?.sequence ?? 0;
      const t = keepCursor && pendingT.current !== undefined ? pendingT.current : last;
      pendingT.current = undefined;
      setCursor(Math.max(first, Math.min(last, t)));
      setRoute((r) => ({ ...r, src: s.id }));
    } catch (e) {
      setError(`Could not load ${entry.label}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoading(null);
    }
  }, []);

  // Discover Hugging Face slices produced by `npm run fetch:ai-village`, then load the requested/default source.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      let hf: SourceEntry[] = [];
      try {
        const res = await fetch(`${DATA_BASE}index.json`, { cache: 'no-cache' });
        if (res.ok) {
          const idx = (await res.json()) as HfIndex;
          hf = idx.sources.map((s) => ({
            id: s.id, label: s.parts && s.parts > 1 ? `Part ${s.part}/${s.parts} · ${s.window?.from.slice(11, 16) ?? ''}–${s.window?.to.slice(11, 16) ?? ''}` : s.label,
            group: s.parentLabel ?? 'AI Village · Hugging Face', origin: 'huggingface' as const, description: s.goal,
            part: s.parts && s.parts > 1 ? { index: s.part ?? 1, count: s.parts, parent: s.parent ?? s.id } : undefined,
            window: s.window, counts: s.counts, highlight: s.highlight,
            load: async () => {
              const r = await fetch(`${DATA_BASE}${s.file}`);
              if (!r.ok) throw new Error(`HTTP ${r.status}`);
              return parseRecallDocument(await r.json());
            },
          }));
        }
      } catch { /* no HF data generated yet */ }
      if (cancelled) return;
      const all = [...hf, SYNTHETIC_ENTRY];
      setSources(all);
      const wanted = all.find((s) => s.id === initial.src) ?? all[0];
      await loadEntry(wanted, true);
    })();
    return () => { cancelled = true; };
  }, [loadEntry, initial]);

  // Keep the hash in sync (replaceState: scrubbing shouldn't flood history).
  useEffect(() => {
    if (!source) return;
    const h = buildHash({ ...route, src: source.id, t: cursor });
    if (h !== window.location.hash) window.history.replaceState(null, '', h);
  }, [route, cursor, source]);

  useEffect(() => {
    const onPop = () => {
      const r = parseHash();
      setRoute(r);
      if (r.t !== undefined) setCursor(r.t);
    };
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

  const navigate = useCallback((view: View, param?: string) => {
    setRoute((r) => {
      const next = { ...r, view, param };
      window.history.pushState(null, '', buildHash({ ...next, t: undefined }));
      return next;
    });
    setPaletteOpen(false);
  }, []);

  const events = useMemo(() => source?.events ?? [], [source]);
  const minSeq = events[0]?.sequence ?? 0;
  const maxSeq = events[events.length - 1]?.sequence ?? 0;
  // Window parts keep original sequence numbers, so sequences can have gaps: snap to a record that exists,
  // in the direction of travel.
  const seqs = useMemo(() => events.map((e) => e.sequence), [events]);
  const snap = useCallback((target: number, from: number) => {
    if (!seqs.length) return 0;
    const t = Math.max(minSeq, Math.min(maxSeq, target));
    if (t >= from) return seqs.find((q) => q >= t) ?? maxSeq;
    for (let k = seqs.length - 1; k >= 0; k--) if (seqs[k] <= t) return seqs[k];
    return minSeq;
  }, [seqs, minSeq, maxSeq]);
  const seek = useCallback((seq: number) => { setPlaying(false); setCursor((c) => snap(seq, c)); }, [snap]);

  const isPlaying = playing && cursor < maxSeq;
  useEffect(() => {
    if (!isPlaying) return;
    const step = Math.max(1, Math.round(events.length / 120));
    const t = setTimeout(() => setCursor((c) => {
      const k = seqs.findIndex((q) => q > c);
      return k < 0 ? maxSeq : seqs[Math.min(seqs.length - 1, k + step - 1)];
    }), events.length > 60 ? 160 : PLAY_MS);
    return () => clearTimeout(t);
  }, [isPlaying, cursor, maxSeq, events.length, seqs]);
  const togglePlay = useCallback(() => {
    if (!isPlaying && cursor >= maxSeq) setCursor(minSeq);
    setPlaying(!isPlaying);
  }, [isPlaying, cursor, maxSeq, minSeq]);
  const replay = useCallback(() => { setCursor(minSeq); setPlaying(true); }, [minSeq]);

  const experimentActive = experimentOn && !!source?.experiment;
  const input: AnalysisInput = useMemo(() => ({
    events,
    withheld: experimentActive ? new Set(source!.experiment!.withhold) : NO_WITHHELD,
    agents: source?.agents ?? [],
    referencesSeen: source?.referencesSeen,
  }), [events, experimentActive, source]);

  const agents = useMemo(() => new Map((source?.agents ?? []).map((a) => [a.id, a])), [source]);
  const name = useCallback((id: string) => agents.get(id)?.name ?? id, [agents]);
  const ws = useMemo(() => (source ? reconstruct(input, cursor) : EMPTY_WS), [source, input, cursor]);
  const findings = useMemo(() => runMonitors(ws), [ws]);
  const allFindings = useMemo(() => (source ? runMonitors(reconstruct(input, maxSeq)) : []), [source, input, maxSeq]);

  const selectSource = useCallback((id: string) => {
    const e = sources.find((s) => s.id === id);
    if (e) loadEntry(e);
  }, [sources, loadEntry]);

  const importFile = useCallback(async (file: File) => {
    try {
      const rows = await readRows(file);
      const doc = rows.length === 1 && rows[0].__recallDocument;
      const s = doc ? parseRecallDocument(doc) : adaptAiVillage(rows, file.name);
      if (!s.events.length) throw new Error('No usable records found in this file.');
      const entry: SourceEntry = { id: s.id, label: s.label, group: 'Imported', origin: 'file', description: s.description, counts: { events: s.events.length }, load: async () => s };
      setSources((prev) => [...prev.filter((p) => p.id !== s.id), entry]);
      await loadEntry(entry);
    } catch (e) {
      setError(`Import failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  }, [loadEntry]);

  const toggleReviewed = useCallback((id: string) => {
    setReviewed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      writeLocal('recall.reviewed', [...next]);
      return next;
    });
  }, []);

  const value: RecallState = {
    sources, source, sourceEntry: sources.find((s) => s.id === source?.id) ?? null, loading, error, selectSource, importFile,
    view: route.view, param: route.param, navigate,
    cursor, minSeq, maxSeq, seek, playing: isPlaying, togglePlay, replay,
    experimentOn: experimentActive, setExperimentOn, input, ws, findings, allFindings, agents, name,
    drawer, openRecord: setDrawer, reviewed, toggleReviewed, paletteOpen, setPaletteOpen,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
