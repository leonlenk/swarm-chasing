import { useMemo } from 'react';
import { useRecall } from './context';
import { eventTone, TYPE_LABEL } from './format';
import { IFlag, INext, IPause, IPlay, IPrev } from './icons';

const hm = (iso: string) => new Date(iso).toISOString().slice(11, 16);
const hms = (iso: string) => new Date(iso).toISOString().slice(11, 19);

/** Global timeline: every view scrubs the same cursor. */
export function Timeline({ compact = false }: { compact?: boolean }) {
  const { source, cursor, minSeq, maxSeq, seek, playing, togglePlay, input, allFindings, name, navigate } = useRecall();
  const events = useMemo(() => source?.events ?? [], [source]);
  const pct = (seq: number) => (maxSeq === minSeq ? 0 : ((seq - minSeq) / (maxSeq - minSeq)) * 100);
  const current = events.find((e) => e.sequence === cursor);
  const hidden = !!current && input.withheld.has(current.id);
  const dense = events.length > 70;
  const flags = useMemo(() => new Set(allFindings.map((f) => f.detectedAt)), [allFindings]);
  const first = useMemo(() => [...allFindings].sort((a, b) => a.detectedAt - b.detectedAt)[0], [allFindings]);

  // Sparse logs show every event; dense logs show notable events only (claims, results, corrections, actions).
  const ticks = useMemo(() => dense
    ? events.filter((e) => e.type !== 'message' && e.type !== 'status_updated' && e.type !== 'task_created')
    : events, [events, dense]);
  const labels = useMemo(() => {
    if (!events.length) return [];
    const n = compact ? 4 : 7;
    return Array.from({ length: n }, (_, i) => events[Math.round((i / (n - 1)) * (events.length - 1))]);
  }, [events, compact]);

  if (!events.length) return null;
  return (
    <>
      <div className="timeline">
        <button className="play" onClick={togglePlay} aria-label={playing ? 'Pause replay' : 'Play replay'} title="Play / pause (space)">
          {playing ? <IPause /> : <IPlay />}
        </button>
        <div className="tl-time">
          {current ? hms(current.timestamp) : '—'}
          <small>#{cursor} of {maxSeq} · UTC</small>
        </div>
        <button className="tl-step" onClick={() => seek(cursor - 1)} disabled={cursor <= minSeq} aria-label="Previous event" title="Previous event (←)"><IPrev /></button>
        <button className="tl-step" onClick={() => seek(cursor + 1)} disabled={cursor >= maxSeq} aria-label="Next event" title="Next event (→)"><INext /></button>
        <div className="tl-track">
          <div className="tl-line" />
          <div className="tl-fill" style={{ width: `${pct(cursor)}%` }} />
          {ticks.map((e) => {
            const withheld = input.withheld.has(e.id);
            return (
              <button
                key={e.id}
                className={`tl-tick ${dense ? 'dense' : ''} ${withheld ? 'withheld' : `tone-${eventTone(e)}`} ${e.sequence > cursor ? 'future' : ''}`}
                style={{ left: `${pct(e.sequence)}%` }}
                title={withheld ? `#${e.sequence} — withheld from analysis` : `#${e.sequence} · ${hm(e.timestamp)} · ${TYPE_LABEL[e.type]} — ${name(e.agentId)}`}
                onClick={() => seek(e.sequence)}
                tabIndex={-1}
              >
                {flags.has(e.sequence) && e.sequence <= cursor && <span className="tl-flag" />}
              </button>
            );
          })}
          <div className="tl-cursor" style={{ left: `${pct(cursor)}%` }} />
          <div className="tl-labels">
            {labels.map((e, i) => <span key={i} style={{ left: `${pct(e.sequence)}%` }}>{hm(e.timestamp)}</span>)}
          </div>
          <input type="range" className="tl-range" min={minSeq} max={maxSeq} step={1} value={cursor}
            onChange={(ev) => seek(Number(ev.target.value))} aria-label="Timeline position" />
        </div>
        <span className="tl-end">{hm(events[events.length - 1].timestamp)}</span>
        {!compact && (
          <button className="jump-btn" disabled={!first} onClick={() => { if (first) { seek(first.detectedAt); navigate('incidents', first.id); } }}
            title={first ? `Move to #${first.detectedAt}, where the first incident is detected` : 'No incidents in this source'}>
            <IFlag /> First discrepancy
          </button>
        )}
      </div>
      <div className="now-caption">
        {hidden ? (
          <><span className="tag withheld">Withheld</span><span className="what">This record is withheld from the analysis by the evidence visibility experiment.</span></>
        ) : current ? (
          <><span className="evt-id">#{current.sequence}</span><b style={{ fontWeight: 500, flex: 'none' }}>{name(current.agentId)}</b><span className="muted" style={{ flex: 'none' }}>{TYPE_LABEL[current.type]}</span><span className="what">{current.text}</span></>
        ) : null}
      </div>
    </>
  );
}
