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
* **Dialogs.** A native ``alert``/``confirm`` is outside the accessibility tree
  and outside the screenshot, and while it is open every further instruction to
  the browser blocks — measured: the click that raised it and the next snapshot
  both time out. It cannot be left for someone to decide later, so it is
  answered the moment it appears: as the capability declared, or else
  dismissed, which for a confirm commits nothing. Either way it is recorded.
  Playwright's own default is to dismiss silently, and a silently cancelled
  irreversible step looks exactly like a successful one.
"""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

from playwright.sync_api import (
    Browser,
    Dialog,
    FloatRect,
    Frame,
    Locator,
    Page,
    Playwright,
    sync_playwright,
)
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Request as PlaywrightRequest
from playwright.sync_api import Response as PlaywrightResponse

from cua.surface import a11y
from cua.surface import conditions as cond
from cua.surface import locators as loc
from cua.surface.evaluators import WebEvaluator
from cua.surface.protocol import (
    ANCHOR_ROLES,
    CONTROL_ROLES,
    DEFAULT_CONFIG,
    INTERACTIVE_ROLES,
    TOP_FRAME,
    Action,
    ActionFailed,
    ActionResult,
    Click,
    ConditionTimeout,
    DialogEvent,
    DialogKind,
    ExpectDialog,
    FrameInfo,
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
    SurfaceConfig,
    SurfaceError,
    TypeText,
    Viewport,
)

DEFAULT_VIEWPORT = Viewport(w=1280, h=800)
SNAPSHOT_TIMEOUT_MS = 5_000
MEASURE_TIMEOUT_MS = 1_000
"""Per-node cap on measuring a box. An element that exists measures at once;
this only bounds the wait on one that vanished mid-read."""
READ_ATTEMPTS = 3
"""How many times a frame is re-read when it navigates while being read."""
ACTION_TIMEOUT_MS = 10_000
FRAME_SETTLE_S = 5.0
SETTLE_TIMEOUT_S = 10.0
NAVIGATION_GRACE_S = 0.5
"""How long after a click or key press a navigation may take to be requested."""
NAVIGATION_POLL_S = 0.02
POLL_INTERVAL_S = 0.15

GEOMETRY_ROLES: frozenset[str] = INTERACTIVE_ROLES | ANCHOR_ROLES
"""Which nodes get measured. Controls need boxes to be clicked by pixel and to
be masked; anchors need them so a label can be tied to the control beside it.
Measuring the containers as well would double the round trips and answer no
question anyone asks."""

MASK_COLOR = "#101010"

DRIFT_TOLERANCE_PX = 8.0
"""How far an unnamed control may have moved between being observed and being
acted on before it is no longer trusted to be the same control. A field that
changed rows on a legacy form moves by a full row height, well past this; a
sub-pixel re-layout does not reach it."""


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
        self._config = config or DEFAULT_CONFIG
        self._evaluator = WebEvaluator(self._config)
        self._observation: Observation | None = None
        self._next_ref = 1
        self._statuses: dict[str, int] = {}
        self._navigations: dict[Frame, int] = {}
        self._expected_dialog: ExpectDialog | None = None
        self._dialogs: list[DialogEvent] = []
        self._pending_documents: set[PlaywrightRequest] = set()
        self._documents_requested = 0
        self._documents_before_act = 0
        self._may_navigate = False
        page.on("request", self._document_requested)
        page.on("requestfinished", self._document_done)
        page.on("requestfailed", self._document_done)
        page.on("response", self._record_status)
        page.on("framenavigated", self._count_navigation)
        # Registering a handler is what stops Playwright dismissing dialogs on
        # its own; from here on every dialog passes through _answer_dialog.
        page.on("dialog", self._answer_dialog)

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

    def close(self, *, interrupted: bool = False) -> None:
        """Shut down the browser this surface launched.

        ``interrupted``: a KeyboardInterrupt landed inside a Playwright call,
        which leaves the sync dispatcher waiting on a reply it will never
        read, so a polite ``browser.close()`` would block forever. Stopping
        the driver alone ends its connection, and the browser goes with it.
        """
        if self._owns is None:
            return
        browser, playwright = self._owns
        self._owns = None
        if not interrupted:
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
        self.close(interrupted=exc_type is not None and issubclass(exc_type, KeyboardInterrupt))

    @property
    def evaluator(self) -> WebEvaluator:
        return self._evaluator

    def idle(self, seconds: float) -> None:
        # The sync API dispatches browser events only while a Playwright call
        # is in progress; waiting through one keeps dialogs answered on time.
        self._page.wait_for_timeout(max(0.0, seconds) * 1000)

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
            nodes.extend(self._nodes_of(frame, start_index=self._next_ref + len(nodes)))
            # Read after the frame, so the URL describes the document the
            # nodes came from rather than the one that was there before.
            frames.append(
                FrameInfo(name=frame.name, url=frame.url, status=self._statuses.get(frame.url))
            )
        self._next_ref += len(nodes)

        observation = Observation(
            observed_at=datetime.now(UTC),
            location=self._page.url,
            title=_title_of(self._page),
            viewport=self._current_viewport(),
            frames=frames,
            nodes=nodes,
            dialogs=self._dialogs,
        )
        self._dialogs = []
        if not screenshot:
            self._observation = observation
            return observation

        png = self._screenshot(observation, masks)
        observation = observation.model_copy(update={"screenshot_png": png})
        self._observation = observation
        return observation

    def _nodes_of(self, frame: Frame, *, start_index: int) -> list[Node]:
        """Read one frame consistently, or report nothing for it.

        A read is a snapshot followed by one measurement per node, and those
        are separate round trips. If the frame navigates in between, the result
        is torn: the old document's tree with the new document's (missing)
        geometry. That is worse than no reading at all, because every condition
        evaluated against it answers about a screen that is already gone. So a
        frame that navigated while being read is read again, and one that is
        still moving after ``READ_ATTEMPTS`` contributes nothing this time —
        the caller is almost always ``wait_for``, which simply looks again.

        The frameset document itself has no body, and a frame can detach while
        being read; neither is an error.
        """
        for _ in range(READ_ATTEMPTS):
            before = self._navigations.get(frame, 0)
            try:
                if frame.is_detached() or frame.locator("body").count() == 0:
                    return []
                snapshot = frame.locator("body").aria_snapshot(timeout=SNAPSHOT_TIMEOUT_MS)
                nodes = a11y.flatten(
                    a11y.parse_snapshot(snapshot), frame=frame.name, start_index=start_index
                )
                boxes = self._measure(frame, nodes)
            except PlaywrightError:
                continue
            if self._navigations.get(frame, 0) == before:
                return a11y.with_geometry(nodes, boxes, self._config)
        return []

    def _measure(self, frame: Frame, nodes: list[Node]) -> dict[str, Rect | None]:
        """Measure every node that needs a box.

        Elements are counted per role first, in one immediate call each. A node
        whose element is not there any more gets no box straight away, instead
        of a per-node wait for an element that is never coming back — ten such
        waits once stretched one observation past a whole ``wait_for``.
        """
        present: dict[str, int] = {}
        boxes: dict[str, Rect | None] = {}
        for node in nodes:
            if node.role not in GEOMETRY_ROLES:
                continue
            key = f"text:{node.name}" if node.role == "text" else node.role
            if key not in present:
                present[key] = _count(_query_for(frame, node))
            if _index_in(node, nodes) >= present[key]:
                boxes[node.ref] = None
                continue
            try:
                locator = self._locator_for(frame, node, nodes)
                box = locator.bounding_box(timeout=MEASURE_TIMEOUT_MS)
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
                targets.append(
                    self._locator_for(
                        self._frame(outcome.node.frame), outcome.node, observation.nodes
                    )
                )
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

    def _locator_for(self, frame: Frame, node: Node, nodes: Sequence[Node]) -> Locator:
        """Turn a ref back into something clickable, through the a11y tree.

        ``ordinal`` is the node's index among the nodes of its role in its
        frame, and Playwright enumerates roles in the same document order the
        snapshot does, so the two agree. Bare text has no ARIA role of its own,
        so it is addressed by its content instead.
        """
        return _query_for(frame, node).nth(_index_in(node, nodes))

    def _element_for(self, node: Node) -> Locator:
        """Resolve a ref to an element and prove it is still that node."""
        frame = self._frame(node.frame)
        assert self._observation is not None
        locator = self._locator_for(frame, node, self._observation.nodes)
        try:
            actual = locator.aria_snapshot(timeout=ACTION_TIMEOUT_MS)
        except PlaywrightError as exc:
            raise PerceptionDrift(f"{node.label} is no longer on the screen") from exc

        parsed = a11y.parse_snapshot(actual)
        if not parsed or parsed[0].role != node.role or parsed[0].name != node.name:
            seen = f"{parsed[0].role} {parsed[0].name!r}" if parsed else "nothing"
            raise PerceptionDrift(f"{node.ref} was {node.label}, now {seen}")
        if not node.name:
            self._check_identity(node, locator)
        return locator

    def _check_identity(self, node: Node, locator: Locator) -> None:
        """Prove an unnamed control is the one that was observed.

        Role and name cannot tell two unnamed textboxes apart, and those are
        the controls this system exists for. Two things are left to check:

        * where it sits — a field inserted above ours shifts every ordinal by
          one, and the element the ref now maps to is a row away;
        * what it sits beside — two fields that swapped rows put the other
          one exactly where ours was, and only the label gives that away.

        Either failing means the screen was re-laid out since it was observed
        and the ref has to be minted again.
        """
        if node.bbox is None:
            return
        try:
            box = locator.bounding_box(timeout=MEASURE_TIMEOUT_MS)
        except PlaywrightError as exc:
            raise PerceptionDrift(f"{node.ref} ({node.label}) cannot be measured") from exc
        if box is None or _moved(node.bbox, box):
            raise PerceptionDrift(f"{node.ref} ({node.label}) is no longer where it was seen")
        if not node.near_text:
            return
        now = Rect(x=box["x"], y=box["y"], w=box["width"], h=box["height"])
        labels = self._frame(node.frame).get_by_text(node.near_text, exact=True)
        found = _count(labels)
        if found == 0:
            return  # the label cannot be measured, so it cannot contradict the ref
        for i in range(found):
            try:
                label = labels.nth(i).bounding_box(timeout=MEASURE_TIMEOUT_MS)
            except PlaywrightError:
                continue
            if label is not None and loc.beside(
                Rect(x=label["x"], y=label["y"], w=label["width"], h=label["height"]),
                now,
                self._config,
            ):
                return
        raise PerceptionDrift(f"{node.ref} ({node.label}) is no longer beside {node.near_text!r}")

    # -- acting ------------------------------------------------------------

    def act(self, action: Action) -> ActionResult:
        """Perform one action.

        A dialog answer declared with ``expect_dialog`` covers this action and
        no other. Left armed, an "accept" meant for one confirm could answer a
        different one three steps later.
        """
        try:
            return self._act(action)
        finally:
            self._expected_dialog = None

    def _act(self, action: Action) -> ActionResult:
        started = _now()

        raised_before = len(self._dialogs)
        # Only these can submit a form or follow a link; settle() waits a
        # moment for the navigation they may have started.
        self._may_navigate = isinstance(action, (Click, Press))
        self._documents_before_act = self._documents_requested

        if isinstance(action, Navigate):
            self._page.goto(action.url)
            self._observation = None
            return ActionResult(
                action="navigate",
                duration_ms=_ms_since(started),
                dialogs=self._dialogs[raised_before:],
            )

        if isinstance(action, Press) and action.ref is None:
            self._page.keyboard.press(action.key)
            self._observation = None
            return ActionResult(
                action="press",
                duration_ms=_ms_since(started),
                dialogs=self._dialogs[raised_before:],
            )

        node = self._node_for(action.ref)
        element = self._element_for(node)
        text: str | None = None

        try:
            if isinstance(action, Click):
                element.click(timeout=ACTION_TIMEOUT_MS)
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
            action=action.action,
            ref=node.ref,
            text=text,
            duration_ms=_ms_since(started),
            dialogs=self._dialogs[raised_before:],
        )

    # -- dialogs -----------------------------------------------------------

    def expect_dialog(self, expectation: ExpectDialog) -> None:
        self._expected_dialog = expectation

    def _answer_dialog(self, dialog: Dialog) -> None:
        """Answer a native dialog the moment it opens, and record the answer.

        A declaration answers exactly one dialog, the next one. A dialog whose
        text does not match is not the one the capability decided about: it
        gets the conservative answer, is reported as unexpected, and uses up
        the declaration all the same.
        """
        expected = self._expected_dialog
        matches = expected is not None and (
            expected.message_contains is None
            or a11y.normalize(expected.message_contains) in a11y.normalize(dialog.message)
        )
        accept = expected is not None and matches and expected.accept
        try:
            if accept and expected is not None:
                dialog.accept(expected.prompt_text or "")
            else:
                dialog.dismiss()
        except PlaywrightError:  # pragma: no cover - page closed under the dialog
            pass
        self._expected_dialog = None
        self._dialogs.append(
            DialogEvent(
                kind=_dialog_kind(dialog.type),
                message=dialog.message,
                answer="accepted" if accept else "dismissed",
                expected=matches,
                location=self._page.url,
            )
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
        return self._evaluator.evaluate(condition, observation, outputs=outputs, target=here)

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
            if self._evaluator.evaluate(condition, observation, outputs=outputs, target=here):
                return observation
            if _now() >= deadline:
                raise ConditionTimeout(condition, timeout_s, observation)
            self._page.wait_for_timeout(POLL_INTERVAL_S * 1000)
            observation = self.observe()

    def settle(self, timeout_s: float = SETTLE_TIMEOUT_S) -> bool:
        """Wait until no document is loading, in any frame.

        A document request in flight is the signal, not a quiet period: a
        server that takes four seconds to answer is perfectly quiet for all
        four, and a loop that observed during them would decide its next move
        against the screen it just left.

        A click or key press may be followed by a navigation that has not been
        *requested* yet when the action returns — a form submission in a frame
        is dispatched a moment later (measured: about one run in four on the
        ``slow_load`` search). So after one of those, this first waits up to
        ``NAVIGATION_GRACE_S`` for a document request to appear, and only then
        concludes that nothing is loading.
        """
        deadline = _deadline(timeout_s)
        if self._may_navigate:
            grace = _deadline(NAVIGATION_GRACE_S)
            while self._documents_requested == self._documents_before_act and _now() < grace:
                self._page.wait_for_timeout(NAVIGATION_POLL_S * 1000)
            self._may_navigate = False
        while self._pending_documents:
            if _now() >= deadline:
                return False
            self._page.wait_for_timeout(POLL_INTERVAL_S * 1000)
        for frame in self._page.frames:
            remaining_ms = max(1.0, (deadline - _now()) * 1000)
            try:
                frame.wait_for_load_state("load", timeout=remaining_ms)
            except PlaywrightError:
                if frame.is_detached():
                    continue
                return False
        self._settle_frames()
        return not self._pending_documents

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
        # A redirect is not the status of any document: a POST answered 303
        # would otherwise overwrite the 200 of the page at the same URL.
        if response.request.is_navigation_request() and not 300 <= response.status < 400:
            self._statuses[response.url] = response.status

    def _document_requested(self, request: PlaywrightRequest) -> None:
        if request.is_navigation_request():
            self._pending_documents.add(request)
            self._documents_requested += 1

    def _document_done(self, request: PlaywrightRequest) -> None:
        self._pending_documents.discard(request)

    def _count_navigation(self, frame: Frame) -> None:
        self._navigations[frame] = self._navigations.get(frame, 0) + 1


def _implements_the_protocol(surface: PlaywrightSurface) -> Surface:
    """Static proof that this class still satisfies ``Surface``.

    Nothing calls it; mypy checks it. The protocol is what every other layer
    codes against, so a signature drifting apart from it should fail the build
    rather than fail at the one moment a run needs the method.
    """
    return surface


def _dialog_kind(kind: str) -> DialogKind:
    if kind == "confirm":
        return "confirm"
    if kind == "prompt":
        return "prompt"
    if kind == "beforeunload":
        return "beforeunload"
    return "alert"


def _index_in(node: Node, nodes: Sequence[Node]) -> int:
    """Position of a node among those its locator enumerates.

    Role queries enumerate a whole role, which ``ordinal`` already counts. Bare
    text is found by its content, so it counts only same-frame text nodes with
    the same words.
    """
    if node.role != "text":
        return node.ordinal
    same = [
        n.ref for n in nodes if n.frame == node.frame and n.role == "text" and n.name == node.name
    ]
    return same.index(node.ref) if node.ref in same else 0


def _query_for(frame: Frame, node: Node) -> Locator:
    """Every element of this node's kind in its frame, in document order.

    A role query for nodes with a role; the text itself for bare text, which
    has no ARIA role to be asked for by.
    """
    if node.role == "text":
        return frame.get_by_text(node.name, exact=True)
    return frame.get_by_role(node.role)  # type: ignore[arg-type]


def _moved(seen: Rect, now: FloatRect) -> bool:
    """Has the element's centre moved further than the drift tolerance?"""
    sx, sy = seen.center
    nx, ny = now["x"] + now["width"] / 2, now["y"] + now["height"] / 2
    return math.hypot(sx - nx, sy - ny) > DRIFT_TOLERANCE_PX


def _count(query: Locator) -> int:
    """How many elements a query finds right now. Never waits."""
    try:
        return query.count()
    except PlaywrightError:
        return 0


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
