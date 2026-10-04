// Shared helpers for the views fed by `swarm-mcp render recall` (Sessions, Explorer, Subtasks).
import { useEffect, useState } from 'react';

const DATA_BASE = `${import.meta.env.BASE_URL}data/`;
const cache = new Map<string, Promise<unknown>>();

/** Fetch a scope file once per (file, version); `version` changes when the index is regenerated. */
export function useScopeFile<T>(file: string | null | undefined, version?: string | null): { data: T | null; error: string | null; loading: boolean } {
  const [state, setState] = useState<{ key: string; data: T | null; error: string | null }>({ key: '', data: null, error: null });
  const key = file ? `${file}@${version ?? ''}` : '';
  useEffect(() => {
    if (!file) return;
    let off = false;
    let p = cache.get(key) as Promise<T> | undefined;
    if (!p) {
      p = fetch(`${DATA_BASE}${file}`, { cache: 'no-cache' }).then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status} for ${file}`);
        return r.json() as Promise<T>;
      });
      cache.set(key, p);
      p.catch(() => cache.delete(key));
    }
    p.then((data) => { if (!off) setState({ key, data, error: null }); })
      .catch((e: unknown) => { if (!off) setState({ key, data: null, error: e instanceof Error ? e.message : String(e) }); });
    return () => { off = true; };
  }, [file, key]);
  if (!file) return { data: null, error: null, loading: false };
  return state.key === key ? { data: state.data, error: state.error, loading: false } : { data: null, error: null, loading: true };
}

export const fmtN = (n: number | null | undefined) => (n == null ? '—' : n.toLocaleString('en-US'));
export const fmtK = (n: number) => (n >= 10_000 ? `${Math.round(n / 1000)}k` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));
export const isoDay = (ms: number) => new Date(ms).toISOString().slice(0, 10);
export const isoMin = (ms: number) => new Date(ms).toISOString().slice(0, 16).replace('T', ' ');
export const monthLabel = (ms: number) => new Date(ms).toLocaleDateString('en-US', { month: 'short', year: '2-digit', timeZone: 'UTC' });

/** Village day of a time: the local calendar date in the dataset's time zone, counted from day one (Day 1). */
export function villageDay(ms: number, days: { day_one: string; tz: string } | null | undefined): number | null {
  if (!days) return null;
  const local = new Intl.DateTimeFormat('en-CA', { timeZone: days.tz, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(ms));
  return Math.round((Date.parse(`${local}T00:00:00Z`) - Date.parse(`${days.day_one}T00:00:00Z`)) / 86_400_000) + 1;
}

/** Relative age of an ISO time, for live status lines. */
export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return 'never';
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86_400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86_400)} d ago`;
}

/** Re-render every `ms` (live "x s ago" labels). */
export function useNow(ms = 5000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), ms); return () => clearInterval(t); }, [ms]);
  return now;
}

/** Categorical slots shared by the store views; colour always comes with a label in the UI. */
export const SLOT_COLORS = ['#2f6a4f', '#3b5f94', '#a2482c', '#8e3b5c', '#9a5b22', '#6b4a96', '#227777', '#5a6f22'];
export const slotColor = (slot: number) => (slot >= 0 ? SLOT_COLORS[slot % SLOT_COLORS.length] : '#a2a79d');
export const LAB_COLORS: Record<string, string> = { Anthropic: '#a2482c', OpenAI: '#2f6a4f', Google: '#3b5f94', Other: '#8a8f88' };

/** Tool families for Claude Code tool calls: colour + label. */
export function toolFamily(tool: string): { key: string; label: string; color: string } {
  if (tool === 'Bash') return { key: 'shell', label: 'Shell', color: '#2f6a4f' };
  if (tool === 'Agent' || tool === 'Task') return { key: 'delegate', label: 'Delegation', color: '#7a5cb8' };
  if (['Edit', 'MultiEdit', 'Write', 'NotebookEdit'].includes(tool)) return { key: 'write', label: 'Edit / write', color: '#c9692f' };
  if (['Read', 'Grep', 'Glob', 'LS', 'NotebookRead'].includes(tool)) return { key: 'read', label: 'Read / search', color: '#3b5f94' };
  if (tool.startsWith('mcp__')) return { key: 'mcp', label: 'MCP tool', color: '#227777' };
  if (['WebFetch', 'WebSearch'].includes(tool)) return { key: 'web', label: 'Web', color: '#5a6f22' };
  return { key: 'other', label: 'Other', color: '#8a8f88' };
}

/** Copy text; returns whether it worked (clipboard can be unavailable). */
export async function copyText(text: string): Promise<boolean> {
  try { await navigator.clipboard.writeText(text); return true; } catch { return false; }
}

/** Track an element's width, so charts draw at real pixel size (rows stay aligned with HTML labels, text unstretched). */
export function useWidth<T extends HTMLElement>(fallback = 900): [(el: T | null) => void, number] {
  const [el, setEl] = useState<T | null>(null);
  const [w, setW] = useState(fallback);
  useEffect(() => {
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(200, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, [el]);
  return [setEl, w];
}
