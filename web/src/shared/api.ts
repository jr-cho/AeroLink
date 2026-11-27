/** Typed client for the AeroLink HTTP API (ARCHITECTURE §11.1). All paths are same-origin. */
import type {
  AiInfo, EvalResponse, EvalRow, FlightRow, HealthResponse, Method, ModelsResponse, PlanRequest, RouteResponse, ScenariosResponse, StateResponse, TrafficResponse,
} from "./types.ts";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, init);
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const body = await r.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* not JSON */
    }
    throw new ApiError(r.status, detail || `HTTP ${r.status}`);
  }
  return (await r.json()) as T;
}

const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "content-type": "application/json" },
  body: JSON.stringify(body),
});

/** The server refused because nobody is signed in as an operator (G2). */
export const isUnauthorized = (e: unknown): boolean => e instanceof ApiError && e.status === 401;

/** Is an operator signed in? `undefined`: the server could not be reached (callers then leave controls enabled). */
export async function operatorSignedIn(): Promise<boolean | undefined> {
  try {
    const r = await fetch("/api/auth/me");
    if (r.status === 401) return false;
    return r.ok ? true : undefined;
  } catch {
    return undefined;
  }
}

export const api = {
  state: () => req<StateResponse>("/api/state"),
  /** Corridors, sites and (when served) no-go volumes, as one GeoJSON FeatureCollection. */
  airspace: () => req<GeoJSON.FeatureCollection>("/api/airspace"),
  traffic: () => req<TrafficResponse>("/api/traffic"),
  flights: (active = true) => req<FlightRow[]>(`/api/flights${active ? "?active=true" : ""}`),
  plan: (body: PlanRequest) => req<RouteResponse>("/api/plan", json("POST", body)),
  models: () => req<ModelsResponse>("/api/models"),
  /** The AI identity, models, measured results and disclaimer (public; numbers come from the committed eval files). */
  ai: () => req<AiInfo>("/api/ai"),
  health: () => req<HealthResponse>("/healthz"),
  evalResults: () => req<{ scenarios?: EvalRow[] } | EvalRow[]>("/api/eval"),
  /** The M4 runs and, when the server has them, the M5 ablation table. */
  evalAll: async (): Promise<EvalResponse> => {
    const d = await req<EvalResponse | EvalRow[]>("/api/eval");
    return Array.isArray(d) ? { scenarios: d } : d;
  },
  scenarios: () => req<ScenariosResponse>("/api/scenarios"),
  setMethod: (method: Method) => req<{ method: Method }>("/api/method", json("PUT", { method })),
  setForecaster: (forecaster: string) => req<{ forecaster: string }>("/api/forecaster", json("PUT", { forecaster })),
  setScenario: (scenario: string) => req<{ scenario: string }>("/api/scenario", json("PUT", { scenario })),
};

/** `/api/eval` returns either a bare list or `{scenarios: [...]}`. */
export function evalRows(data: { scenarios?: EvalRow[] } | EvalRow[]): EvalRow[] {
  return Array.isArray(data) ? data : (data.scenarios ?? []);
}
