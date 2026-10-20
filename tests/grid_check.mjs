// Layout assertions for craft/web/js/grid.js. Run by tests/test_grid.py via node.
// Prints one line per failure and exits non-zero; silent success otherwise.
import assert from "node:assert/strict";
import { COLS, compact, collides, movePanel, normalizeLayout, normalizePanel, resizePanel, tidy } from "../craft/web/js/grid.js";

let failures = 0;
const test = (name, fn) => {
  try {
    fn();
  } catch (err) {
    failures++;
    console.log(`FAIL ${name}: ${err.message.split("\n")[0]}`);
  }
};

const panel = (title, x, y, w, h) => ({ title, gridPos: { x, y, w, h } });
const overlap = (a, b) => {
  const p = a.gridPos, q = b.gridPos;
  return p.x < q.x + q.w && p.x + p.w > q.x && p.y < q.y + q.h && p.y + p.h > q.y;
};
const noOverlaps = (layout, name = "layout") => {
  for (let i = 0; i < layout.length; i++)
    for (let j = i + 1; j < layout.length; j++)
      assert.ok(!overlap(layout[i], layout[j]), `${name}: ${layout[i].title} overlaps ${layout[j].title}`);
};
const withinBounds = (layout) => {
  for (const p of layout) {
    assert.ok(p.gridPos.x >= 0, `${p.title} x < 0`);
    assert.ok(p.gridPos.x + p.gridPos.w <= COLS, `${p.title} overflows the ${COLS} column grid`);
    assert.ok(p.gridPos.w >= 1 && p.gridPos.h >= 1, `${p.title} has a zero span`);
    assert.ok(Number.isInteger(p.gridPos.y) && p.gridPos.y >= 0, `${p.title} has a negative or fractional y`);
  }
};
const byTitle = (layout) => Object.fromEntries(layout.map((p) => [p.title, p.gridPos]));

test("collides detects overlap and ignores adjacency", () => {
  assert.equal(collides(panel("a", 0, 0, 12, 6), panel("b", 11, 5, 12, 6)), true);
  assert.equal(collides(panel("a", 0, 0, 12, 6), panel("b", 12, 0, 12, 6)), false, "side by side");
  assert.equal(collides(panel("a", 0, 0, 12, 6), panel("b", 0, 6, 12, 6)), false, "stacked");
});

test("normalizePanel clamps wild input", () => {
  const a = normalizePanel({ title: "a", gridPos: { x: 30, y: -4, w: 40, h: 0 } });
  assert.equal(a.gridPos.w, COLS);
  assert.equal(a.gridPos.x, 0);
  assert.equal(a.gridPos.h, 6, "h falls back to the default");
  assert.equal(a.gridPos.y, 0);
  const b = normalizePanel({ title: "b" });
  assert.deepEqual([b.gridPos.w, b.gridPos.h], [12, 6]);
});

test("existing grafana-style layout is left alone", () => {
  const layout = [
    panel("a", 0, 0, 8, 6), panel("b", 8, 0, 8, 6), panel("c", 16, 0, 8, 6),
    panel("d", 0, 6, 12, 4), panel("e", 12, 6, 12, 4),
  ];
  const out = normalizeLayout(layout);
  assert.deepEqual(byTitle(out), byTitle(layout));
});

test("nothing floats up into a gap it cannot fill", () => {
  // a wide panel below a narrow one must not slide under it
  const out = normalizeLayout([panel("top", 0, 0, 8, 6), panel("wide", 0, 6, 24, 4)]);
  assert.equal(byTitle(out).wide.y, 6);
  noOverlaps(out);
});

test("panels left dangling in the middle float to the top", () => {
  const out = normalizeLayout([panel("a", 0, 0, 24, 4), panel("b", 0, 40, 24, 4)]);
  assert.equal(byTitle(out).b.y, 4);
});

test("a missing y is placed at the bottom, not on top of everything", () => {
  const out = normalizeLayout([panel("a", 0, 0, 12, 6), { title: "new", gridPos: { x: 0, w: 12, h: 6 } }]);
  assert.equal(byTitle(out).new.y, 6);
  noOverlaps(out);
});

test("overlapping input is separated", () => {
  const out = compact([panel("a", 0, 0, 12, 6), panel("b", 3, 2, 12, 6), panel("c", 0, 0, 6, 3)]);
  noOverlaps(out);
  withinBounds(out);
});

test("dragging a panel onto another pushes it down", () => {
  const layout = [panel("a", 0, 0, 12, 6), panel("b", 12, 0, 12, 6), panel("c", 0, 6, 12, 6)];
  const out = movePanel(layout, 1, 0, 0); // b onto a
  noOverlaps(out, "after move");
  withinBounds(out);
  assert.equal(byTitle(out).b.y, 0, "the dragged panel keeps the row it was dropped on");
  assert.ok(byTitle(out).a.y >= 6, "the displaced panel moved below it");
});

test("dragging clamps inside the grid", () => {
  const layout = [panel("a", 0, 0, 12, 6), panel("b", 12, 0, 12, 6)];
  const right = movePanel(layout, 0, 99, 0);
  assert.equal(byTitle(right).a.x, COLS - 12);
  const left = movePanel(right, 0, -99, 0);
  assert.equal(byTitle(left).a.x, 0);
  withinBounds(left);
});

test("dragging far below everything leaves a compact column", () => {
  const layout = [panel("a", 0, 0, 12, 6), panel("b", 12, 0, 12, 6), panel("c", 0, 6, 24, 6)];
  const out = movePanel(layout, 2, 0, 30);
  noOverlaps(out);
  withinBounds(out);
  assert.ok(byTitle(out).c.y <= 6, "gravity pulls it back up once the space is free");
});

test("resizing a panel pushes its neighbours down", () => {
  const layout = [panel("a", 0, 0, 6, 4), panel("b", 6, 0, 6, 4)];
  const out = resizePanel(layout, 0, 18, 4); // a now overlaps b
  noOverlaps(out);
  withinBounds(out);
  assert.equal(byTitle(out).a.w, 18);
  assert.ok(byTitle(out).b.y >= 4);
});

test("resizing cannot overflow the grid or collapse the panel", () => {
  const layout = [panel("a", 18, 0, 6, 4)];
  const wide = resizePanel(layout, 0, 40, 4);
  assert.equal(byTitle(wide).a.w, 6, "width is capped by the column it starts in");
  const tiny = resizePanel(layout, 0, 0, 0);
  assert.equal(byTitle(tiny).a.w, 1);
  assert.equal(byTitle(tiny).a.h, 1);
});

test("resizing taller pushes the row below", () => {
  const layout = [panel("a", 0, 0, 24, 4), panel("b", 0, 4, 24, 4)];
  const out = resizePanel(layout, 0, 24, 10);
  noOverlaps(out);
  assert.equal(byTitle(out).b.y, 10);
});

test("duplicate indices and empty layouts are harmless", () => {
  assert.deepEqual(movePanel([], 0, 3, 3), []);
  assert.deepEqual(resizePanel([], 0, 3, 3), []);
  const one = [panel("a", 0, 5, 12, 6)];
  const out = movePanel(one, 4, 0, 0);
  assert.equal(out.length, 1);
});

test("tidy re-packs in order without gaps", () => {
  const layout = [
    panel("a", 18, 30, 6, 4), panel("b", 0, 0, 6, 4), panel("c", 0, 0, 6, 4),
    panel("d", 3, 3, 12, 4),
  ];
  const out = tidy(layout);
  noOverlaps(out, "tidy");
  withinBounds(out);
  const g = byTitle(out);
  assert.deepEqual([g.a.x, g.a.y], [0, 0]);
  assert.deepEqual([g.b.x, g.b.y], [6, 0]);
  assert.deepEqual([g.c.x, g.c.y], [12, 0]);
  // 12 + 18 would overflow 24 columns, so the wide panel wraps to the next row
  assert.deepEqual([g.d.x, g.d.y], [0, 4]);
});

test("real seeded dashboard survives a round trip", () => {
  const seeded = [
    panel("Request rate", 0, 0, 8, 6), panel("Error ratio", 8, 0, 8, 6), panel("Latency", 16, 0, 8, 6),
    panel("Availability", 0, 6, 6, 4), panel("p99 now", 6, 6, 6, 4), panel("In flight", 12, 6, 6, 4),
    panel("CPU", 18, 6, 6, 4), panel("Slowest", 0, 10, 12, 6), panel("Volume", 12, 10, 12, 6),
  ];
  const out = normalizeLayout(seeded);
  assert.deepEqual(byTitle(out), byTitle(seeded), "an already-tidy dashboard must not move");
});

test("layout is stable: normalizing twice changes nothing", () => {
  const messy = [panel("a", 20, 12, 8, 6), panel("b", 0, 3, 14, 5), panel("c", 4, 9, 9, 3), panel("d", 0, 0, 24, 2)];
  const once = normalizeLayout(messy);
  const twice = normalizeLayout(once);
  assert.deepEqual(byTitle(twice), byTitle(once));
});

if (failures) {
  console.log(`${failures} layout assertion(s) failed`);
  process.exit(1);
}
