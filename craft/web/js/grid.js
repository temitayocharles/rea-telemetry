// Grafana-style layout maths for the dashboard grid.
//
// The grid is 24 columns wide. A panel occupies {x, y, w, h} in grid units:
// x in [0, 24-w], y >= 0, w in [1, 24], h >= 1. All functions are pure and
// return new arrays, so the caller decides when to re-render.
//
// Behaviour mirrors Grafana (react-grid-layout): dropping a panel onto an
// occupied cell pushes the occupants down, then everything floats back up
// (vertical compaction). Horizontal compaction is deliberately not done —
// it would move panels the user intentionally placed.

export const COLS = 24;
export const ROW_HEIGHT = 30;
export const GAP = 10;

const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

export function normalizePanel(panel) {
  const g = panel.gridPos || {};
  const w = clamp(Math.round(Number(g.w) || 12), 1, COLS);
  const h = clamp(Math.round(Number(g.h) || 6), 1, 60);
  const x = clamp(Math.round(Number(g.x) || 0), 0, COLS - w);
  // A missing y means "put it at the bottom"; compact() resolves the real row.
  const y = Number.isFinite(Number(g.y)) && g.y !== null ? Math.max(0, Math.round(Number(g.y))) : Number.POSITIVE_INFINITY;
  return { ...panel, gridPos: { x, y, w, h } };
}

export function collides(a, b) {
  const p = a.gridPos, q = b.gridPos;
  if (!p || !q) return false;
  return p.x < q.x + q.w && p.x + p.w > q.x && p.y < q.y + q.h && p.y + p.h > q.y;
}

function firstCollision(list, item) {
  for (const other of list) if (other !== item && collides(other, item)) return other;
  return null;
}

function maxBottom(list) {
  return list.reduce((m, p) => Math.max(m, p.gridPos.y + p.gridPos.h), 0);
}

/** The react-grid-layout compaction step, verbatim in spirit: pull a panel up
 *  until something blocks it, then drop it below whatever it still overlaps. */
function compactItem(compareWith, item) {
  item.gridPos.y = Math.min(maxBottom(compareWith), item.gridPos.y);
  while (item.gridPos.y > 0 && !firstCollision(compareWith, item)) item.gridPos.y -= 1;
  let hit = firstCollision(compareWith, item);
  let guard = 0;
  while (hit && ++guard < 2000) {
    item.gridPos.y = hit.gridPos.y + hit.gridPos.h;
    hit = firstCollision(compareWith, item);
  }
}

/**
 * Resolve overlaps and float everything up. Panels are processed top-to-bottom,
 * left-to-right, which is the order a user reads the dashboard in.
 */
export function compact(panels) {
  const layout = panels.map((p) => ({ gridPos: { ...p.gridPos }, _ref: p }));
  const byRef = new Map(layout.map((l) => [l._ref, l]));

  // panels without a y sink to the bottom, in their existing order
  let maxY = 0;
  for (const l of layout) if (Number.isFinite(l.gridPos.y)) maxY = Math.max(maxY, l.gridPos.y + l.gridPos.h);
  for (const l of layout) if (!Number.isFinite(l.gridPos.y)) l.gridPos.y = maxY;

  const sorted = [...layout].sort((a, b) => a.gridPos.y - b.gridPos.y || a.gridPos.x - b.gridPos.x);
  const placed = [];
  for (const item of sorted) {
    compactItem(placed, item);
    placed.push(item);
  }
  return panels.map((p) => ({ ...p, gridPos: { ...byRef.get(p).gridPos } }));
}

export function normalizeLayout(panels) {
  return compact(panels.map(normalizePanel));
}

/** Total height of a layout in grid units — used to size the grid container. */
export function layoutHeight(panels) {
  return panels.reduce((max, p) => Math.max(max, (p.gridPos?.y || 0) + (p.gridPos?.h || 0)), 0);
}

/** Move one panel to (x, y), pushing others out of the way, then compact. */
export function movePanel(panels, index, x, y) {
  const next = panels.map((p) => ({ ...p, gridPos: { ...p.gridPos } }));
  const item = next[index];
  if (!item) return panels;
  item.gridPos.x = clamp(Math.round(x), 0, COLS - item.gridPos.w);
  item.gridPos.y = Math.max(0, Math.round(y));

  // others it now overlaps get pushed below it, transitively
  const seen = new Set();
  const cascade = (blocker) => {
    for (const other of next) {
      if (other === blocker || seen.has(other)) continue;
      if (collides(blocker, other)) {
        seen.add(other);
        other.gridPos.y = blocker.gridPos.y + blocker.gridPos.h;
        cascade(other);
      }
    }
  };
  cascade(item);
  return compact(next);
}

/** Resize one panel, keeping its top-left corner pinned, then compact. */
export function resizePanel(panels, index, w, h) {
  const next = panels.map((p) => ({ ...p, gridPos: { ...p.gridPos } }));
  const item = next[index];
  if (!item) return panels;
  item.gridPos.w = clamp(Math.round(w), 1, COLS - item.gridPos.x);
  item.gridPos.h = clamp(Math.round(h), 1, 60);
  const seen = new Set();
  const cascade = (blocker) => {
    for (const other of next) {
      if (other === blocker || seen.has(other)) continue;
      if (collides(blocker, other)) {
        seen.add(other);
        other.gridPos.y = blocker.gridPos.y + blocker.gridPos.h;
        cascade(other);
      }
    }
  };
  cascade(item);
  return compact(next);
}

/** Re-pack everything from scratch, keeping each panel's width and order. */
export function tidy(panels) {
  let y = 0;
  let x = 0;
  let rowHeight = 0;
  const out = panels.map((p) => {
    const w = clamp(p.gridPos?.w || 12, 1, COLS);
    const h = clamp(p.gridPos?.h || 6, 1, 60);
    if (x + w > COLS) {
      y += rowHeight;
      x = 0;
      rowHeight = 0;
    }
    const panel = { ...p, gridPos: { x, y, w, h } };
    x += w;
    rowHeight = Math.max(rowHeight, h);
    if (x >= COLS) {
      y += rowHeight;
      x = 0;
      rowHeight = 0;
    }
    return panel;
  });
  return compact(out);
}

/** Pixel height of a panel given its row span. */
export function panelHeight(panel) {
  const h = panel.gridPos?.h || 6;
  return h * ROW_HEIGHT + (h - 1) * GAP;
}
