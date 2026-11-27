/**
 * Icons from Lucide (ISC licence, bundled; no CDN, works offline). Import each icon by name so only the used ones
 * ship: `icon(Sparkles)`. Icons are decorative (`aria-hidden`); the text next to them carries the meaning.
 * Rule: no Unicode symbols in user-facing text. Use plain English words, or one of these icons.
 */
import { createElement, Sparkles as SparklesNode, type IconNode } from "lucide";

export { Sparkles, Plane, MapPin, Clock, Radio, TriangleAlert, Check, X, ChevronDown, ChevronUp, Info, QrCode,
  Navigation, Route, Wifi, WifiOff, Hospital, Users, LogIn, RefreshCw } from "lucide";

/** An inline SVG icon sized to the surrounding text (1em). */
export function icon(node: IconNode, cls = ""): SVGElement {
  const el = createElement(node);
  el.setAttribute("width", "1em");
  el.setAttribute("height", "1em");
  el.setAttribute("aria-hidden", "true");
  el.setAttribute("focusable", "false");
  el.setAttribute("class", `icon${cls ? ` ${cls}` : ""}`);
  return el;
}

/** The AI marker: a sparkles icon followed by the word "AI". Replaces the old "✦" glyph on every AI output. */
export function aiMark(): HTMLSpanElement {
  const s = document.createElement("span");
  s.className = "ai-mark";
  s.append(icon(SparklesNode), " AI");
  return s;
}
