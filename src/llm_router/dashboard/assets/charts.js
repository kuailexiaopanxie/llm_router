import { node, time } from "./format.js";

const NS = "http://www.w3.org/2000/svg";
export function lineChart(points, field = "requests") {
  const frame = node("figure", "chart"); frame.setAttribute("aria-label", `${field} time series with ${points.length} observed buckets`);
  if (!points.length) { frame.append(node("div", "empty", "No observed buckets in this range")); return frame; }
  const svg = document.createElementNS(NS, "svg"); svg.setAttribute("viewBox", "0 0 1000 180"); svg.setAttribute("role", "img");
  const values = points.map(point => Number(point[field] || 0)); const maximum = Math.max(1, ...values);
  for (let line = 0; line <= 4; line += 1) { const y = 10 + line * 40; const grid = document.createElementNS(NS, "line"); grid.setAttribute("x1", "0"); grid.setAttribute("x2", "1000"); grid.setAttribute("y1", y); grid.setAttribute("y2", y); grid.setAttribute("class", "chart-grid"); svg.append(grid); }
  const path = document.createElementNS(NS, "path");
  path.setAttribute("d", values.map((value, index) => `${index ? "L" : "M"}${(index / Math.max(1, values.length - 1)) * 990 + 5},${170 - (value / maximum) * 155}`).join(" ")); path.setAttribute("class", "chart-line"); svg.append(path);
  values.forEach((value, index) => { const dot = document.createElementNS(NS, "circle"); dot.setAttribute("cx", String((index / Math.max(1, values.length - 1)) * 990 + 5)); dot.setAttribute("cy", String(170 - (value / maximum) * 155)); dot.setAttribute("r", "3"); dot.setAttribute("class", "chart-dot"); dot.setAttribute("tabindex", "0"); const title = document.createElementNS(NS, "title"); title.textContent = `${time(points[index].bucket_start)}: ${value}`; dot.append(title); svg.append(dot); });
  frame.append(svg); return frame;
}

export function traceWaterfall(spans) {
  const frame = node("div", "trace"); if (!spans.length) return frame;
  const starts = spans.map(span => Date.parse(span.started_at)); const rootStart = Math.min(...starts); const end = Math.max(...spans.map((span, i) => starts[i] + span.duration_ms)); const range = Math.max(1, end - rootStart);
  for (const span of spans) { const row = node("div", "trace-row"); row.append(node("div", "mono", span.name)); const track = node("div", "trace-track"); const bar = node("div", "trace-span"); bar.style.left = `${((Date.parse(span.started_at) - rootStart) / range) * 100}%`; bar.style.width = `${Math.max(.3, (span.duration_ms / range) * 100)}%`; bar.title = `${span.duration_ms} ms`; track.append(bar); row.append(track, node("div", "", `${Math.round(span.duration_ms)} ms`)); frame.append(row); }
  return frame;
}
