export function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

export function time(value) {
  if (!value) return "Unknown";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? "Unknown" : new Intl.DateTimeFormat(undefined, { dateStyle: "short", timeStyle: "medium" }).format(parsed);
}

export function ratio(value) {
  if (!value || value.ratio === null || value.ratio === undefined) return { value: "Unknown", detail: `${value?.numerator || 0} / ${value?.denominator || 0}` };
  return { value: new Intl.NumberFormat(undefined, { style: "percent", maximumFractionDigits: 1 }).format(value.ratio), detail: `${value.numerator} / ${value.denominator}${value.unknown ? `, ${value.unknown} unknown` : ""}` };
}

export function duration(value) {
  if (value === null || value === undefined) return "Unknown";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(value < 10000 ? 2 : 1)} s`;
}

export function knownCost(nanos, currency) {
  if (nanos === null || nanos === undefined || !currency) return "Unknown";
  try {
    const raw = BigInt(nanos); const whole = raw / 1000000000n; const fraction = (raw % 1000000000n).toString().padStart(9, "0").replace(/0+$/, "");
    return `${currency} ${whole}${fraction ? `.${fraction}` : ""}`;
  } catch { return "Unknown"; }
}

export function badge(value, kind) { return node("span", `badge ${kind || "gap"}`, value || "Unknown"); }

export function definition(items) {
  const list = node("dl", "definition");
  for (const [label, value, mono] of items) {
    const cell = node("div"); cell.append(node("dt", "", label), node("dd", mono ? "mono" : "", value ?? "Unknown")); list.append(cell);
  }
  return list;
}
