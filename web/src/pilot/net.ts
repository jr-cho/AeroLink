/** Network layer of the pilot screen: HTTP calls, the WebSocket with reconnect, and polling fallback. */
import type { RouteResponse, TrafficResponse } from "../shared/types.ts";
import type { AtcRequirement, DeviceState, FlightStatus, Reroute, WsMessage } from "./types.ts";

export class PilotError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin", ...init });
  if (!r.ok) {
    let code = `http_${r.status}`;
    let msg = r.statusText;
    try {
      const d = (await r.json()).detail;
      if (d && typeof d === "object") {
        code = d.error ?? code;
        msg = d.message ?? msg;
      } else if (typeof d === "string") msg = d;
    } catch {
      /* not JSON */
    }
    throw new PilotError(r.status, code, msg || `HTTP ${r.status}`);
  }
  return (await r.json()) as T;
}

/** A call whose answer has no body (204). */
async function send(path: string, init?: RequestInit): Promise<void> {
  const r = await fetch(path, { credentials: "same-origin", ...init });
  if (!r.ok) throw new PilotError(r.status, `http_${r.status}`, r.statusText || `HTTP ${r.status}`);
}

const post = (body: unknown): RequestInit => ({ method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
const dev = (id: string) => `/api/devices/${encodeURIComponent(id)}`;

/** The screen sends only a destination: the unit's registered vehicle type is applied by the server. */
export const pilotApi = {
  pair: (id: string, code: string) => call<{ device_id: string; expires_at: string }>(`${dev(id)}/pair`, post({ code })),
  state: (id: string) => call<DeviceState>(dev(id)),
  routeRequest: (id: string, destination: string, requestId: string) =>
    call<RouteResponse>(`${dev(id)}/route-request`, post({ destination, request_id: requestId })),
  routeSelection: (id: string, requestId: string, routeId: string) =>
    call<FlightStatus>(`${dev(id)}/route-selection`, post({ request_id: requestId, route_id: routeId })),
  /** What a candidate route needs from ATC (informational). */
  atcRequirement: (id: string, path: string[]) => call<{ requirement: AtcRequirement; airspaces: string[] }>(`${dev(id)}/atc-requirement?path=${encodeURIComponent(path.join(","))}`),
  /** The pilot acknowledges the AI disclaimer of this version (U-API contract §3). */
  aiAck: (id: string, version: string) => send(`${dev(id)}/ai-ack`, post({ version })),
  /** The pilot accepts the ATC reroute: the unit switches to the proposed route (U-API contract §2). */
  rerouteAck: (id: string, rid: string) => call<{ reroute: Reroute; flight_status: FlightStatus }>(`${dev(id)}/reroutes/${encodeURIComponent(rid)}/acknowledge`, { method: "POST" }),
  /** The pilot is unable: the route stays and ATC is told (must be confirmed by radio). */
  rerouteUnable: (id: string, rid: string) => call<{ reroute: Reroute; flight_status: FlightStatus }>(`${dev(id)}/reroutes/${encodeURIComponent(rid)}/unable`, { method: "POST" }),
  /** The AI traffic forecast (U-API contract §5): the pilot screen never calls `/api/traffic` (behind the web map's auth). */
  traffic: (id: string) => call<TrafficResponse>(`${dev(id)}/traffic`),
  /** The pilot ends the flight (idempotent on the server). */
  flightEnd: (id: string) => call<FlightStatus>(`${dev(id)}/flight/end`, { method: "POST" }),
};

export function uuid(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const x = [...b].map((v) => v.toString(16).padStart(2, "0")).join("");
  return `${x.slice(0, 8)}-${x.slice(8, 12)}-${x.slice(12, 16)}-${x.slice(16, 20)}-${x.slice(20)}`;
}

/** Server heartbeat is 5 s (WS_HEARTBEAT_SECONDS): two missed ones plus slack. */
const SILENCE_MS = 12_000;

export type LinkState = "live" | "down";

/** WebSocket with exponential backoff. A dead socket is one that stays silent past two server heartbeats (5 s each) plus slack. */
export class PilotSocket {
  private ws: WebSocket | null = null;
  private retry = 0;
  private timer = 0;
  private watchdog = 0;
  private closed = false;
  private id: string;
  private onMessage: (m: WsMessage) => void;
  private onLink: (s: LinkState) => void;

  constructor(id: string, onMessage: (m: WsMessage) => void, onLink: (s: LinkState) => void) {
    this.id = id;
    this.onMessage = onMessage;
    this.onLink = onLink;
  }

  start(): void {
    this.closed = false;
    this.open();
  }

  private open(): void {
    if (this.closed) return;
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    let ws: WebSocket;
    try {
      ws = new WebSocket(`${scheme}//${location.host}/ws/devices/${encodeURIComponent(this.id)}`);
    } catch {
      this.lost();
      return;
    }
    this.ws = ws;
    // "Live" only once the server has spoken (it sends `state` first): a socket that opens and is
    // dropped at once (proxy, expired session) must not stop the polling fallback.
    let spoke = false;
    ws.onopen = () => this.arm();
    ws.onmessage = (ev) => {
      this.arm();
      if (!spoke) {
        spoke = true;
        this.retry = 0;
        this.onLink("live");
      }
      try {
        this.onMessage(JSON.parse(String(ev.data)) as WsMessage);
      } catch {
        /* ignore a malformed frame */
      }
    };
    ws.onclose = () => {
      if (this.ws === ws) this.lost();
    };
    ws.onerror = () => ws.close();
  }

  private arm(): void {
    clearTimeout(this.watchdog);
    this.watchdog = window.setTimeout(() => this.ws?.close(), SILENCE_MS);
  }

  private lost(): void {
    clearTimeout(this.watchdog);
    this.ws = null;
    this.onLink("down");
    if (this.closed) return;
    const wait = Math.min(15_000, 1000 * 2 ** this.retry++);
    this.timer = window.setTimeout(() => this.open(), wait);
  }

  stop(): void {
    this.closed = true;
    clearTimeout(this.timer);
    clearTimeout(this.watchdog);
    const ws = this.ws;
    this.ws = null;
    ws?.close();
  }
}
