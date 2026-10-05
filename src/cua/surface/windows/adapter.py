"""``WindowsSurface``: the ``Surface`` protocol over a Windows application.

The capability, the replay engine, the policy and the recorder do not change
for it; they see observations, refs, ladders and conditions, as on the web.
What this adapter decides:

* **The entry.** ``Navigate`` to the tenant's ``uia://<app>`` location starts
  the application fresh (the tenant says how, ``tenants/<id>.yaml``), stopping
  any instance this surface started before. A location that is not this
  tenant's application is refused. ``?inject=<fault>`` passes a test
  deployment its fault, as the mock app's query string does.
* **Dialogs.** A message box blocks its application's window until answered,
  like a browser ``confirm``. So it is answered as soon as the surface sees it,
  after every action and before every look: as declared (``expect_dialog``),
  or else dismissed, and either way reported on the action and the next
  observation. It never appears as screen content.
* **Refs** are minted per observation and never reused, and only the current
  observation's refs can be acted on (``StaleRefError``), exactly as on the
  web. The element behind a ref is re-read just before acting
  (``locator.confirm``).
* **What it cannot do** it refuses rather than approximates. There is no
  screenshot (a mask could not be painted in before the capture), no session
  to hand a person, no egress guard: ``WINDOWS_UIA`` does not claim those
  features, so replay refuses any run that needs them before the application
  starts, and a direct call raises ``SurfaceError`` here.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from types import TracebackType
from typing import Any, ClassVar, Self
from urllib.parse import parse_qs, urlsplit

from cua.surface import locators as loc
from cua.surface.adapters import WINDOWS_UIA
from cua.surface.conditions import Condition
from cua.surface.features import SurfaceDescriptor
from cua.surface.protocol import (
    DEFAULT_CONFIG,
    Action,
    ActionFailed,
    ActionResult,
    Click,
    ClickPoint,
    ConditionTimeout,
    DialogEvent,
    DialogKind,
    ExpectDialog,
    Navigate,
    Node,
    Observation,
    Press,
    ReadText,
    RecordingEnv,
    SelectOption,
    SessionHandle,
    StaleRefError,
    Surface,
    SurfaceConfig,
    SurfaceError,
    TypeText,
)
from cua.surface.windows import actions, locator, uia
from cua.surface.windows.conditions import DesktopEvaluator
from cua.surface.windows.perception import BUTTON, TEXT, RawElement, perceive, signature
from cua.tenant import DesktopApp, Tenant

START_TIMEOUT_S = 30.0
"""How long a started application has to show its main window. PowerShell and
WinForms take a few seconds from cold."""
POLL_INTERVAL_S = 0.15
AFTER_ACTION_S = 0.2
"""How long after an action the surface looks for a dialog it raised. A
message box opened by a click handler is up well within this."""
DIALOG_CLOSE_S = 3.0

ACCEPT_BUTTONS = ("OK", "Yes", "Retry")
DISMISS_BUTTONS = ("Cancel", "No", "Close")
"""A dismissed confirm is its Cancel or No, which commits nothing; an alert
has only OK, which is the only answer there is (``DialogEvent``)."""


class WindowsSurface:
    """A ``Surface`` over one application's main window."""

    DESCRIPTOR: ClassVar[SurfaceDescriptor] = WINDOWS_UIA

    def __init__(
        self,
        app: DesktopApp,
        *,
        base_url: str,
        config: SurfaceConfig | None = None,
        client: uia.Client | None = None,
    ) -> None:
        self._app = app
        self._base = base_url.rstrip("/")
        self._name = urlsplit(self._base).netloc
        self._config = config or DEFAULT_CONFIG
        self._evaluator = DesktopEvaluator(self._config)
        self._client = client or uia.Client()
        self._process: subprocess.Popen[bytes] | None = None
        self._observation: Observation | None = None
        self._handles: dict[str, RawElement] = {}
        self._next_ref = 1
        self._expected: ExpectDialog | None = None
        self._dialogs: list[DialogEvent] = []

    @classmethod
    @contextmanager
    def launch(cls, tenant: Tenant) -> Iterator[WindowsSurface]:
        """A surface for this tenant's application. The application itself
        starts at the entry (``Navigate``), and stops when this closes."""
        if tenant.desktop is None:
            raise SurfaceError(f"tenant {tenant.id!r} has no desktop application")
        surface = cls(tenant.desktop, base_url=tenant.base_url)
        try:
            yield surface
        finally:
            surface.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Stop the application this surface started. Never anything else."""
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    # -- the protocol ------------------------------------------------------------

    @property
    def descriptor(self) -> SurfaceDescriptor:
        return self.DESCRIPTOR

    @property
    def evaluator(self) -> DesktopEvaluator:
        return self._evaluator

    def observe(self, *, screenshot: bool = False, masks: Sequence[loc.Ladder] = ()) -> Observation:
        if screenshot:
            raise SurfaceError(
                f"{self.DESCRIPTOR.name} takes no screenshots: a mask cannot be painted in "
                "before the capture (run without screenshots)"
            )
        self._answer_dialogs()
        window = self._snapshot()
        seen = perceive(
            window,
            app=self._name,
            start_index=self._next_ref,
            dialogs=self._dialogs,
            config=self._config,
        )
        self._next_ref += len(seen.observation.nodes)
        self._dialogs = []
        self._observation, self._handles = seen.observation, seen.handles
        return seen.observation

    def act(self, action: Action) -> ActionResult:
        started = time.monotonic()
        if isinstance(action, Navigate):
            self._start(action.url)
            return ActionResult(action="navigate", duration_ms=_ms_since(started))
        if isinstance(action, ClickPoint):
            # Not a pointer surface (``WINDOWS_UIA`` does not claim it), and a
            # run that needs one is refused before it starts.
            raise ActionFailed("windows-uia acts on controls, not on points of the screen")
        text: str | None = None
        if isinstance(action, Press):
            target = self._element(action.ref) if action.ref else None
            actions.press(target.hwnd if target and target.hwnd else self._main_hwnd(), action.key)
        else:
            node = self._node(action.ref)
            element = self._element(action.ref)
            if isinstance(action, Click):
                actions.click(self._client, node, element)
            elif isinstance(action, TypeText):
                actions.type_text(self._client, node, element, action.text, action.clear)
            elif isinstance(action, SelectOption):
                actions.select(node, element, action.value)
            elif isinstance(action, ReadText):
                text = actions.read(self._client, node, element)
        raised: list[DialogEvent] = []
        if not isinstance(action, ReadText):
            time.sleep(AFTER_ACTION_S)
            raised = self._answer_dialogs()
        return ActionResult(
            action=action.action,
            ref=getattr(action, "ref", None),
            text=text,
            duration_ms=_ms_since(started),
            dialogs=raised,
        )

    def resolve(
        self,
        ladder: loc.Ladder,
        *,
        observation: Observation | None = None,
        recording_env: RecordingEnv | None = None,
    ) -> loc.LadderOutcome:
        target = observation or self._observation or self.observe()
        return loc.resolve_ladder(ladder, target, recording_env=recording_env, config=self._config)

    def wait_for(self, condition: Condition, timeout_s: float) -> Observation:
        deadline = time.monotonic() + timeout_s
        while True:
            observation = self.observe()
            if self._evaluator.evaluate(condition, observation):
                return observation
            if time.monotonic() >= deadline:
                raise ConditionTimeout(condition, timeout_s, observation)
            time.sleep(POLL_INTERVAL_S)

    def evaluate(
        self, condition: Condition, *, outputs: Mapping[str, object] | None = None
    ) -> bool:
        return self._evaluator.evaluate(condition, self.observe(), outputs=outputs)

    def settle(self, timeout_s: float) -> bool:
        """The window stopped changing: the same tree on two looks in a row.
        UIA has no single idle signal, so stillness is the proxy."""
        deadline = time.monotonic() + timeout_s
        previous: tuple[object, ...] | None = None
        while True:
            self._answer_dialogs()
            current = signature(self._snapshot())
            if current == previous:
                return True
            if time.monotonic() >= deadline:
                return False
            previous = current
            time.sleep(POLL_INTERVAL_S)

    def idle(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._answer_dialogs()
            time.sleep(min(POLL_INTERVAL_S, max(0.0, deadline - time.monotonic())))

    def expose(self) -> SessionHandle:
        raise SurfaceError(
            f"{self.DESCRIPTOR.name} has no session a person could attach to (session_handoff)"
        )

    def expect_dialog(self, expectation: ExpectDialog) -> None:
        self._expected = expectation

    # -- starting the application ------------------------------------------------

    def _start(self, url: str) -> None:
        parts = urlsplit(url)
        if f"{parts.scheme}://{parts.netloc}" != self._base:
            raise ActionFailed(f"{url} is not this tenant's application ({self._base})")
        command = list(self._app.launch)
        inject = parse_qs(parts.query).get("inject", [""])[0]
        if inject:
            if self._app.inject_flag is None:
                raise ActionFailed(f"{self._base} takes no injected faults")
            command += [self._app.inject_flag, inject]
        self.close()  # a fresh session, as a web run starts from the sign-on page
        self._process = subprocess.Popen(
            command,
            cwd=self._app.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._observation, self._handles = None, {}
        deadline = time.monotonic() + START_TIMEOUT_S
        while self._main() is None:
            if self._process.poll() is not None:
                error = (self._process.stderr.read() if self._process.stderr else b"").decode(
                    errors="replace"
                )
                raise SurfaceError(f"{self._name} exited on start: {error.strip()[:500]}")
            if time.monotonic() >= deadline:
                raise SurfaceError(f"{self._name} showed no window in {START_TIMEOUT_S:.0f}s")
            time.sleep(POLL_INTERVAL_S)
        self.settle(START_TIMEOUT_S)

    # -- reading -----------------------------------------------------------------

    def _main(self) -> Any | None:
        if self._process is None:
            return None
        return self._client.main_window(self._process.pid)

    def _main_hwnd(self) -> int:
        window = self._main()
        if window is None:
            raise SurfaceError(f"{self._name} has no window")
        return int(window.CurrentNativeWindowHandle)

    def _snapshot(self) -> RawElement:
        window = self._main()
        if window is None:
            if self._process is None:
                raise SurfaceError("no application is running: navigate to its location first")
            raise SurfaceError(f"{self._name}'s window is gone")
        try:
            return self._client.snapshot(window)
        except uia.COMError as exc:
            raise SurfaceError(f"{self._name}'s window could not be read: {exc}") from None

    def _node(self, ref: str | None) -> Node:
        if ref is None or self._observation is None:
            raise StaleRefError(f"no node {ref!r}: nothing has been observed")
        return self._observation.node(ref)

    def _element(self, ref: str | None) -> RawElement:
        node = self._node(ref)
        window = self._main()
        if window is None:
            raise SurfaceError(f"{self._name}'s window is gone")
        rect = window.CurrentBoundingRectangle
        return locator.confirm(
            node,
            self._handles[node.ref],
            lambda raw: self._client.current(raw.handle),
            origin=(float(rect.left), float(rect.top)),
        )

    # -- dialogs -----------------------------------------------------------------

    def _answer_dialogs(self) -> list[DialogEvent]:
        """Answer every message box up now, and report each one."""
        if self._process is None:
            return []
        answered: list[DialogEvent] = []
        for dialog in self._client.dialogs_of(self._process.pid):
            try:
                raw = self._client.snapshot(dialog)
            except uia.COMError:
                continue  # closed between finding and reading
            answered.append(self._answer(raw))
        self._dialogs.extend(answered)
        return answered

    def _answer(self, dialog: RawElement) -> DialogEvent:
        buttons = {
            e.name.replace("&", ""): e.hwnd
            for e in _walk(dialog)
            if e.control_type == BUTTON and e.hwnd and e.name
        }
        texts = [e.name for e in _walk(dialog) if e.control_type == TEXT and e.name]
        message = " ".join(texts).strip()
        kind: DialogKind = "confirm" if any(b in buttons for b in ("Cancel", "No")) else "alert"
        expected = self._expected
        declared = expected is not None and (
            expected.message_contains is None or expected.message_contains in message
        )
        accept = declared and expected is not None and expected.accept
        preference = ACCEPT_BUTTONS if accept else (*DISMISS_BUTTONS, *ACCEPT_BUTTONS)
        chosen = next((b for b in preference if b in buttons), None)
        if chosen is None:
            raise SurfaceError(f"a dialog ({message!r}) has no button this surface can answer")
        uia.post_click(buttons[chosen])
        if declared:
            self._expected = None
        self._wait_closed(dialog)
        return DialogEvent(
            kind=kind,
            message=message,
            answer="accepted" if accept else "dismissed",
            expected=declared,
            location=self._observation.location if self._observation else self._base,
        )

    def _wait_closed(self, dialog: RawElement) -> None:
        deadline = time.monotonic() + DIALOG_CLOSE_S
        while time.monotonic() < deadline:
            try:
                dialog.handle.CurrentName  # type: ignore[attr-defined]  # noqa: B018
            except uia.COMError:
                return
            if self._process is None or dialog.hwnd not in {
                h for h, _ in uia.top_windows(self._process.pid)
            }:
                return
            time.sleep(0.05)
        raise SurfaceError("a dialog did not close when it was answered")


def _walk(element: RawElement) -> Iterator[RawElement]:
    yield element
    for child in element.children:
        yield from _walk(child)


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _implements_the_protocol(surface: WindowsSurface) -> Surface:
    """Static proof, checked by mypy, that this class satisfies ``Surface``."""
    return surface
