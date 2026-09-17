"""Mock app acceptance at the HTTP level.

These need no browser. They exist so that M0 has a verification that fails
loudly if the target app drifts, and so the inject contract (which mode fires
where, and whether it clears) is pinned before any replay code depends on it.
"""

from __future__ import annotations

import time

import httpx
import pytest

from mockapp.data import MEMBERS, format_currency
from mockapp.injects import ONE_SHOT, PERSISTENT, Inject


def arm(client: httpx.Client, mode: Inject | str) -> None:
    value = mode.value if isinstance(mode, Inject) else mode
    client.get("/login", params={"inject": value})


def fired(client: httpx.Client) -> list[str]:
    return list(client.get("/_debug/session").json()["fired"])


def sign_in(client: httpx.Client) -> httpx.Response:
    return client.post("/login", data={"F_USRID": "operator", "F_PWD": "operator"})


def search(client: httpx.Client, member_id: str) -> httpx.Response:
    return client.post("/search", data={"F_MBRID": member_id})


# --------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------


def test_login_page_is_served(client: httpx.Client) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    assert "Sign On" in response.text
    assert "User ID" in response.text


def test_unauthenticated_request_redirects_to_login(client: httpx.Client) -> None:
    response = client.get("/search")
    assert response.status_code == 200
    assert str(response.url).endswith("/login")


def test_bad_credentials_are_rejected(client: httpx.Client) -> None:
    response = client.post("/login", data={"F_USRID": "operator", "F_PWD": "wrong"})
    assert "Invalid user id or password." in response.text
    assert client.get("/_debug/session").json()["authed"] is False


def test_unknown_inject_mode_is_a_400(client: httpx.Client) -> None:
    response = client.get("/login", params={"inject": "no_such_mode"})
    assert response.status_code == 400


def test_signed_in_shell_is_a_frameset(signed_in: httpx.Client) -> None:
    response = signed_in.get("/")
    assert "<frameset" in response.text
    assert 'src="/nav"' in response.text
    assert 'src="/search"' in response.text


def test_happy_path_walk(signed_in: httpx.Client) -> None:
    """Login -> search 10003 -> detail -> sub-account -> review -> confirm."""
    member = MEMBERS["10003"]

    detail = search(signed_in, "10003")
    assert str(detail.url).endswith("/member/10003")
    assert "Balances" in detail.text
    assert member.name in detail.text
    assert format_currency(member.savings) in detail.text

    form = signed_in.get("/subaccount/10003")
    assert "Initial Deposit" in form.text

    review = signed_in.post("/subaccount/10003", data={"F_ACCTTYP": "Savings", "F_DEPAMT": "25.00"})
    assert str(review.url).endswith("/review/10003")
    assert "Confirm" in review.text

    confirmation = signed_in.post("/review/10003")
    assert "Reference number" in confirmation.text
    assert "REF-10003-0001" in confirmation.text


def test_search_textboxes_have_no_accessible_name(signed_in: httpx.Client) -> None:
    """The hostility contract: labels are table cells, never <label for>."""
    markup = signed_in.get("/search").text
    assert "<label" not in markup
    assert "aria-label" not in markup
    assert "Member ID" in markup


# --------------------------------------------------------------------------
# Business-outcome injects (persistent)
# --------------------------------------------------------------------------


def test_not_found_inject_blocks_a_real_member(signed_in: httpx.Client) -> None:
    arm(signed_in, Inject.NOT_FOUND)
    response = search(signed_in, "10003")
    assert "No matching member" in response.text
    assert Inject.NOT_FOUND.value in fired(signed_in)


def test_unknown_member_is_not_found_without_any_inject(signed_in: httpx.Client) -> None:
    response = search(signed_in, "99999")
    assert "No matching member" in response.text
    assert fired(signed_in) == []


def test_validation_error_inject_rejects_a_valid_deposit(signed_in: httpx.Client) -> None:
    search(signed_in, "10003")
    arm(signed_in, Inject.VALIDATION_ERROR)
    response = signed_in.post(
        "/subaccount/10003", data={"F_ACCTTYP": "Savings", "F_DEPAMT": "25.00"}
    )
    assert "Initial deposit is required" in response.text


def test_empty_deposit_is_rejected_without_any_inject(signed_in: httpx.Client) -> None:
    search(signed_in, "10003")
    response = signed_in.post("/subaccount/10003", data={"F_ACCTTYP": "Savings", "F_DEPAMT": ""})
    assert "Initial deposit is required" in response.text


def test_member_10007_is_restricted_by_data(signed_in: httpx.Client) -> None:
    response = search(signed_in, "10007")
    assert "You are not authorized to view this record" in response.text


def test_permission_denied_inject_restricts_any_member(signed_in: httpx.Client) -> None:
    arm(signed_in, Inject.PERMISSION_DENIED)
    response = search(signed_in, "10003")
    assert "You are not authorized to view this record" in response.text


# --------------------------------------------------------------------------
# Hard failure and drift injects (persistent)
# --------------------------------------------------------------------------


def test_server_error_inject_returns_500_repeatedly(signed_in: httpx.Client) -> None:
    arm(signed_in, Inject.SERVER_ERROR)
    for _ in range(2):
        response = signed_in.get("/member/10003")
        assert response.status_code == 500
        assert "Internal Server Error" in response.text


def test_renamed_button_inject_changes_the_submit_label(signed_in: httpx.Client) -> None:
    assert 'value="Search"' in signed_in.get("/search").text
    arm(signed_in, Inject.RENAMED_BUTTON)
    markup = signed_in.get("/search").text
    assert 'value="Find"' in markup
    assert 'value="Search"' not in markup


def test_ambiguous_button_inject_adds_a_second_search_in_the_nav_frame(
    signed_in: httpx.Client,
) -> None:
    assert 'value="Search"' not in signed_in.get("/nav").text
    arm(signed_in, Inject.AMBIGUOUS_BUTTON)
    assert 'value="Search"' in signed_in.get("/nav").text
    # Still one in the main frame: two nodes named "Search" across two frames.
    assert 'value="Search"' in signed_in.get("/search").text


# --------------------------------------------------------------------------
# Recoverable injects (one-shot) - the clearing behaviour is the contract
# --------------------------------------------------------------------------


def test_interstitial_dialog_fires_once_after_login(client: httpx.Client) -> None:
    arm(client, Inject.INTERSTITIAL_DIALOG)
    first = sign_in(client)
    assert "System notice" in first.text
    assert "OK" in first.text

    client.get("/logoff")
    second = sign_in(client)
    assert "System notice" not in second.text
    assert "<frameset" in second.text


def test_session_expired_redirects_once_then_not_again(signed_in: httpx.Client) -> None:
    arm(signed_in, Inject.SESSION_EXPIRED)

    first = signed_in.get("/member/10003")
    assert str(first.url).endswith("/login")
    assert signed_in.get("/_debug/session").json()["authed"] is False

    sign_in(signed_in)
    second = signed_in.get("/member/10003")
    assert str(second.url).endswith("/member/10003")
    assert "Balances" in second.text
    assert fired(signed_in) == [Inject.SESSION_EXPIRED.value]


def test_slow_load_delays_once_then_returns_to_normal(signed_in: httpx.Client) -> None:
    arm(signed_in, Inject.SLOW_LOAD)

    start = time.monotonic()
    first = signed_in.get("/member/10003")
    slow = time.monotonic() - start
    assert first.status_code == 200
    assert slow >= 3.5

    start = time.monotonic()
    second = signed_in.get("/member/10003")
    fast = time.monotonic() - start
    assert second.status_code == 200
    assert fast < 2.0


def test_slow_confirm_delays_the_irreversible_step_once_and_still_commits(
    signed_in: httpx.Client,
) -> None:
    search(signed_in, "10003")
    signed_in.post("/subaccount/10003", data={"F_ACCTTYP": "Savings", "F_DEPAMT": "25.00"})
    arm(signed_in, Inject.SLOW_CONFIRM)

    start = time.monotonic()
    response = signed_in.post("/review/10003")
    elapsed = time.monotonic() - start

    assert elapsed >= 5.5
    # The write lands despite the timeout the engine will see. This is why a
    # timeout on an irreversible step is side_effect: unknown, not a retry.
    assert "Reference number" in response.text


# --------------------------------------------------------------------------
# Registry-level guards
# --------------------------------------------------------------------------


def test_every_inject_mode_is_classified_exactly_once() -> None:
    assert ONE_SHOT | PERSISTENT == frozenset(Inject)
    assert not (ONE_SHOT & PERSISTENT)


@pytest.mark.parametrize("mode", sorted(Inject, key=lambda m: m.value))
def test_every_inject_mode_is_reachable_from_a_fresh_session(
    client: httpx.Client, mode: Inject
) -> None:
    """Acceptance from the plan: every mode reachable from a fresh session.

    Walks far enough for each mode's screen to be requested, then asserts the
    mode actually fired rather than sitting armed and unread.
    """
    arm(client, mode)
    sign_in(client)
    if mode is not Inject.INTERSTITIAL_DIALOG:
        search(client, "10003")
        client.get("/member/10003")
    if mode in (Inject.VALIDATION_ERROR, Inject.SLOW_CONFIRM, Inject.NATIVE_CONFIRM):
        client.post("/subaccount/10003", data={"F_ACCTTYP": "Savings", "F_DEPAMT": "25.00"})
        if mode is Inject.SLOW_CONFIRM:
            client.post("/review/10003")

    if mode in (Inject.RENAMED_BUTTON, Inject.AMBIGUOUS_BUTTON, Inject.NATIVE_CONFIRM):
        # Presentation-only modes: nothing "fires", the screen just differs.
        assert client.get("/_debug/session").json()["inject"] == mode.value
    else:
        assert mode.value in fired(client)
