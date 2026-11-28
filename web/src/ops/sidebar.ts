/** The left sidebar: every active flight with its ATC requirement, and the detail card of the selected one. */
import { h } from "../shared/dom.ts";
import { icon } from "../shared/icon.ts";
import { cutText, showFullText } from "./fulltext.ts";
import { ChevronLeft } from "lucide";
import { fmtClock, fmtDuration, fmtFt, fmtKt, fmtPct } from "../shared/format.ts";
import { shortenName } from "../pilot/geo.ts";
import { SOURCE_LABEL, type Atc, type OpsRoute, type OverviewResponse, type Reroute, type RerouteEvent, type TelemetryRow } from "./types.ts";
import { conflictTitle, cls, type Actions, type Filter, type Ops } from "./panels.ts";
import { ago, aiMark, call, etaWindow, nodeName } from "./util.ts";

/** Pill text and tone for a route's ATC state. Red only for a clearance that is still missing. */
export function atcLabel(a: Atc): { text: string; tone: "ok" | "warn" | "danger" } {
  const who = a.initials ? `, ${a.initials}` : "";
  if (a.requirement === "none") return { text: "No ATC", tone: "ok" };
  const done = a.status === "done";
  if (a.requirement === "clearance") return done ? { text: `Ops marked cleared${who}`, tone: "ok" } : { text: "Clearance needed", tone: "danger" };
  if (a.requirement === "contact_approach") return done ? { text: `Ops marked contacted${who}`, tone: "ok" } : { text: "Contact approach", tone: "warn" };
  return done ? { text: `Ops marked contacted${who}`, tone: "ok" } : { text: "Contact tower", tone: "warn" };
}

export function atcPill(a: Atc): HTMLElement {
  const { text, tone } = atcLabel(a);
  return h("span", { class: `atc-pill ${tone}`, "data-atc": a.requirement, "data-status": a.status, title: a.airspaces.join(", ") || undefined }, h("i", {}), text);
}

const short = (r: OpsRoute, i: number) => shortenName(r.path_names[i] ?? "");

/** R-contract §6.1: a flight needs action when ATC still has a requirement to mark, an AI reroute is waiting for its
 *  answer (proposed or approved), or the flight is off route. Medical alone is a label, not an action. */
export function needsAction(o: Ops, r: OpsRoute): boolean {
  if (r.atc.status === "pending" || r.status === "off_route") return true;
  return o.reroutes.some((rr) => rr.flight_id === r.flight_id && (rr.status === "proposed" || rr.status === "approved") && !o.collapsedReroutes.has(rr.id));
}

/** Needs action first, then the soonest arrival. */
export function sortRoutes(o: Ops, rows: OpsRoute[]): OpsRoute[] {
  return [...rows].sort((a, b) => Number(needsAction(o, b)) - Number(needsAction(o, a)) || (a.eta_p50 ?? Infinity) - (b.eta_p50 ?? Infinity) || call(a.aircraft_id).localeCompare(call(b.aircraft_id), undefined, { numeric: true }));
}

/** Open AI reroute cards: not dismissed, not yet collapsed into the records. Contract §9: medical first, then the
 *  earliest expiry, then the largest gain. The sidebar shows the top three; the Reroutes tab shows all. */
export function visibleReroutes(o: Ops): Reroute[] {
  const medical = (rr: Reroute) => rr.trigger === "medical" || !!o.routes.find((r) => r.flight_id === rr.flight_id)?.medical;
  return o.reroutes
    .filter((rr) => rr.status !== "dismissed" && !o.collapsedReroutes.has(rr.id))
    .sort((a, b) => Number(medical(b)) - Number(medical(a)) || Date.parse(a.expires_at) - Date.parse(b.expires_at) || b.gain_s - a.gain_s);
}
export const SIDEBAR_CARDS = 3;

const EVENT_LABEL: Record<RerouteEvent["kind"], string> = {
  proposed: "Proposed by AeroLink AI", approved: "Sent to pilot: confirm by radio", dismissed: "Dismissed",
  acknowledged: "Accepted by pilot", unable: "Declined by pilot", expired: "Expired",
};

/** A small radio glyph for the "confirm by radio" card (stroke icon, same style as the corner buttons). */
function radioIcon(): SVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const el = document.createElementNS(ns, "svg");
  el.setAttribute("viewBox", "0 0 24 24"); el.setAttribute("width", "18"); el.setAttribute("height", "18");
  el.setAttribute("fill", "none"); el.setAttribute("stroke", "currentColor"); el.setAttribute("stroke-width", "2"); el.setAttribute("stroke-linecap", "round"); el.setAttribute("aria-hidden", "true");
  for (const d of ["M4 11a8 8 0 0 1 16 0", "M7 11v6H5v-6z", "M19 11v6h-2v-6z", "M12 13v7"]) {
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", d);
    el.append(p);
  }
  return el;
}

/** "Reroute via Haines City, then Kissimmee, 4 min faster": the middle nodes the proposal adds, and the gain from the engine. */
function viaText(o: Ops, rr: Reroute): string {
  const cur = new Set(rr.current.path);
  const mid = rr.proposed.path.slice(1, -1).filter((n) => !cur.has(n)).map((n) => shortenName(nodeName(o.airspace, n)));
  const via = mid.length ? `Reroute via ${mid.slice(0, 3).join(", then ")}` : "Reroute";
  return rr.gain_s >= 30 ? `${via}, ${Math.round(rr.gain_s / 60)} min faster` : via;
}

const isSim = (rr: Reroute): boolean => rr.simulated === true || rr.source === "sim";

/** Contract A: "Airspace requirement changes: No ATC → Contact tower", only when the swap changes it. */
const REQ_LABEL: Record<string, string> = { none: "No ATC", contact_tower: "Contact tower", contact_approach: "Contact approach", clearance: "Clearance needed" };
function requirementLine(rr: Reroute): HTMLElement[] {
  const c = rr.requirement_change;
  if (!c) return [];
  return [h("p", { class: "rr-req small", id: `rr-req-${rr.id}` }, `Airspace requirement changes from ${REQ_LABEL[c.from] ?? c.from} to ${REQ_LABEL[c.to] ?? c.to}.`)];
}

/** The live line under "Reroute sent": sent time, then what the pilot answered (or expired). */
function statusLine(rr: Reroute): HTMLElement {
  const at = rr.responded_at ?? rr.approved_at;
  const pilot = isSim(rr) ? "simulated pilot" : "pilot"; // contract B: a simulated answer is never shown as a real pilot's
  if (rr.status === "acknowledged") return h("p", { class: "rr-status ok", id: `rr-status-${rr.id}` }, `Accepted by ${pilot} ${fmtClock(at ?? "")}`);
  if (rr.status === "unable") return h("p", { class: "rr-status warn", id: `rr-status-${rr.id}` }, `Declined by ${pilot}`);
  if (rr.status === "expired") return h("p", { class: "rr-status muted", id: `rr-status-${rr.id}` }, "Expired");
  const by = rr.approved_by ? `, approved by ${rr.approved_by}` : "";
  return h("p", { class: "rr-status muted", id: `rr-status-${rr.id}` }, `Sent ${fmtClock(rr.approved_at ?? "")}${by}`);
}

/** One proposal card: "✦ AI" proposal with Approve / Dismiss, then the status of the approved reroute. Never a modal. */
/** `full`: the flight detail shows the whole reason and via line; the list keeps each to one line (R-contract §10.2). */
export function rerouteCard(o: Ops, act: Actions, rr: Reroute, full = false): HTMLElement {
  const who = call(rr.aircraft_id);
  const busy = o.rerouteBusy.has(rr.id);
  const card = h("section", { class: cls("rr-card", rr.status), "data-reroute": rr.id, "data-flight": rr.flight_id, "aria-label": `AI reroute for ${who}` });
  if (rr.status === "proposed") {
    const sim = rr.source === "sim";
    /** Cut-off name, reason or route on a card: the whole card text in one small panel. */
    const openFull = () => showFullText(`${who} to ${nodeName(o.airspace, rr.destination)}`, [rr.reason, viaText(o, rr)]);
    card.append(
      h("div", { class: "rr-title" }, aiMark(), cutText("rr-name", `${who} to ${shortenName(nodeName(o.airspace, rr.destination))}`, openFull, `${who} to ${nodeName(o.airspace, rr.destination)}`)),
      h("p", { class: cls("rr-reason", full && "full"), title: rr.reason }, cutText("rr-reason-text", rr.reason, full ? null : openFull)),
      h("p", { class: cls("rr-via", full && "full"), title: viaText(o, rr) }, cutText("rr-via-text", viaText(o, rr), full ? null : openFull)),
      ...requirementLine(rr),
      ...(sim ? [h("p", { class: "rr-sim muted small" }, "Simulated pilot")] : []),
      h("div", { class: "rr-actions" },
        h("button", { type: "button", class: "btn primary", "data-focus-key": `approve:${rr.id}`, "data-rr-approve": rr.id, disabled: busy, onclick: () => act.approveReroute(rr.id) }, "Approve reroute"),
        h("button", { type: "button", class: "btn", "data-focus-key": `dismiss:${rr.id}`, "data-rr-dismiss": rr.id, disabled: busy, onclick: () => act.dismissReroute(rr.id) }, "Dismiss")),
      ...(o.rerouteError[rr.id] ? [h("p", { class: "err small", role: "alert" }, o.rerouteError[rr.id])] : []));
  } else {
    card.append(
      h("div", { class: "rr-title" }, radioIcon(), h("span", { class: "rr-sent" }, `Reroute sent to ${who}. Confirm by radio.`)),
      ...requirementLine(rr),
      ...(isSim(rr) ? [h("p", { class: "rr-sim muted small" }, "Simulated pilot")] : []),
      statusLine(rr));
  }
  return card;
}

/** A record line; a simulated pilot's answer reads "Accepted by simulated pilot" and is not credited to a unit id. */
function recordLabel(rr: Reroute, ev: RerouteEvent): string {
  if (isSim(rr) && (ev.kind === "acknowledged" || ev.kind === "unable")) return `${ev.kind === "acknowledged" ? "Accepted" : "Declined"} by simulated pilot`;
  return `${EVENT_LABEL[ev.kind] ?? ev.kind}${ev.by ? `, ${ev.by}` : ""}`;
}

/** The records of every reroute of one flight (its events with times and who did what), shown in the flight's detail. */
function rerouteRecords(o: Ops, flightId: string): HTMLElement[] {
  return o.reroutes.filter((x) => x.flight_id === flightId).map((rr) => h("div", { class: "rr-record", "data-record": rr.id },
    h("div", { class: "small" }, `Reroute ${rr.status}`),
    h("ol", { class: "hist" }, ...rr.events.map((ev) => h("li", {}, h("span", { class: "muted small" }, `${fmtClock(ev.at)} `), recordLabel(rr, ev))))));
}

export function visibleRoutes(o: Ops): OpsRoute[] {
  if (o.filter === "reroutes") return []; // that tab lists the proposals, not the flights
  return sortRoutes(o, o.routes.filter((r) => o.filter === "all" || (o.filter === "needs" ? needsAction(o, r) : r.medical)));
}

/** The three bays of the flight list (CP1 layout): Needs action, Airborne (active or off route), Planned. */
export type Bay = "needs" | "airborne" | "planned";
export const BAY_LABEL: Record<Bay, string> = { needs: "Needs action", airborne: "Airborne", planned: "Planned" };
export function bayOf(o: Ops, r: OpsRoute): Bay {
  if (needsAction(o, r)) return "needs";
  return r.status === "planned" ? "planned" : "airborne";
}

/** The AI overview (R-contract §1, §10.1): one card at the top of the left panel. The header toggles it to one line;
 *  at most three bullets show, "N more" expands the rest in place. Written from the server's facts. */
export function overviewCard(o: Ops, act: Actions, ov: OverviewResponse): HTMLElement {
  const head = h("button", { type: "button", class: "ov-head", id: "ov-toggle", "aria-expanded": String(!o.overviewCollapsed), "aria-controls": "ov-body", onclick: () => act.toggleOverview() },
    h("span", {}, aiMark(), ` overview, next ${ov.horizon_min} min`), h("span", { class: "ov-age", id: "ov-age", "data-generated": ov.generated_at }, overviewAge(ov)));
  const card = h("section", { class: cls("ov-card", o.overviewCollapsed && "collapsed"), id: "ai-overview", "aria-label": "AI overview" }, head);
  if (o.overviewCollapsed) return card;
  const shown = o.overviewAllBullets ? ov.bullets : ov.bullets.slice(0, 3);
  const more = ov.bullets.length - shown.length;
  const toggle = more > 0
    ? h("button", { type: "button", class: "more-row ov-more", id: "ov-more", onclick: () => act.toggleOverviewMore() }, `${more} more`)
    : o.overviewAllBullets && ov.bullets.length > 3 ? h("button", { type: "button", class: "more-row ov-more", id: "ov-more", onclick: () => act.toggleOverviewMore() }, "Show fewer") : null;
  card.append(h("div", { class: "ov-body", id: "ov-body" },
    h("ul", { class: "ov-list", id: "ov-list" }, ...shown.map((b) => h("li", {}, b))),
    toggle,
    h("p", { class: "ov-note" }, "Written from the forecast. Numbers are checked against it; wording is not. Verify before acting.")));
  return card;
}
/** "8 s ago" for the card's age line; never negative when the server's clock runs ahead. */
export const overviewAge = (ov: OverviewResponse, now = Date.now()): string => ago(Math.max(0, (now - Date.parse(ov.generated_at)) / 1000));

const FILTERS: [Filter, string][] = [["all", "All"], ["needs", "Needs action"], ["medical", "Medical"], ["reroutes", "Reroutes"]];

export function flightsPanel(o: Ops, act: Actions) {
  const body = h("div", { class: "fl-body" });
  const el = h("aside", { class: "sidebar", id: "flights-panel", "aria-label": "Active flights" }, body);
  let shown: string | null | undefined;

  function renderList(): void {
    const need = o.routes.filter((r) => needsAction(o, r)).length;
    const cards = visibleReroutes(o);
    const reroutesTab = o.filter === "reroutes";
    // contract §9: the top three proposals are pinned; the rest are one tap away, in the Reroutes tab
    const pinned = reroutesTab ? cards : cards.slice(0, SIDEBAR_CARDS);
    const more = cards.length - pinned.length;
    // the AI overview leads the panel (CP1 plan order): overview, reroute cards, then the flight list with its filters
    // R-contract §12.1: no placeholder. Ruling 5: the overview is list-only, so it steps aside while a flight is selected
    if (o.overview && o.overview.bullets.length && !o.selected) body.append(overviewCard(o, act, o.overview));
    if (pinned.length) body.append(h("div", { class: "rr-stack", id: "reroute-cards" }, ...pinned.map((rr) => rerouteCard(o, act, rr))));
    if (more > 0) body.append(h("button", { type: "button", class: "more-row", id: "rr-more", onclick: () => { o.filter = "reroutes"; render(); act.select(null); } }, `${more} more reroute${more === 1 ? "" : "s"}`));
    body.append(
      h("div", { class: "sb-head" },
        h("h2", {}, "Flights"),
        h("span", { class: cls("needs-badge", need === 0 && "zero"), id: "needs-badge", title: "Flights where ATC still has something to do" }, need ? `${need} need action` : "All clear")),
      h("div", { class: "chips", role: "group", "aria-label": "Filter flights" },
        ...FILTERS.map(([id, label]) => h("button", { type: "button", class: cls("chip", o.filter === id && "on"), "data-filter": id, "aria-pressed": String(o.filter === id), onclick: () => { o.filter = id; render(); act.select(null); } },
          id === "reroutes" ? `${label} ${cards.length}` : label))));
    if (reroutesTab) {
      if (!cards.length) body.append(h("p", { class: "muted empty", id: "fl-empty" }, "No open reroute proposals."));
      return;
    }
    const rows = visibleRoutes(o);
    if (!rows.length) body.append(h("p", { class: "muted empty", id: "fl-empty" }, o.routes.length ? "No flights match." : "No active flights."));
    const byBay: Record<Bay, OpsRoute[]> = { needs: [], airborne: [], planned: [] };
    for (const r of rows) byBay[bayOf(o, r)].push(r);
    const list = h("div", { class: "fl-list", id: "fl-list" });
    for (const bay of ["needs", "airborne", "planned"] as Bay[]) {
      if (!byBay[bay].length) continue; // an empty bay is not shown
      list.append(h("section", { class: "bay", "data-bay": bay, "aria-label": BAY_LABEL[bay] },
        h("h3", { class: "bay-h" }, `${BAY_LABEL[bay]}, ${byBay[bay].length}`),
        ...byBay[bay].map((r) => flightRow(r))));
    }
    body.append(list);
  }

  /** One compact row: callsign, route, ETA on the first line; the ATC pill and the flags on the second. */
  function flightRow(r: OpsRoute): HTMLElement {
    // the worst predicted conflict of this flight sets its ring: high red, medium amber (contract-free UI rule)
    const mine = (o.conflicts?.conflicts ?? []).filter((c) => c.flights.some((f) => f.flight_id === r.flight_id));
    const conflict = mine.some((c) => c.severity === "high") ? "high" : mine.length ? "medium" : "";
    return h("button", { type: "button", class: cls("fl-row", needsAction(o, r) && "needs", r.flight_id === o.selected && "sel"), "data-flight": r.flight_id, "data-aircraft": r.aircraft_id, onclick: () => act.select(r.flight_id) },
      h("div", { class: "r1" },
        h("b", { class: "cs" }, call(r.aircraft_id)),
        h("span", { class: "r2" }, cutText("r2-text", `${short(r, 0)} to ${short(r, r.path_names.length - 1)}`, null)),
        h("span", { class: "eta" }, r.eta_p50 === null ? "none" : fmtClock(r.eta_p50))),
      h("div", { class: "r3" }, atcPill(r.atc),
        r.medical ? h("span", { class: "tag medical" }, "Medical") : null,
        conflict ? h("span", { class: cls("conflict-dot", conflict), title: "Predicted conflict", "aria-label": `${conflict} predicted conflict` }) : null,
        r.status === "off_route" ? h("span", { class: "tag warn" }, ...(r.detected_by === "ai" ? [aiMark(), " "] : []), "Off route") : null));
  }

  function renderDetail(r: OpsRoute): void {
    const conflicts = (o.conflicts?.conflicts ?? []).filter((c) => c.flights.some((f) => f.flight_id === r.flight_id));
    const a = r.atc;
    const cards = visibleReroutes(o).filter((rr) => rr.flight_id === r.flight_id);
    if (cards.length) body.append(h("div", { class: "rr-stack", id: "reroute-cards" }, ...cards.map((rr) => rerouteCard(o, act, rr, true))));
    body.append(
      h("div", { class: "sb-head" },
        h("button", { type: "button", class: "back", id: "fl-back", "aria-label": "Back to all flights", onclick: () => act.select(null) }, icon(ChevronLeft)),
        h("h2", { class: "dd-title" }, call(r.aircraft_id)),
        h("span", { class: `st st-${r.status}` }, r.status.replace("_", " "))),
      h("div", { class: "muted small" }, `${SOURCE_LABEL[r.source]}${r.medical ? ", Medical" : ""}`),
      h("p", { class: "dd-route" }, `${short(r, 0)} to ${short(r, r.path_names.length - 1)}`),
      h("div", { class: "card atc-card", id: "atc-card" },
        atcPill(a),
        a.requirement === "none"
          ? h("p", { class: "muted small" }, "This route crosses no airspace that needs ATC.")
          : h("p", { class: "small" }, `Crosses ${a.airspaces.join(", ") || "controlled airspace"}. ${a.requirement === "clearance" ? "Clearance by radio" : "Two-way radio contact"} is required.`),
        a.requirement !== "none" && a.status === "pending"
          ? h("button", { type: "button", class: "btn primary", id: "mark-btn", disabled: o.markBusy, onclick: () => act.mark(r.flight_id, a.requirement === "clearance" ? "cleared" : "contacted") }, a.requirement === "clearance" ? "Mark cleared" : "Mark contacted")
          : null,
        a.status === "done"
          ? h("div", { class: "done-line" }, h("span", { class: "small muted", id: "marked-by" }, `${a.action === "cleared" ? "Marked cleared" : "Marked contacted"} by ${a.by_name ?? a.by}${a.at ? ` at ${fmtClock(a.at)}` : ""}`),
            h("button", { type: "button", class: "btn", id: "undo-btn", disabled: o.markBusy, onclick: () => act.mark(r.flight_id, "revoke") }, "Undo"))
          : null,
        o.markError ? h("p", { class: "err small", id: "mark-error", role: "alert" }, o.markError) : null,
        a.requirement !== "none" ? h("p", { class: "muted small" }, "Informational: the pilot sees this on the screen and can always go.") : null),
      h("dl", { class: "kv" },
        h("dt", {}, "Arrival"), h("dd", {}, etaWindow(r), " ", aiMark()),
        h("dt", {}, "Bunching risk"), h("dd", {}, r.bunching_risk === null ? "none" : fmtPct(r.bunching_risk)),
        h("dt", {}, "Position"), h("dd", {}, r.position ? `${fmtKt(r.position.ground_speed_mps)}, ${fmtFt(r.position.alt_msl_m)}, fix ${ago(r.position.age_s)}${r.position.off_route ? ", off route" : ""}` : "no live position")));
    const records = rerouteRecords(o, r.flight_id);
    if (records.length) body.append(h("h3", {}, "AI reroutes: records"), ...records);
    if (conflicts.length) {
      body.append(h("h3", {}, "Conflicts"), ...conflicts.map((c) => h("button", { type: "button", class: cls("mini-row hot", c.severity), "data-conflict": c.id, onclick: () => act.selectConflict(c) }, aiMark(), ` ${c.severity === "high" ? "High" : "Medium"}: ${conflictTitle(o, c)}`)));
    }
    body.append(h("h3", {}, "Suggest by radio"), whatIf(o, act));
    body.append(h("details", { class: "fold", id: "eta-fold" }, h("summary", {}, "ETA windows ", aiMark()),
      h("table", { class: "tbl small", id: "eta-table" }, h("thead", {}, h("tr", {}, h("th", {}, "Node"), h("th", {}, "p10"), h("th", {}, "p50"), h("th", {}, "p90"))),
        h("tbody", {}, ...r.nodes.map((n) => h("tr", {}, h("td", {}, n.name), h("td", {}, fmtClock(n.eta_p10)), h("td", {}, fmtClock(n.eta_p50)), h("td", {}, fmtClock(n.eta_p90))))))));
    body.append(h("details", { class: "fold", id: "hist-fold" }, h("summary", {}, "Prediction history"),
      r.prediction_history.length
        ? h("ol", { class: "hist", id: "pred-history" }, ...[...r.prediction_history].reverse().map((p) => h("li", {}, h("span", { class: "muted small" }, `${fmtClock(p.t)} `), `ETA ${fmtClock(p.eta_p50)} (p90 ${fmtClock(p.eta_p90)}): ${p.reason}`)))
        : h("p", { class: "muted small" }, "No changes recorded yet.")));
    body.append(h("details", { class: "fold", id: "track-fold" }, h("summary", {}, "Track"), trackSummary(o.track)));
  }

  function render(): void {
    // keep the scroll position while the same view refreshes; a new view starts at the top
    const top = shown === o.selected ? body.scrollTop : 0;
    const openFolds = new Set([...body.querySelectorAll<HTMLDetailsElement>("details[open]")].map((d) => d.id));
    // a card's button may hold the keyboard focus: the rebuild puts it back on the same button (never moves it elsewhere)
    const focusKey = body.contains(document.activeElement) ? document.activeElement?.getAttribute("data-focus-key") : null;
    shown = o.selected;
    while (body.firstChild) body.removeChild(body.firstChild);
    const r = o.detail && o.selected ? o.routes.find((x) => x.flight_id === o.selected) : undefined;
    if (r) renderDetail(r);
    else renderList();
    for (const id of openFolds) body.querySelector<HTMLDetailsElement>(`#${id}`)?.setAttribute("open", "");
    if (focusKey) body.querySelector<HTMLElement>(`[data-focus-key="${CSS.escape(focusKey)}"]`)?.focus({ preventScroll: true });
    body.scrollTop = top;
  }
  return { el, render };
}

function trackSummary(rows: TelemetryRow[]): HTMLElement {
  if (!rows.length) return h("p", { class: "muted small", id: "track-summary" }, "No stored track yet.");
  const segs: TelemetryRow[][] = [];
  for (const r of rows) {
    const last = segs[segs.length - 1];
    if (last && (last[0].segment_id ?? null) === (r.segment_id ?? null)) last.push(r);
    else segs.push([r]);
  }
  return h("div", { id: "track-summary" },
    h("p", { class: "small" }, `${rows.length} points in ${segs.length} segment${segs.length === 1 ? "" : "s"} (drawn on the map; a new segment starts after a reconnect, gap or lost fix).`),
    h("ul", { class: "segs small" }, ...segs.slice(-6).map((s, i) => h("li", {}, `Segment ${segs.length - Math.min(6, segs.length) + i + 1}: ${fmtClock(s[0].timestamp)} to ${fmtClock(s[s.length - 1].timestamp)}, ${s.length} points`))));
}

function whatIf(o: Ops, act: Actions): HTMLElement {
  const box = h("div", { id: "whatif" });
  const w = o.whatif;
  if (o.whatifError) return h("p", { class: "muted small" }, o.whatifError);
  if (!w) return h("p", { class: "muted small" }, "Calculating alternatives");
  box.append(h("p", { class: "muted small" }, `From ${w.from_name} to ${nodeName(o.airspace, w.destination)}. Nothing here is sent to the aircraft: say it by radio, the pilot updates the route in AeroLink.`));
  if (!w.alternatives.length) box.append(h("p", { class: "small" }, "The route engine sees no alternative route from here."));
  w.alternatives.forEach((a, i) => {
    const saved = a.time_saved_s;
    const text = saved === null ? "No comparison with the current route" : saved >= 30 ? `Suggest by radio: ${Math.round(saved / 60)} min faster` : saved <= -30 ? `${Math.round(-saved / 60)} min slower` : "About the same time";
    box.append(h("button", { type: "button", class: cls("alt", o.whatifShown === i && "sel"), "data-alt": i, "aria-pressed": String(o.whatifShown === i), onclick: () => act.previewWhatIf(o.whatifShown === i ? null : i) },
      h("b", {}, `${text} via ${a.names.filter((_, k, all) => k > 0 && k < all.length - 1).slice(0, 4).join(", then ") || "direct"}`),
      h("div", { class: "muted small" }, `${fmtDuration(a.total_s)} total (${fmtDuration(a.wait_s)} wait), bunching ${fmtPct(a.bunching_risk)}, traffic ${a.traffic}`),
      h("div", { class: "muted small" }, a.reason)));
  });
  return box;
}
