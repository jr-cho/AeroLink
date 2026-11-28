/**
 * Display-only names for the timeline's lanes (R2 CP2 fix 3). No imports, so the tests can load it in Node.
 * A trailing generic airport suffix is shortened to its first word ("Orlando Executive Airport" -> "Orlando Executive",
 * "Lakeland Linder Intl Airport" -> "Lakeland Linder Intl"); a bare " Airport" is dropped. The full name stays in titles.
 */
const TYPED = /\s+(International|Intl|Regional|Executive)\s+Airport$/;

export function shortLaneName(full: string): string {
  const m = TYPED.exec(full);
  if (m) return `${full.slice(0, m.index)} ${m[1]}`;
  return full.replace(/\s+Airport$/, "");
}

/** Visible labels for the lanes shown. Two lanes never show the same label: a clash falls back to the full names. */
export function laneLabels(lanes: { node: string; full: string }[]): Map<string, string> {
  const short = new Map(lanes.map((l) => [l.node, shortLaneName(l.full)]));
  const count = new Map<string, number>();
  for (const s of short.values()) count.set(s, (count.get(s) ?? 0) + 1);
  const out = new Map<string, string>();
  for (const l of lanes) {
    const s = short.get(l.node)!;
    out.set(l.node, (count.get(s) ?? 0) > 1 ? l.full : s);
  }
  return out;
}
