import { cancelPending, state } from "./state.js";

export class DashboardError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}

export async function getJson(path) {
  const headers = { Accept: "application/json" };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  let response;
  try { response = await fetch(path, { method: "GET", headers, signal: cancelPending(), cache: "no-store" }); }
  catch (error) { if (error.name === "AbortError") throw error; throw new DashboardError(0, "network_error", "Dashboard is unavailable"); }
  if (!response.ok) {
    const body = await response.json().catch(() => ({ error: {} }));
    if (response.status === 401) state.token = "";
    throw new DashboardError(response.status, body.error?.code || "request_failed", body.error?.message || "Dashboard request failed");
  }
  const payload = await response.json();
  if (payload.schema_version !== 1) throw new DashboardError(503, "schema_unsupported", "Dashboard schema is unsupported");
  return payload;
}
