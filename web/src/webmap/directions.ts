/** The "Directions" card: vehicle type + origin + destination -> route options (POST /api/plan). */
import { VEHICLE_COLOR, VEHICLE_LABEL, TRAFFIC_COLOR, TRAFFIC_LABEL } from "../shared/colors.ts";
import { api } from "../shared/api.ts";
import { clear, h } from "../shared/dom.ts";
import { fmtClock, fmtDistance, fmtDuration, fmtPct } from "../shared/format.ts";
import { routeBadges, type RouteBadges } from "../shared/labels.ts";
import { fitTo, routeColor, setRoutes, siteServes, type RouteLine } from "../shared/map.ts";
import type { Method, RouteOption, RouteResponse, VehicleType } from "../shared/types.ts";
import type { Ctx } from "./context.ts";
import { aiMark, icon } from "../shared/icon.ts";
import { ArrowUpDown } from "lucide";

interface Query {
  vt: VehicleType;
  from: string;
  to: string;
}

export interface Directions {
  el: HTMLElement;
  /** Re-plan the open query (called after the method or forecaster changed). */
  replan(): Promise<void>;
}

export function mountDirections(ctx: Ctx): Directions {
  let vt: VehicleType = "passenger";
  let from = "";
  let to = "";
  let results: Partial<Record<Method, { resp: RouteResponse; ms: number; query: Query }>> = {};
  let selected = 0;
  let busy = false;
  let error = "";
  let seq = 0;

  const vtButtons = (["passenger", "medical"] as VehicleType[]).map((v) =>
    h("button", { type: "button", class: "seg", "data-vt": v, "aria-pressed": String(v === vt), onclick: () => setVt(v) },
      h("span", { class: "dot", style: { background: VEHICLE_COLOR[v] } }), VEHICLE_LABEL[v]));
  const fromSel = h("select", { id: "dir-from", "aria-label": "From", onchange: () => { from = fromSel.value; run(); } });
  const toSel = h("select", { id: "dir-to", "aria-label": "To", onchange: () => { to = toSel.value; run(); } });
  const swap = h("button", { type: "button", class: "icon-btn", id: "dir-swap", "aria-label": "Swap origin and destination", title: "Swap", onclick: () => { [from, to] = [to, from]; fillSelects(); run(); } }, icon(ArrowUpDown));
  const go = h("button", { type: "button", class: "primary", id: "dir-go", onclick: () => run() }, "Get directions");
  const out = h("div", { class: "dir-results", id: "dir-results", "aria-live": "polite" });
  const el = h("section", { class: "card directions", "aria-label": "Directions" },
    h("h2", {}, "Directions"),
    h("div", { class: "seg-row", role: "group", "aria-label": "Vehicle type" }, vtButtons),
    h("div", { class: "od" },
      h("div", { class: "od-fields" },
        h("label", {}, h("span", { class: "pin from" }), fromSel),
        h("label", {}, h("span", { class: "pin to" }), toSel)),
      swap),
    go, out);

  function sites(forType: VehicleType): { id: string; name: string }[] {
    return [...ctx.airspace.nodes.values()].filter((n) => siteServes(n.props, forType)).map((n) => ({ id: n.props.id, name: n.props.name })).sort((a, b) => a.name.localeCompare(b.name));
  }

  function fillSelects(): void {
    const list = sites(vt);
    const opt = (s: { id: string; name: string }) => h("option", { value: s.id }, s.name);
    clear(fromSel);
    clear(toSel);
    fromSel.append(h("option", { value: "" }, "Choose origin"), ...list.map(opt));
    toSel.append(h("option", { value: "" }, "Choose destination"), ...list.map(opt));
    fromSel.value = list.some((s) => s.id === from) ? from : "";
    toSel.value = list.some((s) => s.id === to) ? to : "";
    from = fromSel.value;
    to = toSel.value;
  }

  function setVt(v: VehicleType): void {
    vt = v;
    vtButtons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.vt === v)));
    results = {};
    setRoutes(ctx.map, []);
    fillSelects();
    render();
  }

  async function run(): Promise<void> {
    if (!from || !to || from === to) {
      error = from && from === to ? "Origin and destination are the same." : "";
      render();
      return;
    }
    const my = ++seq;
    busy = true;
    error = "";
    render();
    const query: Query = { vt, from, to };
    const t0 = performance.now();
    try {
      const resp = await api.plan({ vehicle_type: vt, origin_node: from, destination_node: to });
      if (my !== seq) return;
      const ms = performance.now() - t0;
      // Results from another query are stale: drop them, keep only this method's fresh one.
      for (const k of Object.keys(results) as Method[]) if (JSON.stringify(results[k]!.query) !== JSON.stringify(query)) delete results[k];
      results[resp.method] = { resp, ms, query };
      selected = 0;
      draw(true);
    } catch (e) {
      if (my !== seq) return;
      error = `Could not get routes: ${(e as Error).message}`;
      results = {};
      setRoutes(ctx.map, []);
    } finally {
      if (my === seq) busy = false;
      render();
    }
  }

  function active() {
    return results[ctx.method];
  }

  /** Draw the active method's routes in distinct colours; the other method's fastest route dashed. */
  function draw(fit: boolean): void {
    const lines: RouteLine[] = [];
    const other = results[ctx.method === "aerolink" ? "classical" : "aerolink"];
    if (other?.resp.routes[0]) lines.push({ id: "other", waypoints: other.resp.routes[0].waypoints, color: "#80868b", dashed: true });
    const a = active();
    a?.resp.routes.forEach((r, i) => lines.push({ id: r.route_id, waypoints: r.waypoints, color: routeColor(r.rank), selected: i === selected }));
    // draw the selected route last (on top)
    lines.sort((x, y) => Number(!!x.selected) - Number(!!y.selected));
    setRoutes(ctx.map, lines);
    if (fit && a?.resp.routes.length) fitTo(ctx.map, a.resp.routes.flatMap((r) => r.waypoints.map(([lat, lon]) => [lon, lat] as [number, number])), { top: 100, bottom: 120, left: 420, right: 120 });
  }

  function etaRange(r: RouteOption): string {
    const last = r.eta[r.eta.length - 1];
    if (!last) return "";
    return `${fmtClock(last.eta_p10)} to ${fmtClock(last.eta_p90)}`;
  }

  function card(r: RouteOption, i: number, badges?: RouteBadges): HTMLElement {
    return h("button", { type: "button", class: `route-card${i === selected ? " selected" : ""}`, "data-rank": r.rank, "aria-pressed": String(i === selected), onclick: () => { selected = i; draw(false); render(); } },
      h("span", { class: "swatch", style: { background: routeColor(r.rank) } }),
      h("span", { class: "rc-main" },
        h("span", { class: "rc-time" }, fmtDuration(r.est_time_s),
          badges?.recommended ? h("span", { class: "chip recommended" }, "Recommended") : null,
          badges?.fastest ? h("span", { class: "chip fastest" }, "Fastest") : null),
        h("span", { class: "rc-eta" }, `Arrive ${etaRange(r)} (10 to 90% range)`),
        h("span", { class: "rc-meta" },
          h("span", { class: "chip", style: { color: TRAFFIC_COLOR[r.traffic] } }, `${TRAFFIC_LABEL[r.traffic]} traffic`),
          ` ${fmtDistance(r.distance_m)}`, r.expected_wait_s > 0 ? `, wait ${fmtDuration(r.expected_wait_s)}` : "", `, bunching risk ${fmtPct(r.bunching_risk)}`),
        h("span", { class: "rc-reason" }, r.reason)));
  }

  function compare(): HTMLElement | null {
    const c = results.classical?.resp.routes[0];
    const a = results.aerolink?.resp.routes[0];
    if (!c || !a) return null;
    const diff = c.est_time_s - a.est_time_s;
    const same = JSON.stringify(c.waypoints) === JSON.stringify(a.waypoints);
    const verdict = same ? "Both methods pick the same route" : diff > 0 ? `AeroLink is ${fmtDuration(diff)} faster` : diff < 0 ? `AeroLink is ${fmtDuration(-diff)} slower` : "Same predicted time, different route";
    return h("div", { class: "compare", id: "dir-compare" },
      h("div", { class: "cmp-title" }, "Classical vs AeroLink"),
      h("div", { class: "cmp-row" }, h("span", {}, "Classical"), h("b", {}, fmtDuration(c.est_time_s)), h("span", { class: "muted" }, c.reason)),
      h("div", { class: "cmp-row" }, h("span", {}, "AeroLink"), h("b", {}, fmtDuration(a.est_time_s)), h("span", { class: "muted" }, a.reason)),
      h("div", { class: "cmp-verdict" }, verdict, !same ? ". The grey dashed line is the other method's route." : ""));
  }

  function render(): void {
    clear(out);
    go.disabled = busy || !from || !to;
    go.textContent = busy ? "Planning" : "Get directions";
    if (error) out.append(h("div", { class: "error", role: "alert" }, error));
    const a = active();
    if (!a) {
      if (!busy && !error) out.append(h("p", { class: "muted hint" }, "Pick an origin and a destination to see route options."));
      return;
    }
    const { resp, ms } = a;
    out.append(h("div", { class: "dir-head" },
      h("span", { class: "chip method" }, resp.method === "aerolink" ? "AeroLink" : "Classical"),
      h("span", { class: "muted", id: "dir-timing" }, `${resp.routes.length} route${resp.routes.length === 1 ? "" : "s"}, ${Math.round(ms)} ms, ${resp.forecaster}`)));
    if (resp.status !== "ok") {
      out.append(h("div", { class: "error" }, resp.reason ?? "No route available."));
      return;
    }
    if (resp.summary) out.append(h("div", { class: "summary-box", id: "dir-summary-box" },
      h("p", { class: "summary", id: "dir-summary" }, aiMark(), " ", resp.summary),
      h("p", { class: "muted small", id: "dir-summary-note" }, "AI-generated; verify before acting.")));
    const badges = routeBadges(resp.routes);
    resp.routes.forEach((r, i) => out.append(card(r, i, badges.get(r.route_id))));
    const cmp = compare();
    if (cmp) out.append(cmp);
  }

  const onData = () => { if (!from && !to && !el.dataset.filled && ctx.airspace.nodes.size) { el.dataset.filled = "1"; fillSelects(); } };
  ctx.listeners.add(onData);
  fillSelects();
  render();
  return { el, replan: run };
}
