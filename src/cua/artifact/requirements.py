"""Which surface features a capability needs, and whether a surface has them.

Two sources, never confused with each other:

* **derived** — read off what the capability actually contains: a ``near_text``
  rung needs geometry, a rung scoped to frame ``main`` needs frames, a
  ``type`` step needs forms. This is what running it *will* ask of a surface,
  whatever anyone wrote down.
* **declared** — ``surface_requirements`` in a schema 1.2 artifact: what the
  reviewer approved as the capability's needs. It must cover the derived set
  (``schema.py`` rejects one that does not), and may add to it.

A schema 1.1 artifact predates the field, so it has no declaration. It is not
rewritten to gain one — that would change its content hash and void its
approval — and it is not assumed to need nothing. Its requirements are derived,
and every report says they were (``source: derived``). That is the whole
compatibility layer: an old capability runs as it always did, on a surface
that has what it uses, and is refused on one that does not.

Replay adds what the *run* needs on top: a guarded egress for a web target
(the policy), masked screenshots when the run insists on them (by default a
run keeps them when the surface can take them) and always for a person asked
to help, a session a person can take over when ``--handoff`` is on, and
screenshots and pointer input when the run may fall back to vision
(``--vision``). A surface short of those cannot run anything
safely, whatever the capability.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

from cua.surface.adapters import ADAPTERS, adapter_for
from cua.surface.features import SurfaceDescriptor, SurfaceFeature, ordered

if TYPE_CHECKING:  # the schema validates against derive(); import it lazily
    from cua.artifact.schema import Capability

_BY_STRATEGY: dict[str, tuple[SurfaceFeature, ...]] = {
    "role_name": ("accessibility_tree",),
    "table_cell": ("accessibility_tree",),
    "near_text": ("accessibility_tree", "geometry"),
    "bbox": ("geometry", "fixed_viewport"),
}
"""Locator rungs. A ``bbox`` rung is pixels: it needs boxes, and the viewport
the pixels were measured in (``recording_env``)."""

_BY_KIND: dict[str, tuple[SurfaceFeature, ...]] = {
    "location": ("locations",),
    "location_matches": ("locations",),
    "visible": ("accessibility_tree", "geometry"),
    "text_present": ("accessibility_tree",),
    "region_present": ("accessibility_tree",),
    "value_set": ("accessibility_tree",),
    "error_banner_present": ("accessibility_tree", "document_status"),
    "validation_message_present": ("accessibility_tree", "geometry"),
    "dialog_raised": ("dialogs",),
    "output_extracted": (),
    "timeout": (),
    "all_of": (),
    "any_of": (),
}
"""Conditions (and the entry, ``kind: location``). ``visible`` means "has a
real box", and a validation message is tied to its field by distance, so both
need geometry. An HTTP 500 page has no ARIA to give itself away, so the error
banner leans on the document's status."""

_BY_ACTION: dict[str, tuple[SurfaceFeature, ...]] = {
    "click": (),
    "read": (),
    "type": ("forms",),
    "select": ("forms",),
    "press": ("keyboard",),
}
"""Step and recoverer actions. Clicking and reading need only the node, which
the target's rungs already account for."""

WEB_TARGETS: frozenset[str] = frozenset({"web", "legacy_web"})
WEB_RUN_FEATURES: tuple[SurfaceFeature, ...] = ("egress_control",)
"""What every replay of a web target needs, whatever it runs: the page's
requests held to the policy's origins. A page runs content the application
does not control (a script, a form's destination), which is what the guard is
for. A desktop application's own network traffic is outside what a UI surface
can govern, so for a desktop target this is not asked; its replay still
enforces the policy on every action and on the entry location."""
EVIDENCE_FEATURES: tuple[SurfaceFeature, ...] = ("screenshots",)
HANDOFF_FEATURES: tuple[SurfaceFeature, ...] = ("screenshots", "session_handoff")
"""An intervention request always carries the screen, screenshots or not."""
VISION_FEATURES: tuple[SurfaceFeature, ...] = ("screenshots", "pointer")
"""The vision fallback (``--vision``) looks for a control on a masked
screenshot and clicks a point of the screen."""

Source = Literal["declared", "derived"]


class UnknownConstruct(ValueError):
    """The artifact uses a rung, condition or action this table does not
    know. Refused rather than assumed to need nothing: a new construct is
    added to the vocabulary and to this table together."""


class Requirements(BaseModel):
    """What running a capability asks of a surface."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: list[SurfaceFeature]
    """The capability's own: declared (1.2) or derived (1.1)."""
    source: Source
    run: list[SurfaceFeature]
    """Added by this run: egress, evidence, handoff."""

    @property
    def all(self) -> list[SurfaceFeature]:
        return ordered([*self.capability, *self.run])


class Compatibility(BaseModel):
    """A capability's requirements against one surface."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface: dict[str, object]
    requirements: Requirements
    missing: list[SurfaceFeature]

    @property
    def ok(self) -> bool:
        return not self.missing

    def summary(self) -> dict[str, object]:
        """For ``run.json``."""
        return {
            "adapter": self.surface,
            "requires": self.requirements.capability,
            "requires_source": self.requirements.source,
            "run_requires": self.requirements.run,
            "missing": self.missing,
        }

    def refusal(self, name: str, version: int) -> str:
        surface = f"{self.surface['name']} v{self.surface['version']}"
        return (
            f"{name} v{version} needs surface features this {surface} surface does not have: "
            f"{', '.join(self.missing)} (capability requirements {self.requirements.source}). "
            "Replay does not run a capability on a surface short of what it uses; it needs "
            "an adapter that has them."
        )


def derive(data: Mapping[str, Any]) -> list[SurfaceFeature]:
    """The features a capability's content uses, from its JSON form.

    Takes the dumped dict rather than the model so the schema can validate a
    declaration against it while the model is still being built. Provenance
    and the declaration itself are skipped: neither is run.
    """
    found: set[SurfaceFeature] = set()
    for key, value in data.items():
        if key in ("provenance", "surface_requirements", "description"):
            continue
        for node in _dicts(value):
            found.update(_needs(node))
    return ordered(found)


def of(cap: Capability) -> tuple[list[SurfaceFeature], Source]:
    """The capability's requirements, and where they came from."""
    if cap.surface_requirements is not None:
        return ordered(cap.surface_requirements), "declared"
    return derive(cap.model_dump(mode="json", by_alias=True)), "derived"


def for_run(
    cap: Capability, *, handoff: bool, screenshots: bool | None = None, vision: bool = False
) -> Requirements:
    """``screenshots``: ``True`` requires them; ``None`` (take them where the
    surface can) and ``False`` do not (``ReplayConfig.screenshots``).
    ``vision``: the run may fall back to vision (``ReplayConfig.vision``)."""
    features, source = of(cap)
    run = [
        *(WEB_RUN_FEATURES if cap.target.surface in WEB_TARGETS else ()),
        *(EVIDENCE_FEATURES if screenshots is True else ()),
        *(HANDOFF_FEATURES if handoff else ()),
        *(VISION_FEATURES if vision else ()),
    ]
    return Requirements(capability=features, source=source, run=ordered(run))


def check(
    cap: Capability,
    descriptor: SurfaceDescriptor,
    *,
    handoff: bool,
    screenshots: bool | None = None,
    vision: bool = False,
) -> Compatibility:
    """Would this run of ``cap`` get everything it uses from this surface?"""
    wanted = for_run(cap, handoff=handoff, screenshots=screenshots, vision=vision)
    return Compatibility(
        surface=descriptor.summary(),
        requirements=wanted,
        missing=descriptor.missing(wanted.all),
    )


def refusal(
    cap: Capability,
    *,
    handoff: bool = False,
    screenshots: bool | None = None,
    vision: bool = False,
) -> str | None:
    """Why this build cannot run ``cap``, or None if it can: no adapter for
    its target kind, or an adapter short of a feature it uses. Checked with
    the registered adapter, before any surface exists."""
    adapter = adapter_for(cap.target.surface)
    if adapter is None:
        return (
            f"{cap.name} drives a {cap.target.surface!r} surface, and this build has an "
            f"adapter for {', '.join(sorted(ADAPTERS))} only. Driving it with the browser "
            "adapter would act on the wrong thing; a desktop surface needs a Surface of its "
            "own (UIA or AX)."
        )
    try:
        fits = check(cap, adapter, handoff=handoff, screenshots=screenshots, vision=vision)
    except UnknownConstruct as exc:
        return str(exc)
    return None if fits.ok else fits.refusal(cap.name, cap.version)


def _needs(node: Mapping[str, Any]) -> Iterator[SurfaceFeature]:
    strategy = node.get("strategy")
    if isinstance(strategy, str):
        yield from _lookup(_BY_STRATEGY, strategy, "locator rung")
    kind = node.get("kind")
    if isinstance(kind, str):
        yield from _lookup(_BY_KIND, kind, "condition")
    action = node.get("action")
    if isinstance(action, str):
        yield from _lookup(_BY_ACTION, action, "action")
    # A rung or condition scopes to ``{"frame": name}``, a detector's scope to
    # the bare name; the top document has the empty name and needs no frames.
    within = node.get("within")
    if (isinstance(within, Mapping) and within.get("frame")) or (
        isinstance(within, str) and within
    ):
        yield "frames"
    if node.get("screenshot_masks"):
        yield "screenshots"


def _lookup(
    table: dict[str, tuple[SurfaceFeature, ...]], name: str, what: str
) -> tuple[SurfaceFeature, ...]:
    try:
        return table[name]
    except KeyError:
        raise UnknownConstruct(
            f"no surface requirements are known for {what} {name!r}; add it to "
            "cua.artifact.requirements before any artifact may use it"
        ) from None


def _dicts(data: Any) -> Iterator[Mapping[str, Any]]:
    if isinstance(data, Mapping):
        yield data
        for value in data.values():
            yield from _dicts(value)
    elif isinstance(data, list):
        for value in data:
            yield from _dicts(value)
