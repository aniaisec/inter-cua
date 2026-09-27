"""Where on a screenshot a recorded control appears: candidates, never clicks.

A detector is handed a masked screenshot and the control's recorded
``Appearance`` and returns every place the picture matches well enough to be
worth a look, best first, each with a confidence. It does not decide which
one is meant, whether acting there is allowed, or whether to act at all: that
is ``validator``, the policy, and the engine, in that order.

The detector here is model-free on purpose. Replay never initialises a model
(a test proves it), and a fallback that did would put a model's judgement in
front of every action it touched. ``TemplateDetector`` is zero-mean
normalised cross-correlation: the recorded crop slid over the screen, scored
by how well the two agree in shape once brightness and contrast are taken out.
It finds a control that was drawn exactly as before and has lost its place in
the accessibility tree, which is the gap this fallback exists for. It does not
find a control that was redrawn (renamed, restyled), and that is correct: a
button that now reads differently is a change for a person to look at, not a
match to be forced.
"""

from __future__ import annotations

import base64
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from cua.surface.protocol import Rect
from cua.surface.vision import image
from cua.surface.vision.candidate import Appearance
from cua.surface.vision.image import Pixels

MATCH_FLOOR = 0.5
"""Scores below this are not reported at all. Well under any threshold that
would be acted on, so a near miss still shows up in the evidence as the
runner-up that made a match ambiguous."""
MAX_MATCHES = 5
FLAT = 1e-6
"""A window this uniform (a blank area) has no shape to correlate with."""


class VisualMatch(BaseModel):
    """One place the picture matched: a box in viewport pixels and a score."""

    model_config = ConfigDict(frozen=True)

    bounds: Rect
    confidence: float
    """Normalised cross-correlation, clipped to [0, 1]. 1 is the same picture."""


class Detector(Protocol):
    name: str

    def detect(self, screenshot_png: bytes, appearance: Appearance) -> list[VisualMatch]:
        """Every match at or above the floor, best first, none overlapping."""
        ...


class TemplateDetector:
    name = "template_ncc"

    def __init__(self, *, floor: float = MATCH_FLOOR, limit: int = MAX_MATCHES) -> None:
        self.floor = floor
        self.limit = limit

    def detect(self, screenshot_png: bytes, appearance: Appearance) -> list[VisualMatch]:
        screen = image.gray(screenshot_png)
        template = image.gray(base64.b64decode(appearance.png_base64))
        scores = ncc(screen, template)
        return _peaks(scores, template.shape, floor=self.floor, limit=self.limit)


def ncc(screen: Pixels, template: Pixels) -> Pixels:
    """Zero-mean normalised cross-correlation of ``template`` at every
    position it fits entirely inside ``screen``: an array of
    (H - h + 1) x (W - w + 1) scores in [-1, 1].

    The correlation is computed with FFTs and the per-window statistics with
    summed-area tables, so a full screen costs a few tens of milliseconds.
    """
    import numpy as np

    big_h, big_w = screen.shape
    h, w = template.shape
    if h > big_h or w > big_w or h == 0 or w == 0:
        return np.zeros((0, 0))
    n = h * w
    t = template - template.mean()
    t_norm = float(np.sqrt((t * t).sum()))
    if t_norm < FLAT:
        return np.zeros((big_h - h + 1, big_w - w + 1))

    # Padded to sizes the FFT factors well: 1280 + 62 has a large prime
    # factor, and an FFT of that length is several times slower.
    shape = (_fast_len(big_h + h - 1), _fast_len(big_w + w - 1))
    spectrum = np.fft.rfft2(screen, shape) * np.fft.rfft2(t[::-1, ::-1], shape)
    corr = np.fft.irfft2(spectrum, shape)[h - 1 : big_h, w - 1 : big_w]

    sums = _window_sums(screen, h, w)
    squares = _window_sums(screen * screen, h, w)
    variance = np.maximum(squares - sums * sums / n, 0.0)
    denom = np.sqrt(variance) * t_norm
    out = np.zeros_like(corr)
    live = denom > FLAT
    out[live] = corr[live] / denom[live]
    return np.clip(out, -1.0, 1.0)


def _fast_len(n: int) -> int:
    """The smallest length >= n with no prime factor above 5."""
    best = 1
    while best < n:
        best *= 2
    power5 = 1
    while power5 < best:
        power3 = power5
        while power3 < best:
            length = power3
            while length < n:
                length *= 2
            best = min(best, length)
            power3 *= 3
        power5 *= 5
    return best


def _window_sums(a: Pixels, h: int, w: int) -> Pixels:
    import numpy as np

    table = np.zeros((a.shape[0] + 1, a.shape[1] + 1))
    table[1:, 1:] = a.cumsum(0).cumsum(1)
    return table[h:, w:] - table[:-h, w:] - table[h:, :-w] + table[:-h, :-w]


def _peaks(scores: Pixels, size: tuple[int, int], *, floor: float, limit: int) -> list[VisualMatch]:
    """Best score first; each found peak blanks out every position whose box
    would overlap its box, so one control is reported once."""
    import numpy as np

    h, w = size
    work = scores.copy()
    out: list[VisualMatch] = []
    while len(out) < limit and work.size:
        index = int(np.argmax(work))
        y, x = divmod(index, work.shape[1])
        best = float(work[y, x])
        if best < floor:
            break
        out.append(
            VisualMatch(bounds=Rect(x=x, y=y, w=w, h=h), confidence=round(min(best, 1.0), 4))
        )
        work[max(0, y - h + 1) : y + h, max(0, x - w + 1) : x + w] = -np.inf
    return out
