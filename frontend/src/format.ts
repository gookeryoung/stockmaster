/** Formatting helpers. Chinese market convention: red = up, green = down. */

export function formatPct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "--";
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

export function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "--";
  return value.toFixed(digits);
}

/** Compact large numbers for tables (e.g. 1.23亿). */
export function formatCompact(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "--";
  const abs = Math.abs(value);
  if (abs >= 1e8) return `${(value / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `${(value / 1e4).toFixed(2)}万`;
  return value.toFixed(2);
}

export function changeColor(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "text-flat";
  if (value > 0) return "text-up";
  if (value < 0) return "text-down";
  return "text-flat";
}

export function sideColor(side: string): string {
  return side === "buy" ? "text-up" : "text-down";
}

export function todayISO(): string {
  return new Date().toISOString().slice(0, 10);
}

export function yearsAgoISO(years: number): string {
  const d = new Date();
  d.setFullYear(d.getFullYear() - years);
  return d.toISOString().slice(0, 10);
}
