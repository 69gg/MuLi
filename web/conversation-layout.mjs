// A local layout preference, independent of account, model and conversation data.
const WIDTH_KEY = "muli.conversation-width";
const DEFAULT_WIDTH = 920;
const MIN_WIDTH = 600;
const MAX_WIDTH = 1440;
const PAGE_GUTTER = 30;
const MOBILE_GUTTER = 20;
const MOBILE_WIDTH = 760;
const KEY_STEP = 24;

export function initializeConversationWidth(root, { storage, host = window } = {}) {
  let preferred = DEFAULT_WIDTH;
  try {
    const value = JSON.parse((storage ?? globalThis.localStorage).getItem(WIDTH_KEY));
    if (typeof value === "number" && Number.isFinite(value) && value >= MIN_WIDTH && value <= MAX_WIDTH) preferred = value;
  } catch { /* A blocked or invalid preference must not prevent conversation use. */ }
  const edges = [...root.querySelectorAll("[data-conversation-edge]")];
  const scroll = root.querySelector("#conversation-scroll");
  let drag = null;
  const bounds = () => {
    const width = scroll?.clientWidth || root.clientWidth;
    const gutter = root.clientWidth > MOBILE_WIDTH ? PAGE_GUTTER : MOBILE_GUTTER;
    const maximum = Math.max(0, Math.min(MAX_WIDTH, width - gutter * 2));
    return { maximum, minimum: Math.min(MIN_WIDTH, maximum), enabled: root.clientWidth > MOBILE_WIDTH && maximum >= MIN_WIDTH };
  };
  const clamp = (value) => {
    const { minimum, maximum } = bounds();
    return Math.max(minimum, Math.min(maximum, value));
  };
  const apply = () => {
    const { enabled, minimum, maximum } = bounds();
    const width = clamp(preferred);
    root.style.setProperty("--conversation-width", `${width}px`);
    for (const edge of edges) {
      edge.hidden = !enabled;
      edge.tabIndex = enabled ? 0 : -1;
      edge.setAttribute("aria-valuemin", String(Math.round(minimum)));
      edge.setAttribute("aria-valuemax", String(Math.round(maximum)));
      edge.setAttribute("aria-valuenow", String(Math.round(width)));
      edge.setAttribute("aria-valuetext", `对话宽度 ${Math.round(width)} 像素`);
    }
    return width;
  };
  const save = () => {
    try { (storage ?? globalThis.localStorage).setItem(WIDTH_KEY, JSON.stringify(preferred)); } catch { /* Layout remains usable without persistent storage. */ }
  };
  const finish = (cancelled = false) => {
    if (!drag) return;
    const previous = drag;
    drag = null;
    if (cancelled) preferred = previous.preferred;
    else save();
    root.classList.remove("is-resizing-conversation");
    apply();
    if (previous.edge.hasPointerCapture?.(previous.pointer)) previous.edge.releasePointerCapture(previous.pointer);
  };
  for (const edge of edges) {
    edge.addEventListener("pointerdown", (event) => {
      if (drag || event.button !== 0 || event.isPrimary === false || !bounds().enabled) return;
      event.preventDefault();
      edge.focus({ preventScroll: true });
      drag = { edge, pointer: event.pointerId, x: event.clientX, width: apply(), preferred };
      edge.setPointerCapture(event.pointerId);
      root.classList.add("is-resizing-conversation");
    });
    edge.addEventListener("pointermove", (event) => {
      if (!drag || drag.edge !== edge || drag.pointer !== event.pointerId) return;
      const direction = edge.dataset.conversationEdge === "left" ? -1 : 1;
      preferred = clamp(drag.width + direction * (event.clientX - drag.x) * 2);
      apply();
    });
    edge.addEventListener("pointerup", (event) => { if (drag?.edge === edge && drag.pointer === event.pointerId) finish(); });
    edge.addEventListener("pointercancel", (event) => { if (drag?.edge === edge && drag.pointer === event.pointerId) finish(true); });
    edge.addEventListener("lostpointercapture", (event) => { if (drag?.edge === edge && drag.pointer === event.pointerId) finish(true); });
    edge.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && drag) { event.preventDefault(); finish(true); return; }
      if (!bounds().enabled || drag || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const { minimum, maximum } = bounds();
      const direction = edge.dataset.conversationEdge === "left" ? -1 : 1;
      preferred = event.key === "Home" ? minimum : event.key === "End" ? maximum
        : clamp(apply() + direction * (event.key === "ArrowRight" ? 1 : -1) * KEY_STEP);
      apply(); save();
    });
  }
  const resize = () => { finish(true); apply(); };
  host.addEventListener("resize", resize);
  host.addEventListener("blur", () => finish(true));
  apply();
  return { refresh: apply };
}
