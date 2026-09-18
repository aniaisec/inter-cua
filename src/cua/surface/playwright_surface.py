"""The web implementation of ``Surface``, over Playwright.

Everything Playwright-shaped in the system lives here. The rest of ``src/cua``
sees observations, refs, ladders and conditions.

Three things this module is careful about, each earned from the target app:

* **Frames.** A ``<frameset>`` has no body of its own and its children attach
  after the parent document loads. Perception snapshots every frame that has a
  body, and waits for the panes to arrive rather than reporting an empty screen.
* **Refs.** A ref is turned back into something clickable by role and position
  in the accessibility tree — never a CSS selector. Immediately before acting,
  the element is re-snapshotted and its role and name are checked against the
  node the ref was minted for. If the page re-rendered underneath us, that is a
  ``PerceptionDrift`` fault rather than a click on whatever moved into place.
* **Redaction.** Masks are applied by the screenshot call itself, so a password
  field is never captured and then cleaned up afterwards.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

from playwright.sync_api import Browser, Frame, Locator, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Response as PlaywrightResponse

from cua.surface import a11y
from cua.surface import conditions as cond
from cua.surface import locators as loc
from cua.surface.evaluators import WebEvaluator
from cua.surface.protocol import (
    ANCHOR_ROLES,
    CONTROL_ROLES,
    INTERACTIVE_ROLES,
    TOP_FRAME,
    Action,
    ActionFailed,
    ActionResult,
    Click,
    ConditionTimeout,
    Drag,
    FrameInfo,
    Hover,
    Navigate,
    Node,
    Observation,
    PerceptionDrift,
    Press,
    ReadText,
    RecordingEnv,
    Rect,
    SelectOption,
    SessionHandle,
    StaleRefError,
    Surface,
    SurfaceError,
    SurfaceConfig,
    TypeText,
    Viewport,
)

DEFAULT_VIEWPORT = Viewport(w=1280, h=800)
SNAPSHOT_TIMEOUT_MS = 5_000
ACTION_TIMEOUT_MS = 10_000
FRAME_SETTLE_S = 5.0
POLL_INTERVAL_S = 0.15

GEOMETRY_ROLES: frozenset[str] = INTERACTIVE_ROLES | ANCHOR_ROLES
"""Which nodes get measured. Controls need boxes to be clicked by pixel and to
be masked; anchors need them so a label can be tied to the control beside it.
Measuring the containers as well would double the round trips and answer no
question anyone asks."""

MASK_COLOR = "#101010"


class PlaywrightSurface:
    """A ``Surface`` over one Playwright page.

    Construct it around a page you already have (the tests do), or use
    ``launch`` to get one with a debugging endpoint attached so the session can
    be handed to a human later.
    """

    def __init__(
        self,
        page: Page,
        *,
        cdp_url: str | None = None,
        viewport: Viewport | None = None,
        owns: tuple[Browser, Playwright] | None = None,
        config: SurfaceConfig | None = None,
    ) -> None:
        self._page = page
        self._cdp_url = cdp_url
        self._viewport = viewport or _viewport_of(page)
        self._owns = owns
        self._config = config or SurfaceConfig()
        self._observation: Observation | None = None
        self._next_ref = 1
        self._statuses: dict[str, int] = {}
        page.on("response", self._record_status)

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def launch(
        cls,
        *,
        headed: bool | None = None,
        viewport: Viewport | None = None,
        cdp_port: int | None = None,
    ) -> Self:
        """Start a browser this surface owns.

        The debugging port is opened even for unattended runs: whether a run
        will need a human is not known until it gets stuck, and a session that
        cannot be attached to cannot be handed over.
        """
        view = viewport or DEFAULT_VIEWPORT
        port = cdp_port if cdp_port is not None else _free_port()
        playwright = sync_playwright().start()
        try:
            browser = playwright.chromium.launch(
                headless=not (headed if headed is not None else _headed_from_env()),
                args=[f"--remote-debugging-port={port}"],
            )
            context = browser.new_context(viewport={"width": view.w, "height": view.h})
            page = context.new_page()
        except BaseException:
            playwright.stop()
            raise
        return cls(
            page,
            cdp_url=f"http://127.0.0.1:{port}",
            viewport=view,
            owns=(browser, playwright),
        )

    def close(self) -> None:
        if self._owns is None:
            return
        browser, playwright = self._owns
        self._owns = None
        browser.close()
        playwright.stop()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def page(self) -> Page:
        """The live page, for the handoff and evidence layers. Not for locating."""
        return self._page

    def expose(self) -> SessionHandle:
        if self._cdp_url is None:
            raise SurfaceError("this session has no debugging endpoint; it cannot be handed over")
        return SessionHandle(
            cdp_url=self._cdp_url, page_url=self._page.url, viewport=self._viewport
        )

    # -- perception --------------------------------------------------------

    def observe(self, *, screenshot: bool = False, masks: Sequence[loc.Ladder] = ()) -> Observation:
        """Look at the target, minting a fresh set of refs.

        Refs are never reused across observations. Numbering each observation
        from ``n1`` would be tidier to read and quietly dangerous: a ref held
        over from an earlier screen would still be found, pointing at whatever
        node had since taken that position. Running the counter forward instead
        turns that mistake into ``StaleRefError`` every time.
        """
        self._settle_frames()

        nodes: list[Node] = []
        frames: list[FrameInfo] = []
        for frame in self._page.frames:
            info = FrameInfo(
                name=frame.name,
                url=frame.url,
                status=self._statuses.get(frame.url),
            )
            frames.append(info)
            nodes.extend(self._nodes_of(frame, start_index=self._next_ref + len(nodes)))
        self._next_ref += len(nodes)

        observation = Observation(
            observed_at=datetime.now(UTC),
            location=self._page.url,
            title=_title_of(self._page),
            viewport=self._current_viewport(),
            frames=frames,
            nodes=nodes,
        )
        if not screenshot:
            self._observation = observation
            return observation

        png = self._screenshot(observation, masks)
        observation = observation.model_copy(update={"screenshot_png": png})
        self._observation = observation
        return observation

    def _nodes_of(self, frame: Frame, *, start_index: int) -> list[Node]:
        """Snapshot one frame, or nothing if it has no body to snapshot.

        The frameset document itself has no body, and a frame can detach while
        being read — neither is an error, both mean "this frame contributes no
        nodes to this observation".
        """
        try:
            if frame.is_detached() or frame.locator("body").count() == 0:
                return []
            snapshot = frame.locator("body").aria_snapshot(timeout=SNAPSHOT_TIMEOUT_MS)
        except PlaywrightError:
            return []

        nodes = a11y.flatten(
            a11y.parse_snapshot(snapshot), frame=frame.name, start_index=start_index
        )
        return a11y.with_geometry(nodes, self._measure(frame, nodes), self._config)

    def _measure(self, frame: Frame, nodes: list[Node]) -> dict[str, Rect | None]:
        boxes: dict[str, Rect | None] = {}
        for node in nodes:
            if node.role not in GEOMETRY_ROLES:
                continue
            try:
                box = self._locator_for(frame, node).bounding_box(timeout=SNAPSHOT_TIMEOUT_MS)
            except PlaywrightError:
                box = None
            boxes[node.ref] = (
                Rect(x=box["x"], y=box["y"], w=box["width"], h=box["height"]) if box else None
            )
        return boxes

    def _settle_frames(self) -> None:
        """Wait for a frameset's panes before reporting what is on screen.

        Child frames attach after the parent document loads. Observing in that
        gap yields a screen with nothing on it, which reads exactly like a
        locator failure and is not one.
        """
        if self._page.main_frame.locator("body").count() > 0:
            return
        deadline = _deadline(FRAME_SETTLE_S)
        while _now() < deadline:
            children = [f for f in self._page.frames if f.parent_frame is not None]
            if children and all(f.url.startswith("http") for f in children):
                return
            self._page.wait_for_timeout(POLL_INTERVAL_S * 1000)

    def _screenshot(self, observation: Observation, masks: Sequence[loc.Ladder]) -> bytes:
        targets: list[Locator] = []
        for ladder in masks:
            outcome = loc.resolve_ladder(ladder, observation)
            if isinstance(outcome, loc.Resolved):
                targets.append(self._locator_for(self._frame(outcome.node.frame), outcome.node))
        return self._page.screenshot(mask=targets, mask_color=MASK_COLOR)

    # -- locating ----------------------------------------------------------

    def resolve(
        self,
        ladder: loc.Ladder,
        *,
        observation: Observation | None = None,
        recording_env: RecordingEnv | None = None,
    ) -> loc.LadderOutcome:
        target = observation or self._observation or self.observe()
        return loc.resolve_ladder(ladder, target, recording_env=recording_env, config=self._config)

    def _frame(self, name: str) -> Frame:
        if name == TOP_FRAME:
            return self._page.main_frame
        frame = self._page.frame(name=name)
        if frame is None:
            raise PerceptionDrift(f"frame {name!r} is gone")
        return frame

    def _locator_for(self, frame: Frame, node: Node) -> Locator:
        """Turn a ref back into something clickable, through the a11y tree.

        ``ordinal`` is the node's index among the nodes of its role in its
        frame, and Playwright enumerates roles in the same document order the
        snapshot does, so the two agree. Bare text has no ARIA role of its own,
        so it is addressed by its content instead.
        """
        if node.role == "text":
            return frame.get_by_text(node.name, exact=True).nth(node.ordinal)
        return frame.get_by_role(node.role, include_hidden=False).nth(node.ordinal)  # type: ignore[arg-type]

    def _element_for(self, node: Node) -> Locator:
        """Resolve a ref to an element and prove it is still that node."""
        frame = self._frame(node.frame)
        locator = self._locator_for(frame, node)
        try:
            actual = locator.aria_snapshot(timeout=ACTION_TIMEOUT_MS)
        except PlaywrightError as exc:
            raise PerceptionDrift(f"{node.label} is no longer on the screen") from exc

        parsed = a11y.parse_snapshot(actual)
        if not parsed or parsed[0].role != node.role or parsed[0].name != node.name:
            seen = f"{parsed[0].role} {parsed[0].name!r}" if parsed else "nothing"
            raise PerceptionDrift(f"{node.ref} was {node.label}, now {seen}")
        return locator

    # -- acting ------------------------------------------------------------

    def act(self, action: Action) -> ActionResult:
        started = _now()

        if isinstance(action, Navigate):
            self._page.goto(action.url)
            self._observation = None
            return ActionResult(action="navigate", duration_ms=_ms_since(started))

        if isinstance(action, Press) and action.ref is None:
            self._page.keyboard.press(action.key)
            self._observation = None
            return ActionResult(action="press", duration_ms=_ms_since(started))

        node = self._node_for(action.ref)
        element = self._element_for(node)
        text: str | None = None

        try:
            if isinstance(action, Click):
                element.click(timeout=ACTION_TIMEOUT_MS)
            elif isinstance(action, Hover):
                element.hover(timeout=ACTION_TIMEOUT_MS)
            elif isinstance(action, Drag):
                target_node = self._node_for(action.target_ref)
                target_element = self._element_for(target_node)
                element.drag_to(target_element, timeout=ACTION_TIMEOUT_MS)
            elif isinstance(action, TypeText):
                if action.clear:
                    element.fill(action.text, timeout=ACTION_TIMEOUT_MS)
                else:
                    element.press_sequentially(action.text, timeout=ACTION_TIMEOUT_MS)
            elif isinstance(action, SelectOption):
                element.select_option(action.value, timeout=ACTION_TIMEOUT_MS)
            elif isinstance(action, Press):
                element.press(action.key, timeout=ACTION_TIMEOUT_MS)
            else:
                text = self._read(node, element)
        except PlaywrightError as exc:
            raise ActionFailed(
                f"{action.action} on {node.label} failed: {_first_line(exc)}"
            ) from exc

        if not isinstance(action, ReadText):
            # Anything that touches the page invalidates the refs that
            # described it. The next act() must come from a fresh observation.
            self._observation = None

        return ActionResult(
            action=action.action, ref=node.ref, text=text, duration_ms=_ms_since(started)
        )

    def _read(self, node: Node, element: Locator) -> str:
        """What this node says, read live rather than from the observation.

        Extraction is the one place staleness would be silently wrong: an
        output taken from a tree captured before the screen finished updating
        looks exactly like a correct answer.
        """
        try:
            if node.role in CONTROL_ROLES:
                return element.input_value(timeout=ACTION_TIMEOUT_MS)
            return element.inner_text(timeout=ACTION_TIMEOUT_MS).strip()
        except PlaywrightError:
            return node.text

    def _node_for(self, ref: str | None) -> Node:
        if ref is None:  # pragma: no cover - guarded by the action models
            raise StaleRefError("action needs a ref")
        if self._observation is None:
            raise StaleRefError(f"{ref} came from a spent observation; observe again")
        return self._observation.node(ref)

    # -- conditions --------------------------------------------------------

    def evaluate(
        self,
        condition: cond.Condition,
        *,
        outputs: dict[str, object] | None = None,
        target: str | None = None,
    ) -> bool:
        origin = self._observation
        observation = self.observe()
        here = self._carry(target, observation, origin)
        return WebEvaluator(self._config).evaluate(condition, observation, outputs=outputs, target=here)

    def wait_for(
        self,
        condition: cond.Condition,
        timeout_s: float,
        *,
        outputs: dict[str, object] | None = None,
        target: str | None = None,
    ) -> Observation:
        deadline = _deadline(timeout_s)
        origin = self._observation
        observation = self.observe()
        while True:
            here = self._carry(target, observation, origin)
            if WebEvaluator(self._config).evaluate(condition, observation, outputs=outputs, target=here):
                return observation
            if _now() >= deadline:
                raise ConditionTimeout(condition, timeout_s, observation)
            self._page.wait_for_timeout(POLL_INTERVAL_S * 1000)
            observation = self.observe()

    def _carry(self, ref: str | None, fresh: Observation, origin: Observation | None) -> str | None:
        """Follow a ``{target: self}`` ref into the observation just taken.

        A step types into a field and then asks whether the value took. The
        asking needs a fresh look at the screen, and a fresh look mints fresh
        refs, so the number the caller holds is already spent. The node is
        found again by what identified it in the first place — its frame, role
        and position among that role — rather than by its number.

        A ref that names nothing in either observation is a caller's mistake
        and raises. A node that was there and is now gone is a fact about the
        screen, so the condition simply reports false about it.
        """
        if ref is None:
            return None
        if fresh.find(ref) is not None:
            return ref
        was = origin.find(ref) if origin else None
        if was is None:
            raise StaleRefError(f"{ref} belongs to no observation this surface has taken")
        same = next(
            (
                n
                for n in fresh.nodes
                if n.frame == was.frame
                and n.role == was.role
                and n.ordinal == was.ordinal
                and n.name == was.name
            ),
            None,
        )
        return same.ref if same else None

    # -- internals ---------------------------------------------------------

    def _current_viewport(self) -> Viewport:
        """Read the viewport at observation time, not at construction time.

        A pixel locator is only valid in the window it was recorded in, so the
        window the observation was taken in is part of the observation. A
        surface that reported the size it started with would let a resized
        window keep using rungs that no longer point anywhere.
        """
        size = self._page.viewport_size
        return Viewport(w=size["width"], h=size["height"]) if size else self._viewport

    def _record_status(self, response: PlaywrightResponse) -> None:
        if response.request.is_navigation_request():
            self._statuses[response.url] = response.status


def _implements_the_protocol(surface: PlaywrightSurface) -> Surface:
    """Static proof that this class still satisfies ``Surface``.

    Nothing calls it; mypy checks it. The protocol is what every other layer
    codes against, so a signature drifting apart from it should fail the build
    rather than fail at the one moment a run needs the method.
    """
    return surface


def _viewport_of(page: Page) -> Viewport:
    size = page.viewport_size
    if size is None:
        return DEFAULT_VIEWPORT
    return Viewport(w=size["width"], h=size["height"])


def _title_of(page: Page) -> str:
    try:
        return page.title()
    except PlaywrightError:  # pragma: no cover - page closed mid-observation
        return ""


def _headed_from_env() -> bool:
    return os.environ.get("CUA_HEADED", "0") not in ("", "0", "false", "False")


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _now() -> float:
    import time

    return time.monotonic()


def _deadline(seconds: float) -> float:
    return _now() + seconds


def _ms_since(started: float) -> int:
    return int((_now() - started) * 1000)


def _first_line(exc: PlaywrightError) -> str:
    return str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
