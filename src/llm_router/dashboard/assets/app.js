import { DashboardError, getJson } from "./http.js";
import { node } from "./format.js";
import { apiParams, markFresh, markStale, navigate, readLocation, state, rangeParams } from "./state.js";
import { renderOverview } from "./views/overview.js";
import { renderRequests } from "./views/requests.js";
import { renderDetail } from "./views/request-detail.js";

const app = document.querySelector("#app"), dialog = document.querySelector("#unlock");

function setNavigation() {
  document.querySelectorAll("[data-nav]").forEach(link => link.classList.toggle("active", link.dataset.nav === (state.view === "detail" ? "requests" : state.view)));
}

function updateForm() {
  const form = document.querySelector("#filters");
  for (const name of ["status", "profile", "model", "provider"]) form.elements[name].value = state.filters.get(name) || "";
}

function endpoint() {
  if (state.view === "detail") return `/admin/api/v1/requests/${location.pathname.split("/").pop()}`;
  const params = apiParams();
  if (state.view !== "requests") params.delete("cursor");
  return `/admin/api/v1/${state.view}?${params}`;
}

function locked(message = "") {
  document.querySelector("#unlock-error").textContent = message;
  if (!dialog.open) dialog.showModal();
  setTimeout(() => dialog.querySelector("input").focus(), 0);
}

async function load(manual = false) {
  setNavigation(); updateForm();
  if (!state.last || manual) app.replaceChildren(node("div", "loading", "Loading dashboard"));
  try {
    const payload = await getJson(endpoint()); markFresh(payload); state.last = payload;
    const view = state.view === "overview" ? renderOverview(payload) : state.view === "requests" ? renderRequests(payload) : renderDetail(payload);
    app.replaceChildren(view); document.querySelector("#live-state").textContent = `Updated ${new Date(payload.generated_at).toLocaleTimeString()}`;
    document.title = state.view === "detail" ? `Request ${payload.request.request_id.slice(0, 8)} · LLM Router` : `${state.view === "overview" ? "Overview" : "Requests"} · LLM Router`;
  } catch (error) {
    if (error.name === "AbortError") return;
    markStale();
    if (error instanceof DashboardError && error.status === 401) { locked("Enter the configured client key"); return; }
    if (state.last) { const warning = node("div", "stale", `Stale snapshot · ${error.message}`); app.prepend(warning); }
    else app.replaceChildren(node("div", "error-state", error.message || "Dashboard is unavailable"));
    document.querySelector("#live-state").textContent = "Stale";
  }
}

function schedule() {
  clearInterval(state.timer);
  state.timer = setInterval(() => { if (state.view === "overview" && document.visibilityState === "visible" && !state.controller?.signal.aborted) load(); }, 15000);
}

document.querySelectorAll("[data-nav]").forEach(link => link.addEventListener("click", event => { event.preventDefault(); navigate(link.href); }));
document.querySelector("#refresh").addEventListener("click", () => load(true));
document.querySelector("#filters").addEventListener("submit", event => {
  event.preventDefault(); const data = new FormData(event.currentTarget), params = new URLSearchParams(); const range = rangeParams(data.get("preset")); params.set("from", range.from); params.set("to", range.to);
  for (const key of ["status", "profile", "model", "provider"]) if (data.get(key)) params.set(key, data.get(key)); params.set("view", state.view === "detail" ? "requests" : state.view); navigate(`/admin?${params}`);
});
document.querySelector("#unlock-form").addEventListener("submit", event => { event.preventDefault(); const input = event.currentTarget.elements.token; state.token = input.value; input.value = ""; dialog.close(); load(true); });
window.addEventListener("popstate", () => { readLocation(); state.last = null; load(true); });
window.addEventListener("dashboard:navigate", () => { state.last = null; load(true); });
document.addEventListener("visibilitychange", schedule);

readLocation(); schedule(); load(true);
