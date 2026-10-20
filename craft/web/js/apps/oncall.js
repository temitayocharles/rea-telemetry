// On-call — PagerDuty-style rotations, escalation ladder and manual paging.
import { h, clear, panel, badge, table, empty, toast, ago, dur, modal, formField } from "../ui.js";

const ROTATION_NOTE = {
  weekly: "hands off Monday 09:00",
  daily: "hands off daily at the handoff hour",
  follow_the_sun: "8-hour blocks, passes west to east",
};

export async function render(root, ctx) {
  ctx.setTitle("On-call");
  const cardsHost = h("div", { class: "grid three", style: { marginBottom: "10px" } });
  const ladderHost = h("div", {});
  const schedulesHost = h("div", {});
  const pagesHost = h("div", {});

  const addBtn = h("button", { class: "btn primary sm", onClick: () => openSchedule(ctx, null, refresh) }, "New schedule");
  root.appendChild(h("div", { class: "row", style: { marginBottom: "10px", alignItems: "center" } },
    h("div", { class: "hint" }, "Who is carrying the pager right now, what happens when they don't answer, and every page sent from this instance."),
    h("span", { class: "spacer" }), addBtn));
  root.appendChild(cardsHost);
  root.appendChild(h("div", { class: "grid two", style: { marginBottom: "10px" } }, panel("Escalation", ladderHost), panel("Recent pages", pagesHost)));
  root.appendChild(panel("Rotations", schedulesHost, { pad: false, meta: "rotation and handoff are computed from wall-clock time" }));

  async function refresh() {
    let data;
    try {
      [data] = await Promise.all([ctx.api.onCall(), loadPages()]);
    } catch (err) {
      clear(cardsHost);
      cardsHost.appendChild(empty("Could not load rotations", err.message));
      return;
    }
    drawCards(data.on_call || []);
    drawLadder(data.on_call || []);
    await Promise.all([loadSchedules(), loadPages()]);
  }

  function drawCards(schedules) {
    clear(cardsHost);
    if (!schedules.length) {
      cardsHost.appendChild(empty("No rotations", "Create one to start tracking who is on call.", addBtn.cloneNode(true)));
      return;
    }
    for (const s of schedules) {
      const handoffIn = Math.max(0, (s.next_handoff || 0) - Math.floor(Date.now() / 1000));
      cardsHost.appendChild(h("div", { class: "panel panel-pad" },
        h("div", { class: "row", style: { alignItems: "center" } },
          h("span", { class: "badge ok" }, "on call"),
          h("span", { class: "spacer" }),
          h("span", { class: "hint mono" }, s.rotation)),
        h("div", { style: { fontSize: "22px", fontWeight: 600, margin: "10px 0 2px", letterSpacing: "-0.02em" } }, s.on_call),
        h("div", { class: "hint" }, s.schedule),
        h("div", { class: "row", style: { marginTop: "12px", alignItems: "center" } },
          h("span", { class: "hint" }, `hands off in ${dur(handoffIn)}`),
          h("span", { class: "spacer" }),
          h("button", { class: "btn sm", onClick: () => openPage(ctx, s, refresh) }, "Page"))));
    }
  }

  function drawLadder(schedules) {
    clear(ladderHost);
    const level = (n, label, people) => h("div", { class: "row", style: { gap: "10px", padding: "9px 0", borderTop: n === 1 ? "none" : "1px solid #1e232b", alignItems: "center" } },
      h("span", { class: "badge", style: { minWidth: "54px", justifyContent: "center" } }, `T${n}`),
      h("div", { class: "flex1" }, h("div", { style: { fontWeight: 550, fontSize: "12.5px" } }, label),
        h("div", { class: "hint" }, people.length ? people.join(", ") : "nobody scheduled")));
    const primary = schedules.find((s) => /primary/.test(s.schedule)) || schedules[0];
    const secondary = schedules.filter((s) => s !== primary).slice(0, 1)[0];
    ladderHost.appendChild(h("div", {},
      level(1, "Page the primary on-call", primary ? [primary.on_call] : []),
      level(2, "Escalate after 5 minutes unanswered", secondary ? [secondary.on_call] : []),
      level(3, "Escalate to the incident commander", ["Marta Silva"]),
      h("div", { class: "hint", style: { marginTop: "10px" } },
        "Pages are recorded in the delivery log; no external pager is contacted unless you configure a webhook receiver in Settings.")));
  }

  async function loadSchedules() {
    clear(schedulesHost);
    let data;
    try { data = await ctx.api.schedules(); } catch (err) { schedulesHost.appendChild(empty("Could not load schedules", err.message)); return; }
    const rows = data.schedules || [];
    if (!rows.length) { schedulesHost.appendChild(empty("No schedules yet")); return; }
    schedulesHost.appendChild(table([
      { label: "Schedule", render: (r) => h("div", {}, h("div", { style: { fontWeight: 550 } }, r.name), h("div", { class: "hint" }, ROTATION_NOTE[r.rotation] || r.rotation)) },
      { label: "Rotation", render: (r) => h("span", { class: "mono" }, r.rotation) },
      { label: "Members", render: (r) => h("div", { class: "chips" }, (r.members || []).map((m) => h("span", { class: "chip" }, m))) },
      { label: "Handoff", align: "right", render: (r) => h("span", { class: "mono" }, `${String(r.handoff_hour).padStart(2, "0")}:00`) },
      { label: "", render: (r) => h("button", { class: "btn ghost sm", onClick: () => openSchedule(ctx, r, refresh) }, "edit") },
    ], rows, { class: "data" }));
  }

  async function loadPages() {
    clear(pagesHost);
    let notes = [];
    try {
      const res = await ctx.api.notifications();
      notes = (res.notifications || []).filter((n) => n.kind === "page").slice(0, 8);
    } catch { /* ignore */ }
    if (!notes.length) { pagesHost.appendChild(empty("No pages sent yet", "Use Page on a rotation, or acknowledge an alert to generate traffic.")); return; }
    pagesHost.appendChild(table([
      { label: "When", render: (r) => h("span", { class: "hint" }, ago(r.ts)) },
      { label: "Who", render: (r) => h("span", { class: "mono" }, (r.receiver || "").replace("page:", "")) },
      { label: "Reason", render: (r) => h("div", {}, h("div", {}, r.title), h("div", { class: "hint" }, r.body || "")) },
      { label: "Status", render: (r) => badge(r.status || "delivered", "ok") },
    ], notes));
  }

  await refresh();
  ctx.setCleanup(() => {});
}

function openPage(ctx, schedule, done) {
  const titleIn = h("input", { class: "input", value: "Manual page" });
  const bodyIn = h("textarea", { class: "textarea", rows: 3 }, "Please acknowledge in #incidents.");
  modal({
    title: `Page ${schedule.on_call}`,
    body: h("div", {},
      formField("Reason", titleIn, `${schedule.schedule} · on call now`),
      formField("Message", bodyIn)),
    footer: h("div", { class: "row", style: { justifyContent: "flex-end" } },
      h("button", { class: "btn primary", onClick: async () => {
        try {
          await ctx.api.pageOncall({ person: schedule.on_call, title: titleIn.value.trim() || "Manual page", message: bodyIn.value });
          document.querySelector(".modal-backdrop")?.remove();
          toast(`Paged ${schedule.on_call}`);
          await done();
        } catch { /* handled */ }
      } }, "Send page")),
  });
}

function openSchedule(ctx, existing, done) {
  const nameIn = h("input", { class: "input", value: existing?.name || "", placeholder: "platform-primary" });
  const rotationIn = h("select", { class: "select" },
    ["weekly", "daily", "follow_the_sun"].map((r) => h("option", { value: r, selected: (existing?.rotation || "weekly") === r }, r)));
  const membersIn = h("input", { class: "input", value: (existing?.members || []).join(", "), placeholder: "Ade Okonkwo, Priya Raman" });
  const hourIn = h("input", { class: "input", type: "number", min: 0, max: 23, value: existing?.handoff_hour ?? 9 });
  modal({
    title: existing ? `Edit ${existing.name}` : "New schedule",
    body: h("div", {},
      formField("Name", nameIn),
      formField("Rotation", rotationIn, ROTATION_NOTE[existing?.rotation || "weekly"]),
      formField("Members", membersIn, "Comma separated. Order defines the rotation."),
      formField("Handoff hour", hourIn, "Local hour when the pager moves (weekly and daily rotations).")),
    footer: h("div", { class: "row", style: { justifyContent: "flex-end" } },
      h("button", { class: "btn primary", onClick: async () => {
        const members = membersIn.value.split(",").map((m) => m.trim()).filter(Boolean);
        if (!nameIn.value.trim() || !members.length) return toast("Name and at least one member are required", "err");
        try {
          await ctx.api.saveSchedule({ name: nameIn.value.trim(), rotation: rotationIn.value, members, handoff_hour: Number(hourIn.value) });
          document.querySelector(".modal-backdrop")?.remove();
          toast("Schedule saved");
          await done();
        } catch { /* handled */ }
      } }, "Save")),
  });
}
