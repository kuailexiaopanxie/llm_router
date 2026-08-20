const nowIso = () => new Date().toISOString();

export const state = {
  token: "", controller: null, timer: null, stale: false, last: null,
  view: "overview", cursor: "", filters: new URLSearchParams(),
};

export function readLocation() {
  const path = location.pathname;
  if (path.startsWith("/admin/requests/")) state.view = "detail";
  else state.view = new URLSearchParams(location.search).get("view") === "requests" ? "requests" : "overview";
  state.filters = new URLSearchParams(location.search);
  state.cursor = state.filters.get("cursor") || "";
}

export function rangeParams(hours) {
  const end = new Date();
  const start = new Date(end.getTime() - Number(hours) * 3600000);
  return { from: start.toISOString(), to: end.toISOString() };
}

export function apiParams() {
  const result = new URLSearchParams(state.filters);
  result.delete("view");
  if (!result.has("from") || !result.has("to")) {
    const range = rangeParams(24);
    result.set("from", range.from); result.set("to", range.to);
  }
  return result;
}

export function navigate(url) {
  history.pushState({}, "", url);
  readLocation();
  window.dispatchEvent(new CustomEvent("dashboard:navigate"));
}

export function cancelPending() {
  if (state.controller) state.controller.abort();
  state.controller = new AbortController();
  return state.controller.signal;
}

export function markStale() { state.stale = true; }
export function markFresh(payload) { state.stale = false; state.last = payload; }
export function generatedNow() { return nowIso(); }
