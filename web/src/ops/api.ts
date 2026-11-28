/** HTTP and WebSocket clients for the ATC dashboard. Read-only except acknowledging alerts and the system settings. */
import { ApiError, api as shared } from "../shared/api.ts";
import type { ModelsResponse, TrafficResponse } from "../shared/types.ts";
import type { AdsbResponse, AiInfo, Atc, AlertRow, Congestion, ConflictsResponse, DeepHealth, DeviceRow, FlightOverview, OpsRoute, OverviewResponse, Reroute, TelemetryRow, WhatIf, Watchdog } from "./types.ts";

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new ApiError(r.status, `${path}: HTTP ${r.status}`);
  return (await r.json()) as T;
}

/** POST with no body; errors carry the server's message (`detail` as a string or `{message}`, or top-level `error`/`message`). */
async function post<T>(path: string): Promise<T> {
  const r = await fetch(path, { method: "POST" });
  if (!r.ok) {
    let d = r.statusText;
    try {
      const b = await r.json();
      d = typeof b.detail === "string" ? b.detail : (b.detail?.message ?? b.message ?? d);
    } catch { /* not JSON */ }
    throw new ApiError(r.status, String(d));
  }
  return (await r.json()) as T;
}

export const ops = {
  ...shared,
  routes: () => get<OpsRoute[]>("/api/ops/routes"),
  conflicts: (horizonMin = 45) => get<ConflictsResponse>(`/api/ops/conflicts?horizon_min=${horizonMin}`),
  whatif: (flightId: string) => get<WhatIf>(`/api/ops/whatif/${encodeURIComponent(flightId)}`),
  congestion: () => get<Congestion>("/api/ops/congestion"),
  adsb: () => get<AdsbResponse>("/api/adsb"),
  devices: () => get<DeviceRow[]>("/api/devices"),
  deepHealth: () => get<DeepHealth>("/healthz?deep=1"),
  alerts: (limit = 200) => get<AlertRow[]>(`/api/alerts?limit=${limit}`),
  telemetry: (id: string, limit = 600) => get<TelemetryRow[]>(`/api/aircraft/${encodeURIComponent(id)}/telemetry?limit=${limit}`),
  trafficFull: () => get<TrafficResponse>("/api/traffic"),
  /** Open AI reroute proposals (proposed or approved), for the sidebar cards and the map preview. */
  reroutes: () => get<{ reroutes: Reroute[] }>("/api/ops/reroutes?status=open"),
  /** The AI overview of the next 30 min (R-contract §1): answered from the server's cache at once. */
  overview: () => get<OverviewResponse>("/api/ops/overview"),
  /** One flight's two-sentence AI overview for the popover (R-contract §2); 503 or 404 means no AI text. */
  flightOverview: (flightId: string) => get<FlightOverview>(`/api/ops/flights/${encodeURIComponent(flightId)}/overview`),
  /** The AI's identity and measured results (public, cheap). */
  ai: () => get<AiInfo>("/api/ai"),
  /** Record that the operator saw the AI disclaimer (contract §3). */
  async ackAi(version: string): Promise<void> {
    const r = await fetch("/api/auth/ai-ack", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ version }) });
    if (!r.ok) throw new ApiError(r.status, r.statusText);
  },
  /** Every reroute of one flight, all statuses, newest first, with events (the flight's records). */
  rerouteHistory: async (flightId: string): Promise<Reroute[]> => (await get<{ reroutes: Reroute[] }>(`/api/ops/reroutes?flight_id=${encodeURIComponent(flightId)}`)).reroutes,
  approveReroute: (id: string) => post<Reroute>(`/api/ops/reroutes/${encodeURIComponent(id)}/approve`),
  dismissReroute: (id: string) => post<Reroute>(`/api/ops/reroutes/${encodeURIComponent(id)}/dismiss`),
  modelsFull: () => get<ModelsResponse & { models?: { name: string; loaded: boolean; error?: string | null }[] }>("/api/models"),
  /** ATC marks a flight's requirement handled by radio ("cleared" for Class B, "contacted" for C/D) or undoes it. */
  async mark(flightId: string, action: "cleared" | "contacted" | "revoke"): Promise<Atc> {
    const r = await fetch(`/api/ops/flights/${encodeURIComponent(flightId)}/clearance`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ action }) });
    if (!r.ok) {
      let d = r.statusText;
      try { const b = await r.json(); d = typeof b.detail === "string" ? b.detail : (b.detail?.message ?? d); } catch { /* not JSON */ }
      throw new ApiError(r.status, String(d));
    }
    return (await r.json()) as Atc;
  },
  /** Acknowledge an alert. The server records the logged-in operator; nothing is sent about who. */
  async ack(id: number): Promise<AlertRow> {
    const r = await fetch(`/api/alerts/${id}/ack`, { method: "POST" });
    if (!r.ok) {
      let d = r.statusText;
      try { const b = await r.json(); d = typeof b.detail === "string" ? b.detail : (b.detail?.message ?? d); } catch { /* not JSON */ }
      throw new ApiError(r.status, String(d));
    }
    return (await r.json()) as AlertRow;
  },
};

export type OpsMessage =
  | { type: "state"; payload: { unacked: AlertRow[]; watchdogs: Watchdog[] } }
  | { type: "alert"; payload: AlertRow }
  | { type: "ack"; payload: AlertRow }
  | { type: "watchdog"; payload: Watchdog }
  | { type: "clearance"; payload: { flight_id: string; aircraft_id: string; atc: Atc } }
  | { type: "reroute"; payload: Reroute }
  | { type: "heartbeat"; payload: { t: string } };

/** `/ws/ops` with reconnect. `onState(false)` while it is down so the page can say the live feed is lost. */
export function openOpsSocket(onMessage: (m: OpsMessage) => void, onState: (up: boolean) => void, onRefused: () => void = () => {}): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let delay = 1000;
  let opened = false;
  let silence: ReturnType<typeof setTimeout> | undefined;
  const arm = () => {
    clearTimeout(silence);
    silence = setTimeout(() => ws?.close(), 15_000); // the server sends a heartbeat every 5 s
  };
  const connect = () => {
    if (closed) return;
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${scheme}//${location.host}/ws/ops`);
    opened = false;
    ws.onopen = () => { opened = true; delay = 1000; onState(true); arm(); };
    ws.onmessage = (e) => {
      arm();
      try { onMessage(JSON.parse(e.data) as OpsMessage); } catch { /* ignore malformed */ }
    };
    ws.onclose = () => {
      clearTimeout(silence);
      onState(false);
      if (!opened) onRefused(); // never got in: the handshake was refused (no session) or the server is down
      if (!closed) setTimeout(connect, delay);
      delay = Math.min(delay * 2, 15_000);
    };
    ws.onerror = () => ws?.close();
  };
  connect();
  return () => { closed = true; clearTimeout(silence); ws?.close(); };
}
