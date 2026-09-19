"""Recording what a person does while they hold the controls.

When an operator takes control, the console attaches to the live browser over
its debugging port — a second connection to the very page the run is driving —
and listens: every navigation of every frame, and, through a small script
installed in each document, every click, change and Enter/Tab/Escape. Each one
becomes a line of ``human_actions.jsonl`` in the run directory, tagged with the
request it happened under.

Three deliberate limits:

* **Recorded, not policy-checked.** The person is outside the allowlist by
  design: they are the escalation path for exactly the situations the policy
  and the capability did not foresee. What the system owes them is a faithful
  record, not a veto.
* **No typed values.** A change is recorded as which control and how many
  characters, never what was typed. The console holds no redactor for the
  tenant's secrets, and a person fixing a sign-on types a password.
* **Only what the person did.** A ``change`` event fires when a field loses
  focus, which is the person's first click anywhere — and the value it carries
  may be one the automation typed before it handed over. A field is credited
  to the person only once they have edited it themselves while holding the
  controls. An audit trail that says someone typed something they did not is
  worse than one that says nothing.

Playwright's sync API is bound to the thread that started it, so each capture
runs a connection of its own on a thread of its own, and pumps that
connection's events until it is stopped.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Page, sync_playwright

Sink = Callable[[dict[str, Any]], None]

PUMP_MS = 100
START_TIMEOUT_S = 20.0

_LISTENER = """
(binding) => {
  const flag = "__cua_capture_" + binding;
  if (window[flag]) return;
  window[flag] = true;
  const send = (record) => { try { window[binding](record); } catch (e) {} };
  const text = (el) => ((el && (el.innerText || el.textContent)) || "").trim().slice(0, 60);
  const roleOf = (el, tag, type) => {
    if (tag === "a") return "link";
    if (tag === "button" || (tag === "input" && ["submit", "button", "reset"].includes(type)))
      return "button";
    if (tag === "select") return "combobox";
    if (tag === "input" && type === "checkbox") return "checkbox";
    if (tag === "input" || tag === "textarea") return "textbox";
    return tag;
  };
  const describe = (el) => {
    if (!el || !el.tagName) return {};
    const tag = el.tagName.toLowerCase();
    const type = ((el.getAttribute && el.getAttribute("type")) || "").toLowerCase();
    const role = roleOf(el, tag, type);
    let name = (el.getAttribute && el.getAttribute("aria-label")) || "";
    if (!name && role === "button" && tag === "input") name = el.value || "";
    if (!name && role !== "textbox" && role !== "combobox") name = text(el);
    const cell = el.closest ? el.closest("td") : null;
    const beside = cell && cell.previousElementSibling ? text(cell.previousElementSibling) : "";
    const out = { role, name };
    if (beside) out.near_text = beside;
    if (type === "password") out.password = true;
    return out;
  };
  const control = (el) =>
    (el && el.closest && el.closest("a,button,input,select,textarea,[onclick]")) || el;
  // Fields this person has edited since the listener was installed. A change
  // event on any other field is a value that was already there when they took
  // control - the automation's - surfacing now only because the field lost
  // focus.
  const edited = new WeakSet();
  document.addEventListener("input", (e) => { if (e.target) edited.add(e.target); }, true);
  document.addEventListener("click", (e) => {
    send({ action: "click", target: describe(control(e.target)), x: e.clientX, y: e.clientY });
  }, true);
  document.addEventListener("change", (e) => {
    const el = e.target;
    if (!edited.has(el)) return;
    const tag = el && el.tagName ? el.tagName.toLowerCase() : "";
    send({ action: tag === "select" ? "select" : "type", target: describe(el),
           chars: (el && el.value ? String(el.value).length : 0) });
  }, true);
  document.addEventListener("keydown", (e) => {
    if (["Enter", "Tab", "Escape"].includes(e.key))
      send({ action: "press", key: e.key, target: describe(e.target) });
  }, true);
}
"""


class CaptureError(Exception):
    """The live session could not be attached to."""


class CaptureSession:
    """Attach to one page over CDP and record a person's actions on it."""

    def __init__(self, *, cdp_url: str, target_id: str | None, sink: Sink) -> None:
        self.cdp_url = cdp_url
        self.target_id = target_id
        self.sink = sink
        self._binding = f"__cua_human_{secrets.token_hex(4)}"
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: str | None = None
        self._thread = threading.Thread(target=self._run, name="cua-capture", daemon=True)

    def start(self) -> None:
        """Returns once the listeners are in place, so nothing the person does
        after taking control goes unrecorded."""
        self._thread.start()
        if not self._ready.wait(START_TIMEOUT_S):
            self._stop.set()
            raise CaptureError(f"could not attach to {self.cdp_url} in {START_TIMEOUT_S:g}s")
        if self._error is not None:
            raise CaptureError(self._error)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    @property
    def running(self) -> bool:
        return self._thread.is_alive() and not self._stop.is_set()

    def _run(self) -> None:
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(self.cdp_url)
                try:
                    page = self._page(browser.contexts)
                    self._install(page)
                    self._ready.set()
                    while not self._stop.is_set():
                        try:
                            page.wait_for_timeout(PUMP_MS)
                        except PlaywrightError:
                            break  # the page or the browser went away
                finally:
                    # Disconnects; the browser and its pages stay as they are.
                    browser.close()
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
            self._ready.set()

    def _page(self, contexts: Any) -> Page:
        page = find_page(contexts, self.target_id)
        if page is None:
            raise CaptureError(f"page {self.target_id} is not open in the browser")
        return page

    def _install(self, page: Page) -> None:
        def on_action(source: dict[str, Any], record: dict[str, Any]) -> None:
            frame = source["frame"]
            self.sink({**record, "frame": frame.name or "top", "url": _path_of(frame.url)})

        def on_navigate(frame: Frame) -> None:
            self.sink(
                {"action": "navigate", "frame": frame.name or "top", "url": _path_of(frame.url)}
            )

        page.expose_binding(self._binding, on_action)
        script = f"({_LISTENER})({self._binding!r})"
        page.add_init_script(script)
        for frame in page.frames:
            try:
                frame.evaluate(_LISTENER, self._binding)
            except PlaywrightError:
                continue  # a frameset document, or a frame mid-navigation
        page.on("framenavigated", on_navigate)
        # A listener of our own keeps this connection from dismissing a
        # dialog the person has to answer; the answer is theirs.
        page.on("dialog", lambda dialog: self.sink({"action": "dialog", "kind": dialog.type}))


def find_page(contexts: Any, target_id: str | None) -> Page | None:
    """The page a request names, among every page of a browser attached over
    CDP (``browser.contexts``); the first page if it names none."""
    pages: list[Page] = [p for c in contexts for p in c.pages]
    for page in pages:
        if target_id is None or _target_id(page) == target_id:
            return page
    return None


def _target_id(page: Page) -> str | None:
    try:
        session = page.context.new_cdp_session(page)
        try:
            info = session.send("Target.getTargetInfo")
        finally:
            session.detach()
        return str(info["targetInfo"]["targetId"])
    except PlaywrightError:
        return None


def _path_of(url: str) -> str:
    """Where, without the query string (which is where a value would be)."""
    return url.split("?", 1)[0]
