"""What vision knows about a control, and what it proposes to do about it.

``Appearance`` is recorded: a masked crop of the control, cut from the
screenshot of the screen it was acted on during discovery, and kept in the
capability beside its locator ladder. It is part of what a reviewer approves,
so a fallback can never act on a picture nobody looked at.

``VisualCandidate`` is proposed: one place on one screenshot where that
picture was found, the action vision would take there, how sure it is, and
the evidence file it was found on. A candidate is a proposal. It becomes an
action only through ``validator``, the policy, and the engine's checks after
acting, the same checkpoints every other step passes.
"""

from __future__ import annotations

import base64
import hashlib

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cua.surface.protocol import ClickPoint, Rect

MAX_APPEARANCE_BYTES = 16_384
"""A control, not a screen. A crop this large was cut around the wrong thing."""


class Appearance(BaseModel):
    """A control as it looked when it was recorded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    png_base64: str
    w: int = Field(ge=1)
    h: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    """Of the PNG bytes. Checked on load, so an edited picture is an error
    rather than a different control."""
    scale: float = 1.0
    """Screenshot pixels per viewport pixel at recording."""

    @model_validator(mode="after")
    def _intact(self) -> Appearance:
        try:
            data = base64.b64decode(self.png_base64, validate=True)
        except ValueError as exc:
            raise ValueError(f"appearance is not base64: {exc}") from exc
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("appearance is not a PNG")
        if len(data) > MAX_APPEARANCE_BYTES:
            raise ValueError(
                f"appearance is {len(data)} bytes; a control's crop is under {MAX_APPEARANCE_BYTES}"
            )
        if hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError("appearance does not match its sha256")
        return self

    @classmethod
    def of(cls, png: bytes, *, w: int, h: int, scale: float = 1.0) -> Appearance:
        return cls(
            png_base64=base64.b64encode(png).decode("ascii"),
            w=w,
            h=h,
            sha256=hashlib.sha256(png).hexdigest(),
            scale=scale,
        )


class VisualCandidate(BaseModel):
    """One proposal: act here, this sure, as seen on this evidence."""

    model_config = ConfigDict(frozen=True)

    action: ClickPoint
    confidence: float
    bounds: Rect
    evidence_ref: str
    """The screenshot, in the run directory, the candidate was found on."""
    rank: int = 0
    """0 for the best match on that screenshot."""
