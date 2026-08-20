import { badge, duration, knownCost, node, time } from "../format.js";
import { navigate } from "../state.js";

export function renderRequests(payload) {
  const root = document.createDocumentFragment(); const area = node("section", "section"); area.append(node("h1", "", "Requests"));
  if (!payload.items.length) { area.append(node("div", "empty", "No persisted terminal requests match these filters")); root.append(area); return root; }
  const wrap = node("div", "table-wrap"), table = node("table"), head = node("thead"), hr = node("tr");
  ["Time", "Request", "Profile", "Policy", "Model", "Provider", "Attempts", "Status", "Latency", "Tokens", "Known estimated cost"].forEach((label, index) => hr.append(node("th", index === 2 || index === 3 || index === 5 || index === 8 ? "optional-col" : "", label))); head.append(hr); table.append(head);
  const body = node("tbody");
  for (const item of payload.items) { const row = node("tr"); row.tabIndex = 0; row.append(node("td", "", time(item.received_at)), node("td", "mono", item.request_id.slice(0, 8)), node("td", "optional-col", item.effective_profile || item.profile), node("td", "optional-col mono", `${item.policy_role || "legacy_unknown"} ${(item.policy_hash || "").slice(0, 8)}`), node("td", "", item.primary_model === item.final_model ? item.final_model : `${item.primary_model} → ${item.final_model}`), node("td", "optional-col", item.final_provider), node("td", "", `${item.attempt_count} / ${item.upstream_invoked_count}`)); const status = node("td"); status.append(badge(item.status, item.status === "success" ? "success" : "error")); row.append(status, node("td", "optional-col", duration(item.total_latency_ms)), node("td", "", `${item.input_tokens ?? "?"} / ${item.output_tokens ?? "?"}`), node("td", "", knownCost(item.known_cost_nanos, item.cost_currency))); row.addEventListener("click", () => navigate(`/admin/requests/${item.request_id}`)); body.append(row); }
  table.append(body); wrap.append(table); area.append(wrap);
  if (payload.has_more) { const controls = node("div", "pagination"), next = node("button", "", "Next page"); next.addEventListener("click", () => { const params = new URLSearchParams(location.search); params.set("view", "requests"); params.set("cursor", payload.next_cursor); navigate(`/admin?${params}`); }); controls.append(next); area.append(controls); }
  root.append(area); return root;
}
