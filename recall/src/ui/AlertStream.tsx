// Live alert stream: while the cursor moves forward (replay or scrubbing), every newly fired Open or
// Needs-evidence finding raises an alert with its first remediation step. Moving the cursor back, or switching
// source, re-baselines silently, so alerts only ever announce what the newly visible records produced.
import { useEffect, useRef, useState } from 'react';
import { useRecall } from './context';
import { bucketOf, remediationFor } from '../engine/triage';
import { findingLabel, subjectLabel } from './labels';
import { IAlert } from './icons';

interface Alert { id: string; findingId: string; bucket: 'open' | 'needs'; label: string; subject: string; step?: string; at: number; n?: number }

export function AlertStream() {
  const { findings, ws, source, navigate } = useRecall();
  const seen = useRef<{ src?: string; cursor: number; ids: Set<string> }>({ cursor: -1, ids: new Set() });
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [muted, setMuted] = useState(false);

  useEffect(() => {
    const s = seen.current;
    const ids = new Set(findings.map((f) => f.id));
    const forward = s.src === source?.id && ws.cursor > s.cursor;
    if (forward && !muted) {
      const fresh: Alert[] = [];
      for (const f of findings) {
        if (s.ids.has(f.id)) continue;
        const b = bucketOf(f, ws);
        if (b !== 'open' && b !== 'needs') continue;
        fresh.push({ id: `${f.id}@${ws.cursor}`, findingId: f.id, bucket: b, label: findingLabel(f), subject: subjectLabel(ws.claims.get(f.claimId)?.subject?.artifact) || f.title,
          step: remediationFor(f, ws)?.steps[0], at: f.detectedAt });
      }
      // Alerts are transitions between cursor positions (what the newly visible records produced), so they are
      // derived from the previous position kept in a ref, not from render state.
      // Contradicted findings alert one by one; unchecked claims roll up into one counter alert.
      const open = fresh.filter((x) => x.bucket === 'open').sort((a, b) => b.at - a.at);
      const needs = fresh.filter((x) => x.bucket === 'needs');
      // oxlint-disable-next-line react/set-state-in-effect
      if (open.length || needs.length) setAlerts((prev) => {
        const roll = prev.find((x) => x.id === 'needs-roll');
        const rest = prev.filter((x) => x.id !== 'needs-roll');
        const n = (roll?.n ?? 0) + needs.length;
        const rolled: Alert[] = n ? [{ id: 'needs-roll', findingId: needs[0]?.findingId ?? roll!.findingId, bucket: 'needs', label: `${n} new unchecked claim${n > 1 ? 's' : ''}`,
          subject: needs[0]?.subject ?? roll!.subject, step: 'Verify before relying on them (Needs evidence tab).', at: needs[0]?.at ?? roll!.at, n }] : [];
        return [...open, ...rest, ...rolled].slice(0, 4);
      });
    // oxlint-disable-next-line react/set-state-in-effect
    } else if (s.src !== source?.id || ws.cursor < s.cursor) setAlerts([]); // moved back or switched source: re-baseline
    seen.current = { src: source?.id, cursor: ws.cursor, ids };
  }, [findings, ws, source, muted]);

  useEffect(() => {
    if (!alerts.length) return;
    const t = setTimeout(() => setAlerts((prev) => prev.slice(0, -1)), 9000);
    return () => clearTimeout(t);
  }, [alerts]);

  if (!alerts.length) return null;
  return (
    <div className="alert-stream" role="status" aria-live="polite">
      <div className="alert-head"><b>Live alerts</b><span className="spacer" /><button className="link small" onClick={() => { setMuted(true); setAlerts([]); }}>Mute</button></div>
      {alerts.map((a) => (
        <div key={a.id} className={`alert-item ${a.bucket}`}>
          <IAlert size={18} className={`inc-ico ${a.bucket === 'open' ? 'active' : 'insufficient'}`} />
          <div className="alert-body">
            <b>{a.bucket === 'open' ? 'Contradicted' : 'Unchecked'} · {a.label}</b>
            <span className="muted small">{a.n ? `latest: ${a.subject}` : a.subject} · #{a.at}</span>
            {a.step && <span className="small">Next: {a.step}</span>}
            <div className="row" style={{ gap: 10 }}>
              <button className="link small" onClick={() => navigate('incidents', a.findingId)}>Investigate</button>
              <button className="link small" onClick={() => setAlerts((prev) => prev.filter((x) => x.id !== a.id))}>Dismiss</button>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
