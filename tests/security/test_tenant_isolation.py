"""Tenant isolation: nothing made for one tenant acts for another.

Two tenants run the same product here (``alpha`` and ``beta``, both
``legacy-core``), which is the case isolation is about: with different
products, the app-family check alone would keep them apart. Each has its own
operator credential and its own approval signing key.

Every invocation resolves, from its tenant alone, the deployment, the
credentials, the policy and the capabilities it may run. The records the
runtime keeps between requests (idempotency results, workflow journals,
intervention requests) are keyed by tenant, so a key, a token or a request
id that one tenant's caller holds opens nothing of another's.

No browser is started. A run that gets past every check reaches the surface
factory, which raises ``Reached``: that is how a test tells "would have
executed" from "was refused".
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from cua.api.access import AccessConfig, Gate
from cua.artifact.schema import Capability
from cua.artifact.store import load, save, with_changes
from cua.escalation.controller import ControlStore
from cua.escalation.operator_app import Console, ConsoleError
from cua.escalation.requests import InterventionRequest, Queue, new_resume_token
from cua.evidence.logger import utc_now
from cua.policy import tokens
from cua.policy.allowlist import load_policy
from cua.policy.tokens import TokenRefused
from cua.registry.store import Registry
from cua.replay.invocation import ApprovalGrant, Invocation
from cua.replay.result import (
    Failure,
    IdempotencyCache,
    IdempotencyConflict,
    ReplayResult,
    Success,
    fingerprint,
)
from cua.replay.runner import replay
from cua.secrets.resolver import SecretError, resolve
from cua.tenant import SecretBinding, Tenant, load_tenant, shared_bindings
from cua.workflow import journal
from cua.workflow.journal import Entry, Journal
from tests.conftest import REPO_ROOT

GOAL1 = REPO_ROOT / "capabilities" / "member_savings_balance.json"
GOAL2 = REPO_ROOT / "capabilities" / "open_subaccount.json"
INPUTS = {"member_id": "10003"}
KEY = "same-key"


def tenant(id: str, port: int, **extra: object) -> Tenant:
    up = id.upper()
    return Tenant.model_validate(
        {
            "id": id,
            "app_family": "legacy-core",
            "base_url": f"http://127.0.0.1:{port}",
            "secrets": {
                "mockcore/operator": SecretBinding(
                    var=f"{up}_OPERATOR", format="username:password"
                ),
                tokens.SIGNING_KEY: SecretBinding(var=f"{up}_SIGNING_KEY"),
            },
            **extra,
        }
    )


ALPHA = tenant("alpha", 8000)
BETA = tenant("beta", 8001)
ENV = {
    "ALPHA_OPERATOR": "alpha-op:alpha-pass",
    "BETA_OPERATOR": "beta-op:beta-pass",
    "ALPHA_SIGNING_KEY": "a" * 64,
    "BETA_SIGNING_KEY": "b" * 64,
}


class Reached(Exception):
    """The run got past every check and asked for a surface."""


def reached() -> None:
    raise Reached


def run(
    t: Tenant,
    runs: Path,
    *,
    path: Path = GOAL1,
    key: str | None = None,
    token: str | None = None,
    inputs: dict[str, str] | None = None,
) -> ReplayResult:
    return replay(
        path,
        tenant=t,
        policy=load_policy(REPO_ROOT / "policies" / "default.yaml", t),
        invocation=Invocation(
            inputs=inputs or INPUTS,
            idempotency_key=key,
            approval=ApprovalGrant(token=token) if token else None,
        ),
        runs_dir=runs,
        surface=reached,  # type: ignore[arg-type]
        environ=ENV,
    )


def copy_with(tmp_path: Path, src: Path, **changes: object) -> Path:
    """An approved copy of a committed capability with ``changes`` made."""
    path = tmp_path / "caps" / src.name
    path.parent.mkdir(parents=True, exist_ok=True)
    save(with_changes(load(src), **changes), path)
    return path


def refused(result: ReplayResult, *, runs: Path, says: str) -> None:
    assert isinstance(result, Failure), result
    assert (result.code, result.side_effect) == ("POLICY_BLOCKED", "none")
    assert says in result.message, result.message
    assert not runs.exists(), "a refused run leaves no run directory"


# -- the baseline: each tenant runs what is its own -------------------------------------


def test_each_tenant_runs_the_shared_capability_on_its_own_deployment(tmp_path: Path) -> None:
    for t in (ALPHA, BETA):
        runs = tmp_path / t.id
        with pytest.raises(Reached):
            run(t, runs)
        (run_json,) = runs.glob("*/run.json")
        recorded = json.loads(run_json.read_text(encoding="utf-8"))["tenant"]
        assert recorded == {"id": t.id, "app_family": "legacy-core", "base_url": t.base_url}


# -- a capability from tenant A cannot execute against tenant B --------------------------


def test_a_capability_the_tenant_file_does_not_list_is_refused(tmp_path: Path) -> None:
    only_alpha = BETA.model_copy(update={"capabilities": ["open_subaccount"]})
    runs = tmp_path / "runs"
    refused(run(only_alpha, runs), runs=runs, says="tenant 'beta' does not run")


def test_a_capability_of_another_app_family_is_refused(tmp_path: Path) -> None:
    elsewhere = BETA.model_copy(update={"app_family": "other-core"})
    runs = tmp_path / "runs"
    refused(run(elsewhere, runs), runs=runs, says="is for app family 'legacy-core'")


def test_the_registry_scopes_a_capability_to_the_tenants_that_run_it(tmp_path: Path) -> None:
    tenants = tmp_path / "tenants"
    tenants.mkdir()
    shutil.copy(REPO_ROOT / "tenants" / "local.yaml", tenants / "local.yaml")
    narrow = tenants / "narrow.yaml"
    narrow.write_text(
        "id: narrow\napp_family: legacy-core\nbase_url: http://127.0.0.1:8002\n"
        "capabilities: [open_subaccount]\n",
        encoding="utf-8",
    )
    caps = tmp_path / "capabilities"
    caps.mkdir()
    for src in (GOAL1, GOAL2):
        shutil.copy(src, caps / src.name)
    registry = Registry(caps, state_dir=tmp_path / "state", tenants_dir=tenants)
    scope = {v.record.name: v.record.tenant_scope for v in registry.all_versions()}
    assert scope == {"member_savings_balance": ["local"], "open_subaccount": ["local", "narrow"]}


def test_one_check_decides_what_a_tenant_runs() -> None:
    narrow = ALPHA.model_copy(update={"capabilities": ["open_subaccount"]})
    assert ALPHA.refusal("member_savings_balance", "legacy-core") is None
    assert narrow.refusal("open_subaccount", "legacy-core") is None
    assert "does not run member_savings_balance" in (
        narrow.refusal("member_savings_balance", "legacy-core") or ""
    )
    assert "runs 'legacy-core'" in (ALPHA.refusal("x", "deskcalc") or "")


# -- a secret reference from tenant A cannot resolve for tenant B -------------------------


def test_the_resolver_resolves_a_reference_only_for_its_own_tenant() -> None:
    assert resolve("secret://beta/mockcore/operator", BETA, environ=ENV).field("username") == (
        "beta-op"
    )
    with pytest.raises(SecretError, match="belongs to tenant 'alpha', not 'beta'"):
        resolve("secret://alpha/mockcore/operator", BETA, environ=ENV)


def test_a_capability_naming_another_tenants_credential_is_refused_before_it_is_read(
    tmp_path: Path,
) -> None:
    cap = load(GOAL1)
    creds = {
        n: c.model_copy(update={"ref": "secret://alpha/mockcore/operator"})
        for n, c in cap.credentials.items()
    }
    path = copy_with(tmp_path, GOAL1, credentials=creds)
    runs = tmp_path / "runs"
    refused(run(BETA, runs, path=path), runs=runs, says="belongs to tenant 'alpha'")


def test_no_capability_may_use_the_systems_own_secrets(tmp_path: Path) -> None:
    """The signing key is a secret of the tenant's, bound like the operator's
    password, so ``{tenant.id}`` alone would resolve it. A capability that
    asks for it could type the key that makes consent into a page."""
    cap = load(GOAL1)
    creds = {
        n: c.model_copy(update={"ref": "secret://{tenant.id}/cua/approval-signing-key"})
        for n, c in cap.credentials.items()
    }
    path = copy_with(tmp_path, GOAL1, credentials=creds)
    runs = tmp_path / "runs"
    refused(run(ALPHA, runs, path=path), runs=runs, says="one of the system's own secrets")


# -- an approval token for tenant A is rejected for tenant B ------------------------------


def mint(t: Tenant, cap: Capability, key_of: Tenant | None = None) -> str:
    key = tokens.signing_key(key_of or t, environ=ENV)
    return tokens.mint(cap, t, INPUTS, approved_by="ops", key=key)


def test_a_token_signed_for_tenant_a_does_not_verify_for_tenant_b() -> None:
    cap = load(GOAL2)
    token = mint(ALPHA, cap)
    tokens.verify(token, cap, ALPHA, INPUTS, key=tokens.signing_key(ALPHA, environ=ENV))
    with pytest.raises(TokenRefused, match="does not verify with tenant 'beta'"):
        tokens.verify(token, cap, BETA, INPUTS, key=tokens.signing_key(BETA, environ=ENV))


def test_a_token_naming_tenant_a_is_refused_for_b_even_under_one_key() -> None:
    """Were the key shared, the claim would still say whose consent it is."""
    cap = load(GOAL2)
    token = mint(ALPHA, cap, key_of=BETA)  # alpha's claims, beta's key
    with pytest.raises(TokenRefused, match="tenant 'alpha'"):
        tokens.verify(token, cap, BETA, INPUTS, key=tokens.signing_key(BETA, environ=ENV))


def test_a_run_on_tenant_b_carrying_tenant_as_token_is_refused_before_a_browser(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    token = mint(ALPHA, load(GOAL1))
    refused(run(BETA, runs, token=token), runs=runs, says="approval refused")
    with pytest.raises(Reached):
        run(ALPHA, tmp_path / "alpha", token=token)


def test_no_two_tenants_share_a_secret() -> None:
    """A signing key two tenants read lets whoever may consent for one sign
    consent for the other, whatever tenant the token names. The repository's
    tenants each have their own."""
    repo = [load_tenant(str(p)) for p in sorted((REPO_ROOT / "tenants").glob("*.yaml"))]
    assert len(repo) >= 2 and shared_bindings(repo) == []
    reuses = BETA.model_copy(update={"secrets": ALPHA.secrets})  # alpha's bindings, both
    problems = shared_bindings([ALPHA, reuses])
    assert len(problems) == 2
    assert all("secret://beta/" in p and "secret://alpha/" in p for p in problems)
    assert "$ALPHA_SIGNING_KEY; each tenant needs its own" in problems[1]


def test_the_api_will_not_serve_two_tenants_bound_to_one_key(tmp_path: Path) -> None:
    files = tmp_path / "tenants"
    files.mkdir()
    for name in ("one", "two"):
        (files / f"{name}.yaml").write_text(
            f"id: {name}\napp_family: legacy-core\nbase_url: http://127.0.0.1:8000\n"
            "secrets:\n  cua/approval-signing-key: {provider: file, path: .cua/shared.key}\n",
            encoding="utf-8",
        )
    config = AccessConfig.model_validate({"tenants": {"one": {}, "two": {}}})
    with pytest.raises(ValueError, match="each tenant needs its own"):
        Gate(config, root=tmp_path, tenants_dir=files)


# -- idempotency records do not collide across tenants ------------------------------------


def test_one_key_on_two_tenants_is_two_requests(tmp_path: Path) -> None:
    request = fingerprint("member_savings_balance", 3, INPUTS)
    answer = Success(
        outputs={"savings_balance": "1411.21"}, capability="member_savings_balance",
        capability_version=3,
    )  # fmt: skip
    IdempotencyCache(tmp_path, ALPHA.id).put(KEY, request, answer)
    assert IdempotencyCache(tmp_path, ALPHA.id).get(KEY, request) is not None
    assert IdempotencyCache(tmp_path, BETA.id).get(KEY, request) is None
    # Nor is beta's use of the key a conflict with alpha's request.
    other = fingerprint("member_savings_balance", 3, {"member_id": "10004"})
    assert IdempotencyCache(tmp_path, BETA.id).get(KEY, other) is None


def test_tenant_b_is_never_answered_with_tenant_as_stored_result(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    stored = Success(
        outputs={"savings_balance": "1411.21"}, capability="member_savings_balance",
        capability_version=load(GOAL1).version,
    )  # fmt: skip
    request = fingerprint(stored.capability, stored.capability_version, INPUTS)
    IdempotencyCache(runs, ALPHA.id).put(KEY, request, stored)

    cached = run(ALPHA, runs, key=KEY)
    assert isinstance(cached, Success) and cached.cached
    with pytest.raises(Reached):
        run(BETA, runs, key=KEY)  # its own request, run on its own deployment


def legacy(runs: Path, key: str, result: ReplayResult, *, ran_on: str | None) -> None:
    """A record as the cache kept it before it was per tenant: under the key
    alone, with no tenant in it."""
    cache = IdempotencyCache(runs, "unused")
    cache.dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    request = fingerprint(result.capability, result.capability_version, INPUTS)
    record = {"fingerprint": request, "result": result.model_dump(mode="json")}
    (cache.dir / f"{digest}.json").write_text(json.dumps(record), encoding="utf-8")
    if ran_on is not None and result.run_id is not None:
        (runs / result.run_id).mkdir(parents=True)
        (runs / result.run_id / "run.json").write_text(
            json.dumps({"tenant": {"id": ran_on}}), encoding="utf-8"
        )


def test_a_record_from_before_tenants_is_its_runs_tenants_only(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    committed = Success(
        outputs={}, capability="open_subaccount", capability_version=3, run_id="run_A",
        side_effect="committed",
    )  # fmt: skip
    legacy(runs, KEY, committed, ran_on=ALPHA.id)
    request = fingerprint("open_subaccount", 3, INPUTS)
    assert IdempotencyCache(runs, ALPHA.id).get(KEY, request) is not None
    assert IdempotencyCache(runs, BETA.id).get(KEY, request) is None


def test_a_record_from_before_tenants_whose_tenant_is_unknown_is_not_guessed(
    tmp_path: Path,
) -> None:
    request = fingerprint("open_subaccount", 3, INPUTS)
    # Refused before it ran: nothing happened, so the request simply runs.
    runs = tmp_path / "refused"
    blocked = Failure(code="POLICY_BLOCKED", capability="open_subaccount", capability_version=3)
    legacy(runs, KEY, blocked, ran_on=None)
    assert IdempotencyCache(runs, BETA.id).get(KEY, request) is None
    # It may have committed, on a tenant nobody can name: neither answer is safe.
    runs = tmp_path / "unknown"
    cut = Failure(
        code="INTERRUPTED", capability="open_subaccount", capability_version=3,
        side_effect="unknown", run_id="run_gone",
    )  # fmt: skip
    legacy(runs, KEY, cut, ran_on=None)
    with pytest.raises(IdempotencyConflict, match="use a new key"):
        IdempotencyCache(runs, BETA.id).get(KEY, request)


def test_workflow_journals_are_per_tenant(tmp_path: Path) -> None:
    entry = Entry(fingerprint="f", workflow="w", workflow_version=1, pins={})
    Journal(tmp_path, ALPHA.id).put(KEY, entry)
    assert Journal(tmp_path, ALPHA.id).get(KEY) == entry
    assert Journal(tmp_path, BETA.id).get(KEY) is None


def test_a_workflow_step_run_is_found_only_on_its_own_tenant(tmp_path: Path) -> None:
    step_key = journal.step_key("w", KEY, "open")
    run_dir = tmp_path / "run_1"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps({"tenant": {"id": ALPHA.id}, "request": {"idempotency_key": step_key}}),
        encoding="utf-8",
    )
    assert journal.find_run(tmp_path, step_key, ALPHA.id) == run_dir
    assert journal.find_run(tmp_path, step_key, BETA.id) is None


# -- evidence is tenant-scoped ------------------------------------------------------------


def post_request(runs: Path, t: Tenant, n: int) -> str:
    run_dir = runs / f"run_{t.id}"
    run_dir.mkdir(parents=True)
    ControlStore(run_dir).start(run_dir.name)
    now = utc_now()
    request = InterventionRequest(
        id=f"req_{n:026d}", run_id=run_dir.name, kind="replay", capability="open_subaccount",
        tenant=t.id, step_id="review.submit", reason_code="NEEDS_APPROVAL", code="POLICY_BLOCKED",
        message="needs consent", options=["approve", "abort"], cdp_url="http://127.0.0.1:9",
        created_at=now, expires_at="9999-12-31T00:00:00Z",
    )  # fmt: skip
    # The request lives in its run directory; the queue holds a pointer to it.
    (run_dir / "interventions").mkdir()
    (run_dir / "interventions" / f"{request.id}.json").write_text(
        request.model_dump_json(), encoding="utf-8"
    )
    Queue(runs).post(request, run_dir, new_resume_token())
    return request.id


def test_a_tenants_console_shows_and_decides_only_its_own_requests(tmp_path: Path) -> None:
    alpha_req = post_request(tmp_path, ALPHA, 1)
    beta_req = post_request(tmp_path, BETA, 2)

    console = Console(tmp_path, tenant=BETA.id)
    assert [v.request.id for v in console.views()] == [beta_req]
    assert console.view(beta_req).request.tenant == "beta"
    for look in (console.view, console.screenshot):
        with pytest.raises(ConsoleError) as seen:
            look(alpha_req)
        assert seen.value.status == 404
    with pytest.raises(ConsoleError) as seen:
        console.act(alpha_req, "abort", by="beta-operator", why="not mine")
    assert seen.value.status == 404
    assert ControlStore(tmp_path / "run_alpha").read().state == "AUTOMATION", "untouched"

    assert {v.request.tenant for v in Console(tmp_path).views()} == {"alpha", "beta"}


# -- the tenant file: one resolution, no silent defaults ----------------------------------


def test_every_invocation_resolves_its_deployment_policy_and_credentials_from_the_tenant() -> None:
    local = load_tenant("local", root=REPO_ROOT / "tenants")
    desk = load_tenant("desk", root=REPO_ROOT / "tenants")
    assert (local.base_url, local.policy_file.as_posix()) == (
        "http://127.0.0.1:8000",
        "policies/default.yaml",
    )
    assert (desk.base_url, desk.policy_file.as_posix()) == (
        "uia://deskcalc",
        "policies/deskcalc.yaml",
    )
    assert local.app_secrets == ["mockcore/operator"]
    assert tokens.signing_key_ref(desk) != tokens.signing_key_ref(local)
    assert ALPHA.policy_file.as_posix() == "policies/default.yaml"  # none named


def test_a_tenant_naming_an_overlay_is_refused_not_run_unadapted() -> None:
    with pytest.raises(ValueError, match="applies no overlays"):
        Tenant(id="x", app_family="legacy-core", base_url="http://h", overlay="x-wording")
