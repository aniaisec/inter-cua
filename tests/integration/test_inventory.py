"""Fresh inventory project through ordinary CLI and replay/handoff entry points."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import yaml

from cua.artifact.store import load
from cua.escalation.channel import HandoffSettings
from cua.policy import tokens
from cua.policy.allowlist import load_policy
from cua.project import ProjectContext
from cua.replay.engine import ReplayConfig
from cua.replay.invocation import ApprovalGrant, Invocation
from cua.replay.result import BusinessOutcome, Escalated, Failure, Success
from cua.replay.runner import replay, resume
from cua.surface.playwright_surface import PlaywrightSurface
from cua.tenant import load_tenant
from tests.conftest import REPO_ROOT, free_port
from tests.integration import test_handoff as handoff_helpers

pytestmark = pytest.mark.browser
console = handoff_helpers.console


def cli(root, *args, expected=0):
    result = subprocess.run(
        [sys.executable, "-m", "cua.cli", "--root", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    return result.stdout


@pytest.fixture(scope="module")
def inventory_project(tmp_path_factory, browser_session):
    root = tmp_path_factory.mktemp("inventory project with spaces")
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "examples.inventory.app.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail(proc.stdout.read().decode() if proc.stdout else "target exited")
            try:
                if httpx.get(url + "/login", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.1)
        else:
            pytest.fail("inventory target did not start")
        cli(root, "init", str(root), "--template", "inventory")
        assert not list((root / "capabilities").glob("*.json"))
        tenant_path = root / "tenants/local.yaml"
        tenant = yaml.safe_load(tenant_path.read_text())
        tenant["base_url"] = url
        tenant_path.write_text(yaml.safe_dump(tenant))
        (root / ".env").write_bytes((root / ".env.example").read_bytes())
        scenario = json.loads((root / "scenario.json").read_text())
        for name, spec in scenario.items():
            args = [
                "discover",
                "--name",
                name,
                "--entry",
                "/login",
                "--goal",
                spec["goal"],
                "--llm",
                "scripted",
                "--script",
                str(root / "scripts/discovery" / f"{name}.yaml"),
            ]
            for param in spec["params"]:
                args.extend(["--param", param])
            for output in spec["outputs"]:
                args.extend(["--output", output])
            if name == "adjust_stock":
                args.append("--auto-approve-risky")
            cli(root, *args)
            path = root / "capabilities" / f"{name}.json"
            draft = load(path)
            assert draft.approval_state == "draft" and draft.approved_by is None
            refused = json.loads(
                cli(root, "replay", name, "--input", "item_code=SKU-001", expected=1)
            )
            assert refused["code"] == "POLICY_BLOCKED" and refused["evidence"]["run_dir"] is None
            cli(root, "describe", name)
            cli(root, "approve", name, "--by", "inventory test")
            assert load(path).content_hash() == draft.content_hash()
        assert len(list((root / "evidence/runs").glob("run_*/model_calls.jsonl"))) == 2
        yield root, url, scenario
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        if proc.stdout:
            proc.stdout.close()


class InventoryHarness:
    def __init__(self, project, page, tmp_path, browser_session):
        self.root, self.url, self.scenario = project
        self.project = ProjectContext.resolve(self.root, environ=dict(os.environ))
        self.tenant = load_tenant("local", project=self.project)
        self.policy = load_policy(Path(self.tenant.policy), self.tenant)
        self.environ = self.project.environ
        self.page = page
        self.cdp_url = browser_session.cdp_url
        self.runs = tmp_path / "runs"
        self.launches = 0

    @contextmanager
    def surface(self):
        self.launches += 1
        yield PlaywrightSurface(self.page, cdp_url=self.cdp_url)

    def oracle(self):
        response = httpx.get(self.url + "/_test/state")
        response.raise_for_status()
        return response.json()

    def token(self, inputs):
        key = (self.root / ".cua/approval-signing.key").read_text().strip().encode()
        return tokens.mint(
            load(self.root / "capabilities/adjust_stock.json"),
            self.tenant,
            inputs,
            approved_by="inventory test",
            key=key,
        )

    def run(
        self,
        name="item_lookup",
        *,
        inputs=None,
        inject=None,
        token=None,
        key=None,
        handoff=None,
        tenant=None,
        policy=None,
    ):
        return replay(
            self.root / "capabilities" / f"{name}.json",
            tenant=tenant or self.tenant,
            policy=policy or self.policy,
            invocation=Invocation(
                inputs=inputs or {"item_code": "SKU-002"},
                inject=inject,
                approval=ApprovalGrant(token=token) if token else None,
                idempotency_key=key,
            ),
            runs_dir=self.runs,
            config=ReplayConfig(screenshots=True),
            surface=self.surface,
            environ=self.environ,
            handoff=handoff,
        )


@pytest.fixture
def inventory(inventory_project, page, tmp_path, browser_session):
    return InventoryHarness(inventory_project, page, tmp_path, browser_session)


def no_replay_model_calls(result):
    assert result.evidence.run_dir
    assert not (Path(result.evidence.run_dir) / "model_calls.jsonl").exists()


def test_second_input_and_not_found(inventory):
    before = inventory.oracle()
    spec = inventory.scenario["item_lookup"]
    result = inventory.run(inputs=spec["second_inputs"])
    assert isinstance(result, Success) and result.outputs == spec["second_outputs"]
    assert result.side_effect == "none"
    no_replay_model_calls(result)
    missing = inventory.run(inputs={"item_code": "SKU-999"})
    assert isinstance(missing, BusinessOutcome) and missing.code == "NOT_FOUND"
    assert missing.step_id == "lookup.submit"
    assert inventory.oracle() == before


def test_consent_input_binding_and_same_key_retry_commit_exactly_once(inventory):
    spec = inventory.scenario["adjust_stock"]
    inputs = spec["replay_inputs"]
    before = inventory.oracle()
    blocked = inventory.run("adjust_stock", inputs=inputs)
    assert isinstance(blocked, Failure) and blocked.escalation_reason == "NEEDS_APPROVAL"
    assert inventory.oracle() == before
    token = inventory.token(inputs)
    changed = inventory.run("adjust_stock", inputs={**inputs, "quantity_change": "-1"}, token=token)
    assert isinstance(changed, Failure) and changed.code == "POLICY_BLOCKED"
    assert inventory.oracle() == before
    result = inventory.run("adjust_stock", inputs=inputs, token=token, key="inventory-write")
    assert isinstance(result, Success) and result.side_effect == "committed"
    no_replay_model_calls(result)
    assert result.outputs["quantity"] == before["quantities"]["SKU-001"] - 3
    after = inventory.oracle()
    assert after["commit_count"] == before["commit_count"] + 1
    assert after["commit_posts"] == before["commit_posts"] + 1
    assert after["adjustments"][-1]["receipt"] == result.outputs["receipt"]
    launches = inventory.launches
    cached = inventory.run("adjust_stock", inputs=inputs, token=token, key="inventory-write")
    assert isinstance(cached, Success) and cached.cached and cached.run_id == result.run_id
    assert inventory.launches == launches and inventory.oracle() == after
    reused = inventory.run("adjust_stock", inputs=inputs, token=token)
    assert isinstance(reused, Failure) and reused.code == "POLICY_BLOCKED"
    assert inventory.launches == launches and inventory.oracle() == after


@pytest.mark.parametrize(
    "inputs,inject,code",
    [
        ({"item_code": "SKU-001", "quantity_change": "-100"}, None, "INSUFFICIENT_STOCK"),
        ({"item_code": "SKU-001", "quantity_change": "0"}, None, "VALIDATION_ERROR"),
        ({"item_code": "SKU-001", "quantity_change": "-1"}, "validation_fault", "VALIDATION_ERROR"),
    ],
)
def test_stock_business_outcomes_start_no_commit(inventory, inputs, inject, code):
    before = inventory.oracle()
    result = inventory.run("adjust_stock", inputs=inputs, inject=inject)
    assert isinstance(result, BusinessOutcome) and result.code == code
    assert result.step_id == "adjust.submit" and result.side_effect == "none"
    no_replay_model_calls(result)
    assert inventory.oracle() == before


def test_invalid_inputs_and_denials_start_no_target_action(inventory):
    before = inventory.oracle()
    invalid = inventory.run(
        "adjust_stock", inputs={"item_code": "SKU-001", "quantity_change": "oops"}
    )
    assert isinstance(invalid, Failure) and invalid.code == "INPUT_INVALID"
    denied = inventory.run(tenant=inventory.tenant.model_copy(update={"capabilities": []}))
    assert isinstance(denied, Failure) and denied.code == "POLICY_BLOCKED"
    assert inventory.launches == 0 and inventory.oracle() == before
    with patch.object(
        PlaywrightSurface, "act", side_effect=AssertionError("policy started an action")
    ):
        blocked = inventory.run(policy=inventory.policy.model_copy(update={"allowed_origins": []}))
    assert isinstance(blocked, Failure) and blocked.code == "POLICY_BLOCKED"
    assert inventory.oracle() == before


def test_expired_session_relogs_in_from_a_validated_checkpoint(inventory):
    result = inventory.run(inject="session_expired")
    assert isinstance(result, Success) and result.outputs["quantity"] == 4
    assert [r.code for r in result.recoveries] == ["SESSION_EXPIRED"]
    assert result.recoveries[0].resumed_after_checkpoint == "cp.logged_in"
    no_replay_model_calls(result)


@pytest.mark.parametrize(
    "inject,code",
    [
        ("renamed_control", "LOCATOR_UNRESOLVED"),
        # Both semantic rungs are ambiguous. The final coordinate rung names
        # one button, but unattended replay refuses a pixels-only target.
        ("ambiguous_control", "LOCATOR_UNRESOLVED"),
        ("changed_screen", "CHECKPOINT_FAILED"),
    ],
)
def test_changed_or_ambiguous_controls_refuse_before_a_write(inventory, inject, code):
    before = inventory.oracle()
    result = inventory.run(inject=inject)
    assert isinstance(result, Failure) and result.code == code
    assert result.side_effect == "none" and result.evidence.screenshots
    if inject in ("renamed_control", "ambiguous_control"):
        assert inventory.page.url.endswith("/lookup")
    if inject == "ambiguous_control":
        assert "pixels only" in result.message
    assert inventory.oracle() == before


def test_changed_screen_human_takeover_then_resume_does_not_repeat_lookup(inventory, console):
    first = inventory.run(
        inject="changed_screen", handoff=HandoffSettings(wait_s=0, operator_url=console, poll_s=0.1)
    )
    assert isinstance(first, Escalated)
    run_dir = Path(first.evidence.run_dir)
    before = (run_dir / "log.jsonl").read_text()
    inventory.page.wait_for_timeout(200)
    assert (run_dir / "log.jsonl").read_text() == before

    def show_verified_item(page):
        page.get_by_role("link", name="Show item details", exact=True).click()

    person = handoff_helpers.Person(console, decide="resume", act=show_verified_item)
    person.start()
    person.finish()

    @contextmanager
    def same_page(_):
        with inventory.surface() as surface:
            yield surface

    result = resume(
        first.resume_token, runs_dir=inventory.runs, surface=same_page, environ=inventory.environ
    )
    assert isinstance(result, Success) and result.outputs["quantity"] == 4
    assert result.handoffs[0].decision == "hand_back"
    assert result.handoffs[0].human_actions_count >= 1
    events = [json.loads(line) for line in (run_dir / "log.jsonl").read_text().splitlines()]
    index = max(i for i, e in enumerate(events) if e["event"] == "handoff.resumed")
    assert not [e for e in events[index:] if e["event"] == "step.start"]
    no_replay_model_calls(result)
