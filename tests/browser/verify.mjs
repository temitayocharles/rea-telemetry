// Real-browser verification for the TelemetryCraft console.
//
// The Python suite proves the API and the layout maths; the jsdom smoke test
// proves the views render into a DOM. Neither of them can prove the thing a
// person actually sees: that charts paint, that nothing overflows at phone
// width, that dragging a panel does what the finger does. That needs a real
// engine, which is why this file is separate and why it is not part of
// `make test`.
//
//   python3 -m craft --port 8787 &
//   cd tests/browser && npm install && npx playwright install chromium
//   BASE=http://127.0.0.1:8787 node verify.mjs
//
// Options (env):
//   BASE        server origin                       (default http://127.0.0.1:8787)
//   AUTH_TOKEN  also verify the sign-in gate against a server started with
//               --auth-token <AUTH_TOKEN>           (section 7 is skipped without it)
//   CHROME_PATH use a specific chrome binary instead of Playwright's download
//   OUT         screenshot directory                (default ./artifacts)
//
// Exit code 0 = every check passed. Screenshots are written for every view and
// for each failure, because the point is to look at them.

import { chromium } from "playwright";
import { mkdir, writeFile, readdir, stat } from "node:fs/promises";
import path from "node:path";

const BASE = process.env.BASE || "http://127.0.0.1:8787";
const AUTH_TOKEN = process.env.AUTH_TOKEN || "";
const OUT = process.env.OUT || path.join(import.meta.dirname, "artifacts");

const results = [];
const check = (ok, label, detail = "") => {
  results.push({ ok, label, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${detail ? "  — " + detail : ""}`);
};

const VIEWPORTS = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "laptop", width: 1280, height: 800 },
  { name: "tablet", width: 834, height: 1112 },
  { name: "phone", width: 390, height: 844 },
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The console renders asynchronously; "stable" means the same text length and
// the same element count four polls in a row.
async function waitStable(page, selector = "#view", tries = 25) {
  let last = -1;
  let streak = 0;
  let snapshot = "";
  for (let i = 0; i < tries; i += 1) {
    const now = await page.$eval(selector, (el) => el.innerText.length + ":" + el.querySelectorAll("*").length).catch(() => "0:0");
    if (now === snapshot) streak += 1;
    else {
      streak = 0;
      snapshot = now;
    }
    last = Number(now.split(":")[0]);
    if (streak >= 3) break;
    await sleep(150);
  }
  return last;
}

async function shot(page, name) {
  const file = path.join(OUT, `${name}.png`);
  await page.screenshot({ path: file, fullPage: false });
  return file;
}

function attachErrorCollectors(page) {
  const errors = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push("console: " + msg.text());
  });
  page.on("pageerror", (err) => errors.push("pageerror: " + err.message));
  page.on("requestfailed", (req) => {
    // favicon noise is not a failure
    if (!/favicon/.test(req.url())) errors.push("requestfailed: " + req.url());
  });
  page.on("response", (res) => {
    if (res.status() >= 400 && !/favicon/.test(res.url())) errors.push(`http ${res.status()}: ${res.url()}`);
  });
  return errors;
}

async function main() {
  await mkdir(OUT, { recursive: true });
  const browser = await chromium.launch(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {});

  // ---- 1. boot -----------------------------------------------------------
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await context.newPage();
  const errors = attachErrorCollectors(page);

  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  const bootChars = await waitStable(page);
  check(bootChars > 200, "console boots and renders the overview", `${bootChars} chars`);
  check(await page.locator(".rail-link").count() === 15, "15 apps in the rail", `${await page.locator(".rail-link").count()} links`);
  check(await page.locator(".brand-word").count() === 1, "brand renders exactly once");
  await shot(page, "00-overview");

  const apps = await page.evaluate(async (base) => {
    const res = await fetch(base + "/api/meta");
    return (await res.json()).apps;
  }, BASE);

  // ---- 2. every view -----------------------------------------------------
  for (const app of apps) {
    errors.length = 0;
    await page.goto(BASE + "/" + app.path.replace(/^\/*/, "").replace(/^#/, ""), { waitUntil: "domcontentloaded" });
    if (!page.url().includes("#")) await page.evaluate((h) => { location.hash = h; }, "#" + app.path.replace(/^\/?#?\/?/, ""));
    const chars = await waitStable(page);
    const body = await page.$eval("#view", (el) => el.innerText).catch(() => "");
    const failed = /failed to load|Retry/i.test(body);
    check(chars > 120 && !failed, `view ${app.id} renders`, `${chars} chars${failed ? " — error state" : ""}`);
    if (errors.length) check(false, `view ${app.id} has no console/HTTP errors`, errors.slice(0, 2).join(" | "));
    await shot(page, `view-${String(app.id).padStart(2, "0")}`);
  }

  // ---- 3. charts actually paint -----------------------------------------
  await page.goto(BASE + "/#/", { waitUntil: "domcontentloaded" });
  await waitStable(page);
  const charts = await page.evaluate(() => {
    const svgs = [...document.querySelectorAll("svg")];
    return svgs.map((svg) => {
      const box = svg.getBoundingClientRect();
      const paths = [...svg.querySelectorAll("path, rect, circle, polyline")];
      const lengths = paths.map((p) => (typeof p.getTotalLength === "function" && p.tagName === "path" ? p.getTotalLength() : 0));
      return { w: Math.round(box.width), h: Math.round(box.height), marks: paths.length, drawn: lengths.filter((l) => l > 0).length };
    });
  });
  const realCharts = charts.filter((c) => c.w > 100 && c.h > 40 && c.marks > 0);
  check(realCharts.length >= 1, "overview draws at least one chart with marks", `${realCharts.length} of ${charts.length} svgs`);
  check(realCharts.every((c) => c.drawn > 0), "chart paths have a non-zero length", JSON.stringify(realCharts.slice(0, 3)));

  await page.goto(BASE + "/#/dashboards", { waitUntil: "domcontentloaded" });
  await waitStable(page);
  const dashCards = await page.locator(".dash-card, .panel").count();
  check(dashCards > 0, "dashboards list renders", `${dashCards} panels/cards`);

  // ---- 4. layout at four widths -----------------------------------------
  for (const vp of VIEWPORTS) {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await page.goto(BASE + "/#/", { waitUntil: "domcontentloaded" });
    await waitStable(page);
    await sleep(400);
    const layout = await page.evaluate(() => ({
      scrollW: document.documentElement.scrollWidth,
      innerW: window.innerWidth,
      rail: document.querySelector(".rail")?.getBoundingClientRect().width || 0,
      // any element wider than the viewport is a horizontal-scroll bug
      offenders: [...document.querySelectorAll("body *")]
        .filter((el) => {
          const r = el.getBoundingClientRect();
          return r.width > 0 && r.right > window.innerWidth + 2 && getComputedStyle(el).position !== "fixed";
        })
        .slice(0, 3)
        .map((el) => `${el.tagName}.${el.className}`.slice(0, 60)),
    }));
    check(layout.scrollW <= layout.innerW + 1, `no horizontal overflow at ${vp.name} (${vp.width}px)`, `scrollW=${layout.scrollW} innerW=${layout.innerW} ${layout.offenders.join(", ")}`);
    if (vp.width < 860) check(layout.rail <= 60, `rail collapses at ${vp.name}`, `${Math.round(layout.rail)}px`);
    await shot(page, `width-${vp.name}`);
  }
  await page.setViewportSize({ width: 1280, height: 800 });

  // ---- 5. command palette ------------------------------------------------
  await page.goto(BASE + "/#/", { waitUntil: "domcontentloaded" });
  await waitStable(page);
  await page.keyboard.press("Control+k");
  await page.waitForSelector(".palette input, #palette input, [role=dialog] input", { timeout: 3000 }).catch(() => {});
  const paletteOpen = await page.locator("input[placeholder]").count();
  if (paletteOpen) {
    await page.keyboard.type("checkout");
    await sleep(500);
    const hits = await page.locator("[role=dialog] a, .palette-item, .palette li").count();
    check(hits > 0, "palette returns results for a search term", `${hits} items`);
  } else {
    check(false, "command palette opens with Ctrl+K");
  }
  await page.keyboard.press("Escape");

  // ---- 6. drag a panel, save, reload ------------------------------------
  await page.goto(BASE + "/#/dashboards", { waitUntil: "domcontentloaded" });
  await waitStable(page);
  const uid = await page.evaluate(async (base) => {
    const res = await fetch(base + "/api/dashboards");
    const body = await res.json();
    const list = body.dashboards || body;
    return list[0]?.uid;
  }, BASE);
  check(Boolean(uid), "a dashboard exists to drag", String(uid));

  if (uid) {
    await page.goto(BASE + `/#/dashboards/${uid}?edit=1`, { waitUntil: "domcontentloaded" });
    await waitStable(page);
    await sleep(600);
    const before = await page.evaluate(async (base) => {
      const res = await fetch(base + "/api/dashboards");
      const body = await res.json();
      const list = body.dashboards || body;
      return list[0].panels.map((p) => p.gridPos || p.pos || {});
    }, BASE);

    const handle = page.locator(".dp-head.grab").first();
    check(await handle.count() > 0, "edit mode exposes drag handles");
    if (await handle.count()) {
      const box = await handle.boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.down();
      for (let i = 1; i <= 10; i += 1) {
        await page.mouse.move(box.x + box.width / 2 + i * 30, box.y + box.height / 2 + 4);
        await sleep(30);
      }
      await page.mouse.up();
      await sleep(500);
      await shot(page, "drag-after-move");

      const save = page.getByRole("button", { name: "Save" }).first();
      if (await save.count()) {
        await save.click();
        await sleep(800);
      }
      const after = await page.evaluate(async (base) => {
        const res = await fetch(base + "/api/dashboards");
        const body = await res.json();
        const list = body.dashboards || body;
        return list[0].panels.map((p) => p.gridPos || p.pos || {});
      }, BASE);
      check(JSON.stringify(before) !== JSON.stringify(after), "dragging moves a panel and saving persists it", JSON.stringify(after[0] || {}));

      await page.reload({ waitUntil: "domcontentloaded" });
      await waitStable(page);
      await sleep(800);
      const reloaded = await page.evaluate(async (base) => {
        const res = await fetch(base + "/api/dashboards");
        const body = await res.json();
        const list = body.dashboards || body;
        return list[0].panels.map((p) => p.gridPos || p.pos || {});
      }, BASE);
      check(JSON.stringify(reloaded) === JSON.stringify(after), "the layout survives a reload");
      await shot(page, "drag-after-reload");
    }
  }

  // ---- 7. the sign-in gate (only against an auth-enabled server) ---------
  if (AUTH_TOKEN) {
    const fresh = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const login = await fresh.newPage();
    const loginErrors = attachErrorCollectors(login);
    await login.goto(BASE + "/", { waitUntil: "domcontentloaded" });
    await login.waitForSelector(".auth-overlay", { timeout: 8000 }).catch(() => {});
    check(await login.locator(".auth-overlay").count() === 1, "sign-in gate appears");
    check(await login.locator(".rail-link").count() === 0, "no navigation is built before sign-in");
    await shot(login, "auth-gate");

    await login.fill(".auth-overlay .input", "definitely-wrong");
    await login.click(".auth-overlay .btn.primary");
    await sleep(900);
    check(await login.locator(".auth-overlay").count() === 1, "a wrong token keeps the gate up");
    check((await login.evaluate(() => localStorage.getItem("craft.credential"))) === null, "a wrong token stores nothing");
    await shot(login, "auth-rejected");

    await login.fill(".auth-overlay .input", AUTH_TOKEN);
    await login.click(".auth-overlay .btn.primary");
    await sleep(2500);
    check(await login.locator(".auth-overlay").count() === 0, "the right token opens the console");
    check(await login.locator(".rail-link").count() === 15, "all 15 apps mount after sign-in");
    check((await login.evaluate(() => localStorage.getItem("craft.credential"))) === `Bearer ${AUTH_TOKEN}`, "the credential is kept for reloads");
    await shot(login, "auth-signed-in");

    await login.reload({ waitUntil: "domcontentloaded" });
    await sleep(2000);
    check(await login.locator(".auth-overlay").count() === 0, "a reload stays signed in");
    check(loginErrors.filter((e) => !/401/.test(e)).length === 0, "no unexpected errors during sign-in", loginErrors.slice(0, 2).join(" | "));
    await fresh.close();
  } else {
    console.log("SKIP  sign-in gate (set AUTH_TOKEN and start the server with --auth-token)");
  }

  await browser.close();

  // ---- report -----------------------------------------------------------
  const failed = results.filter((r) => !r.ok);
  const summary = [
    `# Browser verification — ${new Date().toISOString()}`,
    ``,
    `Target: ${BASE}`,
    `Checks: ${results.length}, passed: ${results.length - failed.length}, failed: ${failed.length}`,
    ``,
    ...results.map((r) => `${r.ok ? "- [x]" : "- [ ]"} ${r.label}${r.detail ? ` — \`${r.detail}\`` : ""}`),
    ``,
    `Screenshots: ${(await readdir(OUT)).length} files in ${OUT}`,
  ].join("\n");
  await writeFile(path.join(OUT, "report.md"), summary + "\n");

  const sizes = await Promise.all(
    (await readdir(OUT)).filter((f) => f.endsWith(".png")).map(async (f) => ({ f, bytes: (await stat(path.join(OUT, f))).size })),
  );
  const tiny = sizes.filter((s) => s.bytes < 4000);
  if (tiny.length) console.log(`\nnote: ${tiny.length} screenshot(s) under 4 KB — look at them, they may be blank: ${tiny.map((t) => t.f).join(", ")}`);

  console.log(`\n${results.length - failed.length}/${results.length} checks passed. Report: ${path.join(OUT, "report.md")}`);
  process.exit(failed.length ? 1 : 0);
}

main().catch((err) => {
  console.error("harness crashed:", err);
  process.exit(2);
});
