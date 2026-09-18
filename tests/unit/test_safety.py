"""Safety as pure functions: where automation may go, what consent looks like,
and what never reaches the model or the disk.

The allowlist is checked before every action by both loops; approval tokens
are checked before a browser starts; the scrubber is the last thing every log
line goes through. None of it needs a browser to prove.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cua.artifact.store import load, with_changes
from cua.evidence.logger import RunLog
from cua.policy import tokens
from cua.policy.allowlist import Allow, Block, NeedsApproval, check, load_policy
from cua.policy.redaction import MASK, Redactor
from cua.policy.tokens import Approval, SpentTokens, TokenRefused
from cua.replay.invocation import ApprovalGrant, Invocation
from cua.replay.result import Failure
from cua.replay.runner import replay
from cua.secrets.resolver import SecretError, resolve
from cua.surface.protocol import Click, Press, ReadText
from cua.tenant import SecretBinding, Tenant, load_tenant
from tests.unit import screens

REPO = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8000"
KEY = "k" * 40
KEY_BYTES = KEY.encode()
TENANT = Tenant(
    id="local",
    app_family="legacy-core",
    base_url=BASE,
    secrets={
        "mockcore/operator": SecretBinding(var="CUA_TEST_OPERATOR", format="username:password"),
        "cua/approval-signing-key": SecretBinding(var="CUA_TEST_SIGNING_KEY"),
    },
)
ENV = {"CUA_TEST_OPERATOR": "operator:operator", "CUA_TEST_SIGNING_KEY": KEY}
POLICY = load_policy(REPO / "policies" / "default.yaml", TENANT)
GOAL2 = REPO / "capabilities" / "open_subaccount.json"
INPUTS = {"member_id": "10003", "initial_deposit": "250.00"}

LINKS = """
- table:
  - rowgroup:
    - row:
      - cell:
        - link "Vendor Support":
          - /url: https://support.mockcore.example/kb
    - row:
      - cell:
        - link "Download Statement":
          - /url: /member/10003/statement.csv
    - row:
      - cell:
        - link "Session Debug":
          - /url: /_debug/session
    - row:
      - cell:
        - link "Open Sub-account":
          - /url: /subaccount/10003
    - row:
      - cell:
        - link "Member Search":
          - /url: javascript:parent.main.location='/search'
"""

REVIEW = """
- heading "Review Sub-account" [level=3]
- table:
  - rowgroup:
    - row "Confirm":
      - cell "Confirm":
        - button "Confirm"
"""


def screen(snapshot: str, path: str):
    return screens.build({"main": snapshot}, urls={"main": f"{BASE}{path}"})


def ref(observation, role: str, name: str) -> str:
    return next(n.ref for n in observation.nodes if n.role == role and n.name == name)


def click(name: str, obs=None):
    obs = obs or screen(LINKS, "/member/10003")
    return check(POLICY, Click(ref=ref(obs, "link", name)), obs)


# -- where automation may go ---------------------------------------------------


def test_a_link_off_the_allowed_origins_is_blocked_before_it_is_clicked() -> None:
    decision = click("Vendor Support")
    assert isinstance(decision, Block)
    assert "external navigation is blocked" in decision.reason
    assert "support.mockcore.example" in decision.reason


def test_a_link_that_fetches_a_file_is_blocked() -> None:
    decision = click("Download Statement")
    assert isinstance(decision, Block)
    assert "downloads are blocked" in decision.reason


def test_a_link_to_a_path_outside_the_allowlist_is_blocked() -> None:
    decision = click("Session Debug")
    assert isinstance(decision, Block)
    assert "outside the allowed paths (/_debug/session)" in decision.reason


def test_links_inside_the_app_and_script_links_are_allowed() -> None:
    assert isinstance(click("Open Sub-account"), Allow)
    assert isinstance(click("Member Search"), Allow)


def test_reading_a_blocked_link_is_not_following_it() -> None:
    obs = screen(LINKS, "/member/10003")
    assert isinstance(check(POLICY, ReadText(ref=ref(obs, "link", "Vendor Support")), obs), Allow)


def test_enter_on_a_blocked_link_is_blocked_too() -> None:
    obs = screen(LINKS, "/member/10003")
    press = Press(key="Enter", ref=ref(obs, "link", "Download Statement"))
    assert isinstance(check(POLICY, press, obs), Block)


def test_a_screen_on_a_path_outside_the_allowlist_blocks_every_action() -> None:
    obs = screen(LINKS, "/admin/users")
    decision = check(POLICY, Click(ref=ref(obs, "link", "Open Sub-account")), obs)
    assert isinstance(decision, Block)
    assert "outside the allowed paths (/admin/users)" in decision.reason


def test_removing_a_kind_from_blocked_actions_lets_it_through() -> None:
    lenient = POLICY.model_copy(update={"blocked_actions": ["external_navigation"]})
    obs = screen(LINKS, "/member/10003")
    link = Click(ref=ref(obs, "link", "Download Statement"))
    # Allowed as a download; still stopped by the path allowlist.
    decision = check(lenient, link, obs)
    assert isinstance(decision, Block) and "allowed paths" in decision.reason


def test_the_agent_is_told_what_is_blocked() -> None:
    summary = POLICY.summary()
    assert "leave the allowed origins are blocked" in summary
    assert "download a file are blocked" in summary


# -- approval in the policy check -----------------------------------------------

CONSENT = Approval(
    capability="open_subaccount",
    version=2,
    tenant="local",
    approved_by="ops",
    expires_at=2_000_000_000,
    token_sha256="0" * 64,
)


def test_confirm_on_review_needs_approval_without_one_and_is_allowed_with_one() -> None:
    obs = screen(REVIEW, "/review/10003")
    confirm = Click(ref=ref(obs, "button", "Confirm"))
    assert isinstance(check(POLICY, confirm, obs), NeedsApproval)
    decision = check(POLICY, confirm, obs, approval=CONSENT)
    assert isinstance(decision, Allow)
    assert decision.approved_rule == "commit_on_review"


def test_an_approval_never_lifts_a_block() -> None:
    obs = screen(LINKS, "/member/10003")
    link = Click(ref=ref(obs, "link", "Vendor Support"))
    assert isinstance(check(POLICY, link, obs, approval=CONSENT), Block)


# -- approval tokens ---------------------------------------------------------------


def goal2():
    return load(GOAL2)


def mint(**over) -> str:
    args = {"approved_by": "anil", "key": KEY.encode(), "now": 1_000.0}
    args.update(over)
    return tokens.mint(goal2(), TENANT, dict(INPUTS), **args)  # type: ignore[arg-type]


def verify(token: str, *, inputs=None, cap=None, tenant=TENANT, now=1_010.0, key=KEY_BYTES):
    return tokens.verify(token, cap or goal2(), tenant, dict(inputs or INPUTS), key=key, now=now)


def test_a_token_verifies_for_exactly_the_invocation_it_was_signed_for() -> None:
    token = mint()
    approval = verify(token)
    assert (approval.capability, approval.approved_by) == ("open_subaccount", "anil")
    assert approval.expires_at == 1_000 + tokens.DEFAULT_TTL_S
    assert approval.token_sha256 == tokens.token_sha256(token)


def test_the_token_carries_a_hash_of_the_inputs_not_the_values() -> None:
    body = mint().split(".")[1]
    claims = tokens._unb64(body).decode()
    assert "10003" not in claims and "250.00" not in claims


@pytest.mark.parametrize(
    ("change", "complaint"),
    [
        ({"inputs": {**INPUTS, "initial_deposit": "9999.00"}}, "other inputs"),
        ({"now": 1_000.0 + tokens.DEFAULT_TTL_S}, "expired"),
        ({"key": b"another-key-another-key-another-key"}, "does not verify"),
        (
            {"tenant": TENANT.model_copy(update={"id": "fcu042"})},
            "tenant 'local'",
        ),
    ],
)
def test_a_token_for_anything_else_is_refused(change: dict, complaint: str) -> None:
    with pytest.raises(TokenRefused, match=complaint):
        verify(mint(), **change)


def test_a_token_for_an_edited_capability_is_refused() -> None:
    cap = goal2()
    data = cap.model_dump(mode="json", by_alias=True)
    data["description"] = data["description"] + " (edited)"
    edited = type(cap).model_validate(data)
    with pytest.raises(TokenRefused, match="that content"):
        verify(mint(), cap=edited)


def test_approving_the_capability_does_not_invalidate_a_token() -> None:
    """Approval bookkeeping is not content: the thing consented to is the same."""
    approved = with_changes(goal2(), approved_by="someone-else")
    assert verify(mint(), cap=approved).approved_by == "anil"


def test_a_tampered_token_is_refused() -> None:
    prefix, body, mac = mint().split(".")
    claims = json.loads(tokens._unb64(body))
    claims["by"] = "mallory"
    forged = ".".join([prefix, tokens._b64(json.dumps(claims).encode()), mac])
    with pytest.raises(TokenRefused, match="does not verify"):
        verify(forged)


@pytest.mark.parametrize("junk", ["", "s3cret", "cat1.", "cat1.abc", "cat1.!!.??"])
def test_something_that_is_not_a_token_is_refused(junk: str) -> None:
    with pytest.raises(TokenRefused):
        verify(junk)


def test_a_short_signing_key_is_refused() -> None:
    with pytest.raises(TokenRefused, match="shorter than"):
        tokens.signing_key(TENANT, environ={"CUA_TEST_SIGNING_KEY": "short"})


def test_a_file_bound_key_is_created_once_and_never_replaced(tmp_path: Path) -> None:
    tenant = load_tenant("local", root=REPO / "tenants")
    created = tokens.create_signing_key(tenant, root=tmp_path)
    assert created == tmp_path / ".cua" / "approval-signing.key"
    key = tokens.signing_key(tenant, root=tmp_path)
    assert len(key) == 64
    assert tokens.create_signing_key(tenant, root=tmp_path) is None
    assert tokens.signing_key(tenant, root=tmp_path) == key


def test_a_spent_token_is_remembered_by_hash_only(tmp_path: Path) -> None:
    token = mint()
    approval = verify(token)
    spent = SpentTokens(tmp_path)
    assert spent.spent_by(approval) is None
    spent.spend(approval, "run_X")
    assert spent.spent_by(approval) == "run_X"
    stored = "".join(p.read_text() for p in spent.dir.iterdir())
    assert token not in stored


# -- the runner checks consent before any browser starts --------------------------


def no_browser():
    raise AssertionError("a browser was started")


def run(tmp_path: Path, token: str | None, *, by: str | None = None):
    return replay(
        GOAL2,
        tenant=TENANT,
        policy=POLICY,
        invocation=Invocation(
            inputs=dict(INPUTS),
            approval=ApprovalGrant(token=token, approved_by=by) if token else None,
        ),
        runs_dir=tmp_path / "runs",
        surface=no_browser,  # type: ignore[arg-type]
        environ=ENV,
    )


@pytest.mark.parametrize(
    "token",
    [
        "any-non-empty-string",
        tokens.mint(
            load(GOAL2),
            TENANT,
            {**INPUTS, "initial_deposit": "1.00"},
            approved_by="anil",
            key=KEY.encode(),
        ),
    ],
)
def test_a_token_that_does_not_verify_is_policy_blocked_with_nothing_touched(
    tmp_path: Path, token: str
) -> None:
    result = run(tmp_path, token)
    assert isinstance(result, Failure)
    assert result.code == "POLICY_BLOCKED"
    assert result.message.startswith("approval refused:")
    assert result.side_effect == "none"
    assert not (tmp_path / "runs").exists()


def test_a_token_naming_someone_else_than_the_caller_says_is_refused(tmp_path: Path) -> None:
    token = tokens.mint(load(GOAL2), TENANT, dict(INPUTS), approved_by="anil", key=KEY.encode())
    result = run(tmp_path, token, by="ops")
    assert isinstance(result, Failure) and "signed for 'anil', not 'ops'" in result.message


def test_a_token_already_spent_on_a_commit_is_refused(tmp_path: Path) -> None:
    token = tokens.mint(load(GOAL2), TENANT, dict(INPUTS), approved_by="anil", key=KEY.encode())
    approval = tokens.verify(token, load(GOAL2), TENANT, dict(INPUTS), key=KEY.encode())
    SpentTokens(tmp_path / "runs").spend(approval, "run_EARLIER")
    result = run(tmp_path, token)
    assert isinstance(result, Failure) and result.code == "POLICY_BLOCKED"
    assert "already used by run_EARLIER" in result.message


# -- secrets from a file ---------------------------------------------------------


def file_tenant(path: str) -> Tenant:
    binding = SecretBinding(provider="file", path=path, format="username:password")
    return Tenant(id="t", app_family="x", base_url=BASE, secrets={"core/op": binding})


def test_a_file_bound_secret_resolves_without_its_trailing_newline(tmp_path: Path) -> None:
    (tmp_path / "op.secret").write_text("opuser:pa:ss\n", encoding="utf-8")
    cred = resolve("secret://t/core/op", file_tenant("op.secret"), root=tmp_path)
    assert (cred.field("username"), cred.field("password")) == ("opuser", "pa:ss")


@pytest.mark.parametrize(("content", "complaint"), [(None, "does not exist"), ("", "is empty")])
def test_a_missing_or_empty_secret_file_says_so_without_a_value(
    tmp_path: Path, content: str | None, complaint: str
) -> None:
    if content is not None:
        (tmp_path / "op.secret").write_text(content, encoding="utf-8")
    with pytest.raises(SecretError, match=complaint):
        resolve("secret://t/core/op", file_tenant("op.secret"), root=tmp_path)


def test_a_binding_must_name_where_its_value_lives() -> None:
    with pytest.raises(ValueError, match="names its file"):
        SecretBinding(provider="file")
    with pytest.raises(ValueError, match="names its variable"):
        SecretBinding(provider="env")


# -- shapes of personal data -------------------------------------------------------

SCRUB = Redactor.for_policy(POLICY)


@pytest.mark.parametrize(
    "text",
    [
        "SSN 123-45-6789 on file",
        "account 123456789012",
        "card 4111 1111 1111 1111",
        "card 4111-1111-1111-1111.",
    ],
)
def test_the_scrubber_masks_ssn_and_long_number_shapes(text: str) -> None:
    clean = SCRUB.text(text)
    assert MASK in clean
    assert not any(c.isdigit() for c in clean)


@pytest.mark.parametrize(
    "text",
    [
        "member 10003",
        "REF-10003-0001",
        "$1,411.21",
        "2026-09-18 12:34",
        "run_01K5ABCDEF123456789012345",
        "sha 0123456789abcdef",
        "12345678",  # eight digits: below the shape
    ],
)
def test_the_scrubber_leaves_ordinary_values_alone(text: str) -> None:
    assert SCRUB.text(text) == text


def test_an_ssn_on_screen_never_reaches_the_observation() -> None:
    obs = screen('- paragraph: "Tax id 123-45-6789"\n', "/member/10003")
    assert "123-45-6789" not in SCRUB.observation(obs).model_dump_json()


def test_the_run_log_scrubs_every_line_it_writes(tmp_path: Path) -> None:
    log = RunLog(tmp_path / "run")
    log.scrub_with(Redactor.for_policy(POLICY, ["hunter2pw"]).text)
    log.event("x", note="typed hunter2pw", nested={"ssn": ["123-45-6789"]}, n=123456789012)
    log.write_json("result.json", {"message": "card 4111 1111 1111 1111"})
    text = "".join(p.read_text(encoding="utf-8") for p in log.dir.rglob("*") if p.is_file())
    for leaked in ("hunter2pw", "123-45-6789", "4111 1111"):
        assert leaked not in text
    assert '"n": 123456789012' in text  # numbers are data the program wrote, not screen text


# -- the command a person signs consent with -------------------------------------


def test_cua_approval_token_creates_a_key_once_and_prints_a_verifiable_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cua.cli import main

    monkeypatch.chdir(tmp_path)
    tenant_file = tmp_path / "local.yaml"
    tenant_file.write_text(
        (REPO / "tenants" / "local.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    args = ["approval-token", str(GOAL2), "--tenant", str(tenant_file), "--by", "anil"]
    args += [f"--input={k}={v}" for k, v in INPUTS.items()]

    assert main(args) == 0
    first = capsys.readouterr()
    assert "created a signing key" in first.err
    token = first.out.strip()
    tenant = load_tenant(str(tenant_file))
    key = tokens.signing_key(tenant)
    assert verify(token, tenant=tenant, now=None, key=key).approved_by == "anil"

    assert main(args) == 0
    assert "created a signing key" not in capsys.readouterr().err
    assert tokens.signing_key(tenant) == key


def test_cua_approval_token_refuses_inputs_the_capability_would_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cua.cli import main

    monkeypatch.chdir(tmp_path)
    tenant_file = tmp_path / "local.yaml"
    tenant_file.write_text(
        (REPO / "tenants" / "local.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    args = ["approval-token", str(GOAL2), "--tenant", str(tenant_file), "--by", "anil"]
    code = main([*args, "--input", "member_id=abc", "--input", "initial_deposit=1"])
    assert code == 1
    assert "does not match" in capsys.readouterr().err
    assert not (tmp_path / ".cua").exists()


def test_the_signing_key_is_never_an_app_credential() -> None:
    tenant = load_tenant("local", root=REPO / "tenants")
    assert set(tenant.secrets) == {"mockcore/operator", "cua/approval-signing-key"}
    assert tenant.app_secrets == ["mockcore/operator"]


def test_discover_refuses_to_hand_the_agent_the_signing_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cua.cli import main

    code = main(
        [
            "discover",
            "--goal",
            "anything",
            "--tenant",
            str(REPO / "tenants" / "local.yaml"),
            "--credential",
            "app_login=secret://local/cua/approval-signing-key",
            "--llm",
            "scripted",
        ]
    )
    assert code != 0
    assert "the system's own secret" in capsys.readouterr().err
