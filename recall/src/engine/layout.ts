// Stable node positions. Computed once per data source from dependency structure
// so nodes never move while scrubbing. Layout is presentation only — reconstructed
// state at a cursor still sees only past events.

import type { RecallEvent } from '../model/types';

export const NODE_W = 248;
export const NODE_H = 214;
const GAP_X = 64;
const GAP_Y = 36;

export function computeLayout(events: RecallEvent[]): Map<string, { x: number; y: number }> {
  const order: string[] = [];
  const owner = new Map<string, string>();
  const parents = new Map<string, Set<string>>();
  for (const e of events) {
    if (e.type === 'task_created') {
      for (const t of e.payload.tasks) {
        order.push(t.taskId);
        owner.set(t.taskId, t.owner);
        parents.set(t.taskId, new Set(t.dependsOn ?? []));
      }
    } else if (e.type === 'dependency_created') {
      parents.get(e.payload.to)?.add(e.payload.from);
    }
  }
  const pos = new Map<string, { x: number; y: number }>();
  const hasEdges = [...parents.values()].some((p) => p.size);

  if (!hasEdges) {
    // Lanes per agent, columns by creation order.
    const lanes = [...new Set(order.map((t) => owner.get(t)!))];
    const col = new Map<string, number>();
    for (const t of order) {
      const lane = owner.get(t)!;
      const c = col.get(lane) ?? 0;
      col.set(lane, c + 1);
      pos.set(t, { x: c * (NODE_W + GAP_X / 2), y: lanes.indexOf(lane) * (NODE_H + GAP_Y) });
    }
    return pos;
  }

  const depth = new Map<string, number>();
  const visit = (t: string, stack: Set<string>): number => {
    if (depth.has(t)) return depth.get(t)!;
    if (stack.has(t)) return 0;
    stack.add(t);
    const d = Math.max(-1, ...[...(parents.get(t) ?? [])].filter((p) => parents.has(p)).map((p) => visit(p, stack))) + 1;
    stack.delete(t);
    depth.set(t, d);
    return d;
  };
  order.forEach((t) => visit(t, new Set()));

  const layers = new Map<number, string[]>();
  for (const t of order) {
    const d = depth.get(t)!;
    layers.set(d, [...(layers.get(d) ?? []), t]);
  }
  const tallest = Math.max(...[...layers.values()].map((l) => l.length));
  for (const [d, ids] of layers) {
    const offset = ((tallest - ids.length) * (NODE_H + GAP_Y)) / 2;
    ids.forEach((t, i) => pos.set(t, { x: d * (NODE_W + GAP_X), y: offset + i * (NODE_H + GAP_Y) }));
  }
  return pos;
}
