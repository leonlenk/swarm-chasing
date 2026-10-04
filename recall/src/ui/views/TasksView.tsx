import { useMemo, useState } from 'react';
import { ReactFlowProvider } from '@xyflow/react';
import { useRecall } from '../context';
import { computeLayout } from '../../engine/layout';
import { dependencyReach } from '../../engine/reconstruct';
import { difference } from '../../engine/difference';
import { GraphView } from '../GraphView';
import { Timeline } from '../Timeline';
import { Inspector } from '../Inspector';
import { TaskLanes } from '../TaskLanes';
import type { Highlight, ViewMode } from '../TaskNode';

export function TasksView() {
  const r = useRecall();
  const { ws, param, navigate, source, agents, findings, experimentOn, openRecord, seek } = r;
  const [mode, setMode] = useState<ViewMode>('difference');
  const structured = useMemo(() => (source?.events ?? []).some((e) => e.type === 'dependency_created' || (e.type === 'task_created' && e.payload.tasks.some((t) => t.dependsOn?.length))), [source]);
  const [layout, setLayout] = useState<'graph' | 'lanes' | null>(null);
  const shown = layout ?? (structured ? 'graph' : 'lanes');
  const positions = useMemo(() => computeLayout(source?.events ?? []), [source]);
  const selected = param && ws.tasks.has(param) ? param : null;
  const highlights = useMemo(() => {
    const h = new Map<string, Highlight>();
    if (selected) dependencyReach(ws, selected).forEach((t) => h.set(t, 'reach'));
    return h;
  }, [ws, selected]);
  const tally = { conflict: 0, insufficient: 0, agree: 0 };
  ws.tasks.forEach((t) => tally[difference(t)]++);
  const now = ws.visible[ws.visible.length - 1];

  return (
    <div className="page-wide">
      <div className="hero">
        <div>
          <h1>Where the work stands.</h1>
          <p className="sub">What agents reported, next to what the records establish. {shown === 'graph' ? 'Arrows run from prerequisites to dependent tasks.' : 'Each lane is an agent; each bar is a task (a computer-use session for AI Village data).'}</p>
          <div className="hero-chips">
            <span className="chip red"><span className="dot" />{tally.conflict} accounts conflict</span>
            <span className="chip amber"><span className="dot" />{tally.insufficient} insufficient evidence</span>
            <span className="chip green"><span className="dot" />{tally.agree} accounts agree</span>
          </div>
        </div>
        <div className="hero-actions">
          <div className="seg" aria-label="Layout">
            <button className={shown === 'lanes' ? 'on' : ''} onClick={() => setLayout('lanes')}>Lanes</button>
            <button className={shown === 'graph' ? 'on' : ''} onClick={() => setLayout('graph')} title={structured ? '' : 'No dependencies recorded in this source'}>Graph</button>
          </div>
          <div className="seg" role="tablist" aria-label="Graph view">
            {(['reported', 'evidence', 'difference'] as const).map((m) => (
              <button key={m} role="tab" aria-selected={mode === m} className={mode === m ? 'on' : ''} onClick={() => setMode(m)}>
                {m === 'reported' ? 'Reported' : m === 'evidence' ? 'Evidence' : 'Difference'}
              </button>
            ))}
          </div>
        </div>
      </div>
      <div className="split">
        <section className="card">
          <div className="flow-canvas" style={{ height: 600, marginTop: 12 }}>
            {shown === 'lanes' ? (
              <TaskLanes mode={mode} selected={selected} onSelect={(id) => navigate('tasks', id)} />
            ) : (
              <ReactFlowProvider key={source?.id}>
                <GraphView ws={ws} positions={positions} agents={agents} mode={mode} selectedTask={selected} highlights={highlights}
                  focusTask={now?.taskId ?? null} onSelectTask={(id) => navigate('tasks', id ?? undefined)} />
              </ReactFlowProvider>
            )}
          </div>
          <Timeline />
        </section>
        <aside className="card" style={{ overflow: 'hidden' }}>
          <Inspector
            selection={selected ? { kind: 'task', id: selected } : { kind: 'none' }}
            ws={ws} findings={findings} agents={agents} source={source!} experimentOn={experimentOn}
            onSelectTask={(id) => navigate('tasks', id)}
            onSelectFinding={(f) => navigate('incidents', f.id)}
            onSelectClaim={(id) => navigate('propagation', id)}
            onSelectEvent={(id) => openRecord(id)}
            onJump={seek}
          />
        </aside>
      </div>
    </div>
  );
}
