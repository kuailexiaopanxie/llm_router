import { lineChart } from "../charts.js";
import { badge, duration, knownCost, node, ratio, time } from "../format.js";
import { navigate } from "../state.js";

function section(title) { const area = node("section", "section"); area.append(node("h2", "", title)); return area; }
function kpi(label, value, detail) { const item = node("div", "kpi"); item.append(node("div", "kpi-label", label), node("div", "kpi-value", value), node("div", "kpi-detail", detail)); return item; }

function breakdown(payload, name) {
  const area = section(name); const tableWrap = node("div", "table-wrap"); const table = node("table");
  const head = node("thead"), header = node("tr"); [name, "Requests", "Success", "Latest"].forEach(label => header.append(node("th", "", label))); head.append(header); table.append(head);
  const body = node("tbody");
  for (const item of payload?.items || []) { const row = node("tr"); row.tabIndex = 0; row.append(node("td", "mono", item.key), node("td", "", item.requests), node("td", "", ratio(item.success).value), node("td", "", time(item.latest_received_at)));
    row.addEventListener("click", () => { const params = new URLSearchParams(); const key = Object.keys(item.filter || {})[0]; if (key) params.set(key === "final_model" ? "model" : key, item.filter[key]); params.set("view", "requests"); navigate(`/admin?${params}`); }); body.append(row); }
  table.append(body); tableWrap.append(table); area.append(tableWrap); return area;
}

export function renderOverview(payload) {
  const root = document.createDocumentFragment(); const title = node("section", "section"); title.append(node("h1", "", "Overview"));
  const runtime = payload.runtime || {}; const strip = node("div", `status-strip ${runtime.status === "unavailable" ? "gap" : ""}`);
  strip.append(badge(runtime.ready ? "Router ready" : "Router not ready", runtime.ready ? "success" : "gap"), badge(runtime.capture_enabled ? "Capture enabled" : "Capture disabled", runtime.capture_enabled ? "success" : "gap"), node("span", "", `Latest persisted ${time(payload.freshness?.latest_completed_at)}`), node("span", "", `Drops since start ${runtime.observation_dropped_since_start ?? "unknown"}`)); title.append(strip); root.append(title);
  const summary = payload.summary; const success = ratio(summary.success), fallback = ratio(summary.fallback); const costs = summary.cost.known_amounts || [];
  const band = node("section", "section"); band.append(node("h2", "", "Current range")); const grid = node("div", "kpis");
  grid.append(kpi("Requests", summary.requests, `${time(payload.range.start)} to ${time(payload.range.end)}`), kpi("Success", success.value, success.detail), kpi("Fallback", fallback.value, fallback.detail), kpi("P50 latency", duration(summary.latency_ms.p50), "Nearest rank"), kpi("P95 latency", duration(summary.latency_ms.p95), "Nearest rank"), kpi("Known estimated cost", costs.map(item => knownCost(item.known_amount_nanos, item.currency)).join(" · ") || "Unknown", ratio(summary.cost.coverage).detail)); band.append(grid); root.append(band);
  const requests = section("Requests over time"); requests.append(lineChart(payload.request_series || [], "requests")); root.append(requests);
  const cost = section("Known estimated cost"); if (!payload.known_cost_series_by_currency?.length) cost.append(node("div", "empty", "No known estimated cost in this range")); else { for (const currency of new Set(payload.known_cost_series_by_currency.map(item => item.currency))) { cost.append(node("h3", "", currency), lineChart(payload.known_cost_series_by_currency.filter(item => item.currency === currency).map(item => ({ ...item, value: Number(BigInt(item.known_amount_nanos) / 1000000n) })), "value")); } } root.append(cost);
  root.append(breakdown(payload.final_model_breakdown, "Models"), breakdown(payload.provider_breakdown, "Providers"), breakdown(payload.profile_breakdown, "Profiles"), breakdown(payload.policy_role_breakdown, "Policies"), breakdown(payload.route_reason_breakdown, "Route reasons"));
  const failures = section("Recent failures"); const list = node("div", "table-wrap"), table = node("table"), head = node("thead"), hr = node("tr"); ["Time", "Request", "Status", "Stage", "Model", "Latency"].forEach(value => hr.append(node("th", "", value))); head.append(hr); table.append(head); const body = node("tbody"); for (const item of payload.recent_failures || []) { const row = node("tr"); row.append(node("td", "", time(item.received_at)), node("td", "mono", item.request_id.slice(0, 8)), node("td", "", item.status), node("td", "", item.terminal_stage), node("td", "", item.final_model), node("td", "", duration(item.total_latency_ms))); row.addEventListener("click", () => navigate(`/admin/requests/${item.request_id}`)); body.append(row); } table.append(body); list.append(table); failures.append(list); root.append(failures); return root;
}
