"""The discovery loop against the real mock app, with the bundled script as the model.

The unit tests cover every way a run can end; these prove the loop and the
real surface agree — refs from a live frameset, typing that lands, a screen
that is waited for rather than read half-loaded, and a password that the live
accessibility tree reports and the run directory never contains.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from playwright.sync_api import Page

from cua.agent.goal import Goal, OutputSpec, ParamSpec
from cua.agent.llm import ScriptedClient
from cua.agent.loop import DiscoveryLoop, DiscoveryOutcome
from cua.agent.script import load_script
from cua.evidence.logger import RunLog
from cua.policy.allowlist import load_policy
from cua.secrets.resolver import resolve
from cua.surface.playwright_surface import PlaywrightSurface
from cua.tenant import SecretBinding, Tenant
from mockapp.data import MEMBERS
from mockapp.injects import Inject

pytestmark = pytest.mark.browser

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "discovery" / "member_savings_balance.yaml"


def discover(page: Page, mockapp_url: str, run_dir: Path, *, inject: str | None = None):
    tenant = Tenant(
        id="local",
        app_family="legacy-core",
        base_url=mockapp_url,
        secrets={
            "mockcore/operator": SecretBinding(var="CUA_TEST_OPERATOR", format="username:password")
        },
    )
    credential = resolve(
        "secret://local/mockcore/operator",
        tenant,
        environ={"CUA_TEST_OPERATOR": "operator:operator"},
    )
    goal = Goal(
        goal="Look up a member by id and return the current savings balance",
        name="member_savings_balance",
        entry="/login",
        params=[ParamSpec(name="member_id", value="10003")],
        outputs=[
            OutputSpec(name="savings_balance", type="decimal"),
            OutputSpec(name="member_name", optional=True),
        ],
        credentials={"app_login": credential.ref},
    )
    entry = tenant.url("/login") + (f"?inject={inject}" if inject else "")
    log = RunLog(run_dir)
    outcome = DiscoveryLoop(
        surface=PlaywrightSurface(page),
        llm=ScriptedClient(load_script(SCRIPT)),
        goal=goal,
        tenant=tenant,
        policy=load_policy(REPO / "policies" / "default.yaml", tenant),
        credentials={"app_login": credential},
        log=log,
        entry_url=entry,
    ).run()
    return outcome, log


def assert_found_the_seeded_balance(outcome: DiscoveryOutcome) -> None:
    assert outcome.kind == "done", outcome.message
    member = MEMBERS["10003"]
    assert Decimal(outcome.outputs["savings_balance"].normalized) == member.savings
    assert outcome.outputs["member_name"].normalized == member.name


def test_the_bundled_script_discovers_goal_one_on_the_live_app(
    page: Page, mockapp_url: str, tmp_path: Path
) -> None:
    outcome, log = discover(page, mockapp_url, tmp_path / "run")

    assert_found_the_seeded_balance(outcome)
    assert outcome.steps == 6
    assert (log.dir / "script.yaml").exists()
    assert len(list((log.dir / "screenshots").glob("*.png"))) == 6


def test_the_live_password_field_never_reaches_the_run_directory(
    page: Page, mockapp_url: str, tmp_path: Path
) -> None:
    outcome, log = discover(page, mockapp_url, tmp_path / "run")
    assert outcome.kind == "done"

    # After the password was typed, the live tree reported the field's value;
    # the stored observation says *** instead.
    typed = json.loads((log.dir / "observations" / "0002.json").read_text())
    fields = [n for n in typed["nodes"] if n["role"] == "textbox"]
    assert [f["value"] for f in fields] == ["***", "***"]
    for path in log.dir.rglob("*.json*"):
        assert '"value": "operator"' not in path.read_text(encoding="utf-8"), path


def test_a_slow_screen_is_waited_for_before_the_agent_decides(
    page: Page, mockapp_url: str, tmp_path: Path
) -> None:
    """``slow_load`` holds the member detail page for four seconds. Deciding
    during them would mean deciding against the search screen just left."""
    outcome, log = discover(page, mockapp_url, tmp_path / "run", inject=Inject.SLOW_LOAD.value)

    assert_found_the_seeded_balance(outcome)
    after_search = json.loads((log.dir / "observations" / "0005.json").read_text())
    assert any("/member/10003" in f["url"] for f in after_search["frames"])


# Runs `cua discover` for real, with Ctrl+C simulated (``interrupt_main``) one
# second into the four-second slow_load after the Search click, while the loop
# is inside a Playwright wait.
_INTERRUPTED_DISCOVER = """
import _thread, sys, threading
from cua.cli import main
from cua.surface.playwright_surface import PlaywrightSurface
from cua.surface.protocol import Click

act, clicks = PlaywrightSurface.act, []

def interrupting_act(self, action):
    result = act(self, action)
    if isinstance(action, Click):
        clicks.append(action)
        if len(clicks) == 2:  # Sign On, then Search
            threading.Timer(1.0, _thread.interrupt_main).start()
    return result

PlaywrightSurface.act = interrupting_act
sys.exit(main(sys.argv[1:]))
"""


def test_ctrl_c_inside_a_live_wait_is_recorded_and_the_process_exits(
    mockapp_url: str, tmp_path: Path
) -> None:
    """Ctrl+C lands wherever the run happens to be, usually inside a Playwright
    call. The run must still say how it ended, and closing the browser must
    not wait forever on a reply the interrupted call will never read."""
    import os
    import subprocess
    import sys

    tenant = tmp_path / "tenant.yaml"
    tenant.write_text(
        "\n".join(
            [
                "id: local",
                "app_family: legacy-core",
                f"base_url: {mockapp_url}",
                "secrets:",
                "  mockcore/operator: {var: CUA_TEST_OPERATOR, format: 'username:password'}",
            ]
        ),
        encoding="utf-8",
    )
    args = [
        "discover", "--llm", "scripted", "--script", str(SCRIPT),
        "--goal", "Look up a member by id and return the current savings balance",
        "--name", "member_savings_balance", "--entry", "/login",
        "--param", "member_id:string=10003", "--output", "savings_balance:decimal",
        "--tenant", str(tenant), "--inject", Inject.SLOW_LOAD.value,
        "--runs-dir", str(tmp_path / "runs"), "--no-record",
    ]  # fmt: skip
    done = subprocess.run(
        [sys.executable, "-c", _INTERRUPTED_DISCOVER, *args],
        cwd=REPO,
        env={**os.environ, "CUA_TEST_OPERATOR": "operator:operator"},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert done.returncode == 130, done.stderr
    assert "interrupted; the run says so" in done.stderr
    (result_file,) = (tmp_path / "runs").glob("*/result.json")
    result = json.loads(result_file.read_text())
    assert (result["kind"], result["reason"]) == ("interrupted", "KeyboardInterrupt")
