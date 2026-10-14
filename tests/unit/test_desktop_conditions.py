"""Named desktop regions use UIA groups, tabs and windows, with frame scopes."""

from cua.surface.conditions import AllOf, RegionPresent, TextPresent
from cua.surface.locators import Within
from cua.surface.windows import perception as p
from cua.surface.windows.conditions import DesktopEvaluator
from tests.unit.test_windows_surface import deskcalc, el, seen


def test_a_desktop_group_is_a_region_but_a_same_named_control_is_not():
    window = deskcalc()
    window.children.append(el(p.GROUP, "Legacy", (440, 260, 580, 330)))
    obs = seen(window).observation
    evaluator = DesktopEvaluator()
    assert evaluator.evaluate(
        AllOf(all_of=[RegionPresent(name="Legacy"), TextPresent(text="First number")]), obs
    )
    assert not evaluator.evaluate(RegionPresent(name="Calculate"), obs)
    assert not evaluator.evaluate(RegionPresent(name="Legacy", within=Within(frame="missing")), obs)
    assert evaluator.evaluate(RegionPresent(name="DeskCalc"), obs)
