"""The allowlist check and observation redaction, as pure functions."""

from __future__ import annotations

from pathlib import Path

from cua.policy.allowlist import Allow, Block, NeedsApproval, check, load_policy
from cua.policy.redaction import MASK, Redactor
from cua.surface.protocol import Click, Navigate, Press, ReadText, TypeText
from cua.tenant import Tenant
from tests.unit import screens

REPO = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8000"
TENANT = Tenant(id="local", app_family="legacy-core", base_url=BASE)
POLICY = load_policy(REPO / "policies" / "default.yaml", TENANT)

REVIEW = """
- heading "Review Sub-account" [level=3]
- table:
  - rowgroup:
    - row "Confirm":
      - cell "Confirm":
        - button "Confirm"
- table:
  - rowgroup:
    - row "Note":
      - cell "Note"
      - cell:
        - textbox
"""


def screen(snapshot: str, url: str):
    return screens.build({"main": snapshot}, urls={"main": url})


def ref(observation, role: str, name: str = "") -> str:
    return next(n.ref for n in observation.nodes if n.role == role and n.name == name)


def test_the_tenant_base_url_is_filled_into_the_policy() -> None:
    assert POLICY.allowed_origins == [BASE]


def test_an_ordinary_click_on_the_tenant_is_allowed() -> None:
    obs = screen(screens.SEARCH, f"{BASE}/search")
    assert isinstance(check(POLICY, Click(ref=ref(obs, "button", "Search")), obs), Allow)


def test_confirm_on_the_review_screen_needs_approval() -> None:
    obs = screen(REVIEW, f"{BASE}/review/10003")
    decision = check(POLICY, Click(ref=ref(obs, "button", "Confirm")), obs)
    assert isinstance(decision, NeedsApproval)
    assert decision.rule == "commit_on_review"


def test_enter_in_a_field_on_the_review_screen_needs_approval_too() -> None:
    obs = screen(REVIEW, f"{BASE}/review/10003")
    field = ref(obs, "textbox")
    assert isinstance(check(POLICY, Press(key="Enter", ref=field), obs), NeedsApproval)
    assert isinstance(check(POLICY, TypeText(ref=field, text="x"), obs), Allow)


def test_a_screen_on_another_origin_blocks_every_action() -> None:
    obs = screen(screens.SEARCH, "https://elsewhere.example/search")
    decision = check(POLICY, ReadText(ref=ref(obs, "button", "Search")), obs)
    assert isinstance(decision, Block)
    assert "elsewhere.example" in decision.reason


def test_an_action_not_on_the_allowlist_is_blocked() -> None:
    obs = screen(screens.SEARCH, f"{BASE}/search")
    assert isinstance(check(POLICY, Navigate(url=f"{BASE}/search"), obs), Block)


def test_the_policy_summary_names_the_risky_rule_for_the_agent() -> None:
    assert "needs approval" in POLICY.summary()
    assert BASE in POLICY.summary()


# -- redaction ---------------------------------------------------------------


LOGIN_TYPED = screens.LOGIN.replace(
    '- cell "Password"\n      - cell:\n        - textbox\n',
    '- cell "Password"\n      - cell:\n        - textbox: unknown-to-us\n',
).replace("MockCore is a simulated", "Signed on as opuser. MockCore is a simulated")


def test_a_control_labelled_like_a_secret_is_masked_whatever_it_holds() -> None:
    obs = screens.build({"": LOGIN_TYPED})
    clean = Redactor(sensitive_labels=POLICY.sensitive_labels).observation(obs)
    assert "unknown-to-us" not in clean.model_dump_json()
    assert f"={MASK!r}" in clean.compact()


def test_a_known_secret_is_masked_wherever_it_appears() -> None:
    obs = screens.build({"": LOGIN_TYPED})
    clean = Redactor(["opuser"]).observation(obs)
    assert "opuser" not in clean.model_dump_json()
    assert "Signed on as ***." in clean.compact()


def test_a_longer_secret_is_masked_whole_before_a_shorter_one_inside_it() -> None:
    assert Redactor(["pass", "password1"]).text("x password1 y") == "x *** y"
