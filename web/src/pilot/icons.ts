/**
 * Icons for the pilot screen, all from Lucide through the shared helper (`../shared/icon.ts`), so there is one icon
 * source and no glyphs pasted into text. Each name maps to a Lucide icon; the button around an icon carries the label.
 */
import { Locate, MapPin, Navigation, Search, ChevronLeft, X } from "lucide";
import { icon as sharedIcon } from "../shared/icon.ts";

const NODES = { navigation: Navigation, locate: Locate, search: Search, back: ChevronLeft, pin: MapPin, close: X } as const;

export type IconName = keyof typeof NODES;

/** A decorative inline SVG of `size` px, optionally rotated (the guidance arrow). */
export function icon(name: IconName, size = 24, rotateDeg = 0): HTMLElement {
  const span = document.createElement("span");
  span.className = "ico";
  span.setAttribute("aria-hidden", "true");
  const svg = sharedIcon(NODES[name]);
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("stroke-width", "2");
  svg.style.transform = `rotate(${rotateDeg}deg)`;
  span.append(svg);
  return span;
}
