"""The acceptance line for the Windows surface: a non-browser capability runs
through the same capability, runtime, policy and replay abstractions.

Every test replays a committed desktop capability through
``cua.replay.runner.replay``, the entry point ``cua replay`` uses, with the
desk tenant and policy. Nothing here is desktop-specific but the tenant: the
runner picks the adapter from ``target.surface``. What happened to DeskCalc is
judged by its ledger, never by what the run says about itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cua.artifact.store import load, save, with_changes
from cua.policy import tokens
from cua.policy.allowlist import load_policy
from cua.registry.store import Registry
from cua.replay.engine import ReplayConfig
from cua.replay.invocation import ApprovalGrant, Invocation
from cua.replay.result import BusinessOutcome, Failure, ReplayResult, Success
from cua.replay.runner import replay
from cua.tenant import DesktopApp, SecretBinding, Tenant
from tests.desktop.conftest import REPO, lines

pytestmark = pytest.mark.desktop

COMPUTE = REPO / "capabilities" / "deskcalc_compute.json"
RECORD = REPO / "capabilities" / "deskcalc_record.json"
SIGNING_KEY = "desktop-signing-key-0123456789abcdef"
ENV = {"CUA_TEST_SIGNING_KEY": SIGNING_KEY}
INPUTS = {"first": "12.5", "second": "4", "operation": "Multiply"}


@pytest.fixture
def run(desk: Tenant, ledger: Path, tmp_path: Path):
    tenant = desk.model_copy(
        update={"secrets": {"cua/approval-signing-key": SecretBinding(var="CUA_TEST_SIGNING_KEY")}}
    )
    policy = load_policy(REPO / "policies" / "deskcalc.yaml", tenant)

    def approved(path: Path) -> Path:
        """A copy approved and registered as `cua approve` would leave it."""
        cap = load(path)
        if cap.approval_state != "approved":
            cap = with_changes(cap, approval_state="approved", approved_by="test")
        copy = tmp_path / path.name
        Registry(tmp_path).register(save(cap, copy))
        return copy

    def go(
        capability: Path,
        inputs: dict[str, str] = INPUTS,
        *,
        inject: str | None = None,
        consent: bool = False,
        token: str | None = None,
        key: str | None = None,
    ) -> ReplayResult:
        path = approved(capability)
        if consent:
            token = tokens.mint(
                load(path), tenant, inputs, approved_by="test", key=SIGNING_KEY.encode()
            )
        return replay(
            path,
            tenant=tenant,
            policy=policy,
            invocation=Invocation(
                inputs=inputs,
                inject=inject,
                idempotency_key=key,
                approval=ApprovalGrant(token=token) if token else None,
            ),
            runs_dir=tmp_path / "runs",
            environ=ENV,
        )

    go.token = lambda inputs=INPUTS: tokens.mint(  # type: ignore[attr-defined]
        load(approved(RECORD)), tenant, inputs, approved_by="test", key=SIGNING_KEY.encode()
    )
    return go


def test_a_desktop_capability_replays_with_no_model_and_no_browser(run) -> None:
    result = run(COMPUTE, {"first": "12.5", "second": "4", "operation": "Divide"})
    assert isinstance(result, Success), result
    assert result.outputs == {"result": "3.125"} and result.side_effect == "none"
    assert set(result.locator_rungs_used.values()) == {"role_name"}
    # Screenshots are taken where the surface can; this one cannot, and says so.
    assert result.evidence.screenshots == []
    assert any("takes no screenshots" in w for w in result.warnings)


def test_a_business_answer_from_the_desktop_is_typed(run) -> None:
    result = run(COMPUTE, {"first": "1", "second": "0", "operation": "Divide"})
    assert isinstance(result, BusinessOutcome)
    assert (result.code, result.side_effect) == ("DIVIDE_BY_ZERO", "none")


@pytest.mark.parametrize(
    ("inject", "code", "says"),
    [
        ("renamed_button", "LOCATOR_UNRESOLVED", "role_name: 0 matches"),
        ("ambiguous", "LOCATOR_UNRESOLVED", "role_name: 2 matches"),
        ("disabled", "ACTION_FAILED", "disabled"),
        ("modal", "ACTION_FAILED", "unexpected alert ('The rate service is unavailable.')"),
    ],
)
def test_desktop_faults_stop_safely_with_the_same_codes_as_the_web(
    run, inject: str, code: str, says: str
) -> None:
    result = run(COMPUTE, inject=inject)
    assert isinstance(result, Failure)
    assert (result.code, result.side_effect) == (code, "none")
    assert says in result.message


def test_the_commit_needs_consent_and_one_consent_commits_once(run, ledger: Path) -> None:
    refused = run(RECORD)
    assert isinstance(refused, Failure)
    assert (refused.code, refused.escalation_reason) == ("POLICY_BLOCKED", "NEEDS_APPROVAL")
    assert lines(ledger) == []  # Record was never clicked

    token = run.token()
    done = run(RECORD, token=token, key="k-1")
    assert isinstance(done, Success), done
    assert (done.side_effect, done.outputs) == ("committed", {"result": "50.0"})
    assert lines(ledger) == ["12.5 Multiply 4 = 50.0"]

    # The same request again is the first run's result, not a second entry.
    again = run(RECORD, token=token, key="k-1")
    assert again.cached and again.run_id == done.run_id
    # The same consent for a new request is refused: it was spent.
    spent = run(RECORD, token=token)
    assert isinstance(spent, Failure) and "already used" in spent.message
    assert len(lines(ledger)) == 1


def test_a_desktop_run_the_surface_cannot_serve_is_refused_before_it_starts(
    tmp_path: Path,
) -> None:
    """The UIA surface takes no screenshots, so a run that asks for them is
    refused before anything starts, rather than run without its evidence. The
    tenant's launch command does not exist: had the run tried to start the
    application, it would have failed differently."""
    tenant = Tenant(
        id="desk",
        app_family="deskcalc",
        base_url="uia://deskcalc",
        desktop=DesktopApp(launch=["no-such-program.exe"]),
    )
    result = replay(
        COMPUTE,
        tenant=tenant,
        policy=load_policy(REPO / "policies" / "deskcalc.yaml", tenant),
        invocation=Invocation(inputs=INPUTS),
        runs_dir=tmp_path / "runs",
        allow_draft=True,
        config=ReplayConfig(screenshots=True),
    )
    assert isinstance(result, Failure)
    assert (result.code, result.side_effect) == ("SURFACE_INCOMPATIBLE", "none")
    assert "screenshots" in result.message and not (tmp_path / "runs").exists()
