import { lazy, Suspense, useEffect } from 'react';
import { RecallProvider } from './ui/store';
import { useRecall } from './ui/context';
import { Drawer, Palette, Sidebar, TopBar } from './ui/Shell';
const Overview = lazy(() => import('./ui/views/Overview').then((m) => ({ default: m.Overview })));
const Propagation = lazy(() => import('./ui/views/Propagation').then((m) => ({ default: m.Propagation })));
const TasksView = lazy(() => import('./ui/views/TasksView').then((m) => ({ default: m.TasksView })));
const Agents = lazy(() => import('./ui/views/Agents').then((m) => ({ default: m.Agents })));
const Incidents = lazy(() => import('./ui/views/Incidents').then((m) => ({ default: m.Incidents })));
const Monitors = lazy(() => import('./ui/views/Monitors').then((m) => ({ default: m.Monitors })));
const Evidence = lazy(() => import('./ui/views/Evidence').then((m) => ({ default: m.Evidence })));
import { Logo } from './ui/icons';

function useShortcuts() {
  const { togglePlay, seek, cursor, setPaletteOpen, paletteOpen, drawer, openRecord } = useRecall();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); setPaletteOpen(!paletteOpen); return; }
      const t = e.target as HTMLElement;
      if (paletteOpen || t.tagName === 'INPUT' && (t as HTMLInputElement).type !== 'range' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA') return;
      if (e.key === 'ArrowRight') { e.preventDefault(); seek(cursor + 1); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); seek(cursor - 1); }
      else if (e.key === ' ' && t.tagName !== 'BUTTON') { e.preventDefault(); togglePlay(); }
      else if (e.key === 'Escape' && drawer) openRecord(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [togglePlay, seek, cursor, setPaletteOpen, paletteOpen, drawer, openRecord]);
}

function Screen() {
  const { view, param, source, sources, loading, error, experimentOn, setExperimentOn, ws } = useRecall();
  useShortcuts();

  if (!source) {
    return (
      <div className="loading-screen">
        <div className="box">
          <Logo />
          {error ? <><b>Couldn't load data</b><span className="muted small" style={{ maxWidth: 420, textAlign: 'center' }}>{error}</span></>
            : <><div className="spinner" /><span>{loading ?? 'Loading…'}</span></>}
        </div>
      </div>
    );
  }

  const crumb = view === 'propagation' || view === 'incidents' || view === 'tasks' || view === 'agents' ? param : undefined;
  return (
    <div className="shell">
      <Sidebar />
      <div className="main">
        <TopBar crumb={crumb} />
        {loading && <div className="loading-bar" />}
        {error && <div className="error-banner">{error}</div>}
        {source.meta?.origin === 'synthetic' && (
          <div className="demo-banner"><span className="tag demo">Synthetic demonstration</span>Hand-written fixture for illustration. Not an AI Village incident; no deployments or publications were executed.
            {!sources.some((x) => x.group === 'AI Village · Hugging Face') && <span className="muted"> · Real AI Village slices appear in the source menu after <span className="mono">npm run fetch:ai-village</span>.</span>}
          </div>
        )}
        {experimentOn && source.experiment && (
          <div className="experiment-banner">
            <span className="exp-badge">Experiment on</span>
            <span>Withheld from analysis: <span className="mono">{ws.withheld.length ? ws.withheld.join(', ') : source.experiment.withhold.join(', ')}</span> — {source.experiment.label}.</span>
            <span className="spacer" />
            <button className="link" onClick={() => setExperimentOn(false)}>Reveal record</button>
          </div>
        )}
        <main className="page" id="main">
          <Suspense fallback={<div className="empty-state"><div className="spinner" style={{ margin: '0 auto 12px' }} />Loading view…</div>}>
          {view === 'overview' && <Overview />}
          {view === 'propagation' && <Propagation />}
          {view === 'tasks' && <TasksView />}
          {view === 'agents' && <Agents />}
          {view === 'incidents' && <Incidents />}
          {view === 'monitors' && <Monitors />}
          {view === 'evidence' && <Evidence />}
          </Suspense>
        </main>
      </div>
      <Drawer />
      <Palette />
    </div>
  );
}

export default function App() {
  return <RecallProvider><Screen /></RecallProvider>;
}
