/**
 * Cut-off text opens its full text. A line that does not fit shows "..." from the browser's CSS ellipsis and a chevron
 * at its end; clicking it opens a small panel with the whole text (Esc or the close button closes it). Callsigns are
 * never cut, so they never use this. The chevron only shows while the text really is cut.
 */
import { h } from "../shared/dom.ts";
import { X, icon } from "../shared/icon.ts";
import { ChevronRight } from "lucide";

let panel: HTMLElement | null = null;

/** Close the full-text panel. Returns true when one was open (the caller then stops handling Esc). */
export function closeFullText(): boolean {
  if (!panel) return false;
  panel.remove();
  panel = null;
  return true;
}

/** A small panel beside the sidebar with the whole text, never a modal: the map stays usable. */
export function showFullText(title: string, lines: string[]): void {
  closeFullText();
  const close = h("button", { type: "button", class: "btn ft-close", id: "fulltext-close", "aria-label": "Close", onclick: () => closeFullText() }, icon(X));
  panel = h("section", { class: "fulltext", id: "fulltext", role: "dialog", "aria-label": title },
    h("div", { class: "ft-head" }, h("b", {}, title), close), ...lines.map((l) => h("p", { class: "ft-line" }, l)));
  document.querySelector(".ops")?.append(panel);
  close.focus({ preventScroll: true });
}

/**
 * One line of text that may be cut off. `onOpen` makes the whole line (and its chevron) a button while it is cut;
 * `null` shows only the chevron as a cue, for text inside something that already opens a panel when clicked.
 */
export function cutText(cls: string, text: string, onOpen: (() => void) | null, title?: string, clamped = false): HTMLElement {
  const t = h("span", { class: "cut-text" }, text);
  const chev = onOpen
    ? h("button", { type: "button", class: "cut-chev", "aria-label": "Show full text", hidden: true, onclick: (e: Event) => { e.stopPropagation(); onOpen(); } }, icon(ChevronRight))
    : h("span", { class: "cut-chev", "aria-hidden": "true", hidden: true }, icon(ChevronRight));
  const wrap = h("span", { class: `cut ${cls}`, title: title ?? text }, t, chev);
  const check = () => {
    // one line is cut sideways; a line-clamped block (the timeline lane names) is cut at its height
    // (compared with the whole line, not the text beside the chevron, so showing the chevron never flips the answer)
    const cut = clamped ? t.scrollHeight > t.clientHeight + 1 : t.scrollWidth > wrap.clientWidth + 1;
    chev.hidden = !cut;
    wrap.classList.toggle("is-cut", cut);
  };
  new ResizeObserver(check).observe(t);
  if (onOpen) wrap.addEventListener("click", () => { if (wrap.classList.contains("is-cut")) onOpen(); });
  return wrap;
}
