/**
 * The arrivals timeline (R2 checkpoint 2, R-contract §3, §6.2, §6.3, §6.6): the right column. One lane per landing
 * site with arrivals in the next 45 minutes (the busiest four), "Now" at the bottom and +45 at the top. Each arrival is
 * a tag spanning its AI window (p10 to p90); an open reroute proposal adds a dashed ghost tag at its proposed window.
 * Built once and updated in place, so a tag under the pointer is the same element after every poll (no re-creation).
 */
import { h } from "../shared/dom.ts";
import { ChevronUp, icon, Sparkles } from "../shared/icon.ts";
import { cutText, showFullText } from "./fulltext.ts";
import { laneLabels } from "./labels.ts";
import type { Actions, Ops } from "./panels.ts";
import { call, nodeName } from "./util.ts";

export const HORIZON_S = 45 * 60;
/** Lanes shown: three below 1600 px, four from 1600 px (R2 CP2 fix 2); the rest are counted in "+N sites". */
export const maxLanes = (width: number): number => (width >= 1600 ? 4 : 3);
/** Each overlapping tag in a lane sits this far right of the one before, so its callsign still fits (fix 1). */
export const staggerPx = (lanes: number): number => (lanes >= 4 ? 4 : 10);

export interface Tag {
  key: string;
  flightId: string;
  label: string;
  lane: string;
  /** Window in fractions of the horizon: 0 is Now, 1 is +45 min. */
  lo: number;
  hi: number;
  /** The window runs past +45 min: clipped at the top edge (§6.2). */
  clipped: boolean;
  /** p50 has passed and the flight has not landed: pinned at Now (§6.2). */
  due: boolean;
  ghost: boolean;
  medical: boolean;
  /** The visible text: the callsign (never truncated, fix 1). */
}
export interface Callout {
  key: string;
  lane: string;
  hi: number;
  text: string;
}
export interface Model {
  lanes: { node: string; name: string; full: string; count: number }[];
  hiddenSites: number;
  tags: Tag[];
  callouts: Callout[];
}

const clamp01 = (x: number) => Math.min(1, Math.max(0, x));

/** The timeline's contents from the page state, at `nowS` (epoch seconds). Pure, so it can be reasoned about and tested. */
export function arrivalModel(o: Ops, nowS: number, lanesShown = 3): Model {
  const tags: Tag[] = [];
  const counts = new Map<string, number>();
  const names = new Map<string, string>();
  const place = (p10: number, p90: number) => {
    const clipped = p90 > HORIZON_S;
    return { lo: clamp01(p10 / HORIZON_S), hi: clamp01(p90 / HORIZON_S), clipped };
  };
  for (const r of o.routes) {
    if (r.eta_p10 === null || r.eta_p50 === null || r.eta_p90 === null) continue;
    const p10 = r.eta_p10 - nowS, p50 = r.eta_p50 - nowS, p90 = r.eta_p90 - nowS;
    if (p10 > HORIZON_S) continue; // §6.2: a p10 beyond +45 min is not shown
    const due = p50 <= 0;
    const w = place(Math.max(0, p10), p90);
    const lo = due ? 0 : w.lo;
    tags.push({ key: `t:${r.flight_id}`, flightId: r.flight_id, label: call(r.aircraft_id) + (due ? " due" : ""), lane: r.destination, lo, hi: Math.max(w.hi, lo), clipped: w.clipped, due, ghost: false, medical: r.medical });
    counts.set(r.destination, (counts.get(r.destination) ?? 0) + 1);
    names.set(r.destination, nodeName(o.airspace, r.destination));
  }
  // §6.3: an open proposal (proposed or approved, until answered) is a dashed ghost at its proposed window
  for (const rr of o.reroutes) {
    if ((rr.status !== "proposed" && rr.status !== "approved") || o.collapsedReroutes.has(rr.id)) continue;
    const [a, b] = rr.proposed.eta_window_s ?? [rr.proposed.eta_s - 60, rr.proposed.eta_s + 60];
    const p10 = a - nowS, p90 = b - nowS;
    if (p10 > HORIZON_S) continue;
    const w = place(Math.max(0, p10), p90);
    tags.push({ key: `g:${rr.id}`, flightId: rr.flight_id, label: rr.callsign, lane: rr.destination, lo: w.lo, hi: Math.max(w.hi, w.lo), clipped: w.clipped, due: false, ghost: true, medical: false });
    names.set(rr.destination, nodeName(o.airspace, rr.destination));
    counts.set(rr.destination, (counts.get(rr.destination) ?? 0) + 1);
  }
  const all = [...counts.entries()].map(([node, count]) => ({ node, name: names.get(node) ?? node, count }));
  // the busiest first; ties by name, so a lane does not jump around between polls
  all.sort((x, y) => y.count - x.count || x.name.localeCompare(y.name));
  const shown = all.slice(0, lanesShown);
  const labels = laneLabels(shown.map((l) => ({ node: l.node, full: l.name })));
  const laneSet = new Set(shown.map((l) => l.node));
  const callouts: Callout[] = (o.overview?.bunches ?? [])
    .filter((b) => laneSet.has(b.node))
    .map((b) => ({ key: `b:${b.node}:${b.from_s}`, lane: b.node, hi: clamp01((b.to_s - nowS) / HORIZON_S), text: `${b.flight_ids.length} arrival${b.flight_ids.length === 1 ? "" : "s"} within ${b.within_min} min` }));
  return {
    lanes: shown.map((l) => ({ node: l.node, name: labels.get(l.node)!, full: l.name, count: l.count })),
    hiddenSites: all.length - shown.length,
    tags: tags.filter((t) => laneSet.has(t.lane)),
    callouts,
  };
}

/** Greedy packing: overlapping tags in one lane sit side by side. Returns slot index and slot count per tag key. */
export function packSlots(tags: Tag[]): Map<string, { slot: number; slots: number }> {
  const out = new Map<string, { slot: number; slots: number }>();
  const byLane = new Map<string, Tag[]>();
  for (const t of tags) byLane.set(t.lane, [...(byLane.get(t.lane) ?? []), t]);
  for (const list of byLane.values()) {
    const ends: number[] = [];
    const placed: { t: Tag; slot: number }[] = [];
    for (const t of [...list].sort((a, b) => a.lo - b.lo || a.key.localeCompare(b.key))) {
      let slot = ends.findIndex((e) => e <= t.lo + 1e-3);
      if (slot < 0) { slot = ends.length; ends.push(t.hi); } else ends[slot] = t.hi;
      placed.push({ t, slot });
    }
    for (const p of placed) out.set(p.t.key, { slot: p.slot, slots: ends.length });
  }
  return out;
}

interface Lane { col: HTMLElement; title: HTMLElement; plot: HTMLElement }
export type Full = { node: string; full: string };

/** The timeline element and its `paint`, which updates the DOM in place (keys are the flight or the reroute). */
export function createArrivals(act: Actions) {
  const ticks = h("div", { class: "arr-ticks", "aria-hidden": "true" },
    h("span", { class: "tk", style: { bottom: "100%" } }, "45 min"), h("span", { class: "tk", style: { bottom: "66.7%" } }, "30 min"),
    h("span", { class: "tk", style: { bottom: "33.3%" } }, "15 min"), h("span", { class: "tk", style: { bottom: "11.1%" } }, "5 min"),
    h("span", { class: "tk now", style: { bottom: "0" } }, "Now"));
  const lanesBox = h("div", { class: "arr-lanes", id: "arr-lanes" });
  const more = h("span", { class: "arr-more", id: "arr-more", hidden: true });
  const el = h("aside", { class: "arrivals", id: "arrivals", "aria-label": "Arrivals" },
    h("div", { class: "arr-head" }, h("h2", {}, "Arrivals"), more),
    h("p", { class: "arr-sub muted small" }, "Bar height shows the AI arrival window (likely range)"),
    h("div", { class: "arr-grid" }, ticks, lanesBox));
  const lanes = new Map<string, Lane>();
  let laneCount = 3;
  const tagEls = new Map<string, HTMLElement>();
  const calloutEls = new Map<string, HTMLElement>();
  let laneOrder = "";

  function paint(o: Ops, nowS = Date.now() / 1000): void {
    laneCount = maxLanes(innerWidth);
    const m = arrivalModel(o, nowS, laneCount);
    // lanes: created once, kept in order; reordered only when the order really changes
    const order = m.lanes.map((l) => l.node).join("|");
    for (const l of m.lanes) {
      let lane = lanes.get(l.node);
      if (!lane) {
        const title = h("div", { class: "arr-lane-h" });
        const plot = h("div", { class: "arr-plot" });
        const col = h("div", { class: "arr-lane", "data-lane": l.node }, title, plot);
        lane = { col, title, plot };
        lanes.set(l.node, lane);
      }
      const info = `${l.count} arrival${l.count === 1 ? "" : "s"} in 45 min`;
      if (lane.title.dataset.sig !== `${l.name}|${l.full}|${info}`) {
        lane.title.dataset.sig = `${l.name}|${l.full}|${info}`;
        lane.title.title = `${l.full}: ${info}`;
        lane.title.replaceChildren(cutText("arr-lane-name", l.name, () => showFullText(l.full, [info]), `${l.full}: ${info}`, true));
      }
    }
    if (order !== laneOrder) {
      for (const [node, lane] of lanes) if (!m.lanes.some((l) => l.node === node)) { lane.col.remove(); lanes.delete(node); }
      for (const l of m.lanes) lanesBox.append(lanes.get(l.node)!.col); // re-appending moves them into order
      laneOrder = order;
    }
    more.hidden = m.hiddenSites === 0;
    more.textContent = `${m.hiddenSites} more site${m.hiddenSites === 1 ? "" : "s"}`;
    more.title = "More landing sites with arrivals in the next 45 min than the four lanes show";

    const pack = packSlots(m.tags);
    const seen = new Set<string>();
    for (const t of m.tags) {
      seen.add(t.key);
      let el = tagEls.get(t.key);
      if (!el) {
        el = h("button", { type: "button", class: "arr-tag", "data-key": t.key, "data-flight": t.flightId, onclick: () => act.select(t.flightId) });
        tagEls.set(t.key, el);
      }
      const { slot } = pack.get(t.key)!;
      const off = slot * staggerPx(laneCount);
      el.dataset.flight = t.flightId;
      el.className = ["arr-tag", t.ghost && "ghost", t.due && "due", t.clipped && "clipped", t.medical && "medical", t.flightId === o.selected && "sel"].filter(Boolean).join(" ");
      el.style.bottom = `${(t.lo * 100).toFixed(2)}%`;
      el.style.height = `${Math.max(0, (t.hi - t.lo) * 100).toFixed(2)}%`;
      el.style.left = `${off + 3}px`;
      el.style.width = `calc(100% - ${off + 6}px)`;
      const sig = `${t.ghost ? "g" : ""}${t.clipped ? "c" : ""}${t.label}`;
      if (el.dataset.sig !== sig) {
        el.dataset.sig = sig;
        el.replaceChildren(...(t.ghost ? [icon(Sparkles), " "] : []), t.label, ...(t.clipped ? [icon(ChevronUp, "clip-mark")] : []));
      }
      el.title = t.ghost ? "If rerouted (AI proposal)" : `${t.label}${t.clipped ? " (window runs past 45 min)" : ""}`;
      el.setAttribute("aria-label", t.ghost ? `Proposed reroute, ${t.label}` : `Arrival ${t.label}`);
      const lane = lanes.get(t.lane)!;
      if (el.parentElement !== lane.plot) lane.plot.append(el);
    }
    for (const [key, el] of tagEls) if (!seen.has(key)) { el.remove(); tagEls.delete(key); }

    const seenC = new Set<string>();
    for (const c of m.callouts) {
      seenC.add(c.key);
      let el = calloutEls.get(c.key);
      if (!el) { el = h("div", { class: "arr-callout", "data-key": c.key }); calloutEls.set(c.key, el); }
      el.style.bottom = `${(c.hi * 100).toFixed(2)}%`;
      if (el.dataset.sig !== c.text) { el.dataset.sig = c.text; el.replaceChildren(h("span", {}, icon(Sparkles), " ", c.text)); }
      const lane = lanes.get(c.lane)!;
      if (el.parentElement !== lane.plot) lane.plot.append(el);
    }
    for (const [key, el] of calloutEls) if (!seenC.has(key)) { el.remove(); calloutEls.delete(key); }
  }
  return { el, paint };
}
