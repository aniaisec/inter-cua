"""Execute this directory's machine-readable scenario through ordinary entry points."""

from __future__ import annotations

import json
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import Page

from cua.benchmark.environment import free_port
from cua.demo.runner import server
from examples._session import Session
from examples._support import ExampleError, check_subset, require


def run(s: Session, stack: ExitStack) -> dict[str, Any]:
    port = free_port()
    console = f"http://127.0.0.1:{port}"
    stack.enter_context(
        server(
            ["cua.cli", "--root", str(s.work), "operator", "--port", str(port)],
            console + "/api/requests",
            s.cwd,
            s.env,
        )
    )
    first = s.cli(
        "replay",
        "item_lookup",
        *s.inputs(s.spec["inputs"]),
        "--inject",
        s.spec["inject"],
        "--handoff",
        "--handoff-wait",
        "0",
        "--operator-url",
        console,
        expected=3,
    )
    require(first["kind"] == "escalated", "expected a live handoff")
    run_dir = Path(first["evidence"]["run_dir"])
    log = (run_dir / "log.jsonl").read_bytes()
    request_id = first["request_id"]
    with httpx.Client(base_url=console, timeout=30) as http:
        http.post(
            f"/api/requests/{request_id}/take_control", json={"by": "example-operator (scripted)"}
        ).raise_for_status()
        time.sleep(0.3)
        require(
            (run_dir / "log.jsonl").read_bytes() == log, "automation acted while a person held it"
        )

    def show_item(page: Page) -> None:
        page.get_by_role("link", name="Show item details", exact=True).click()

    # Perform the explicitly scripted operator action on the same browser.
    from playwright.sync_api import sync_playwright

    from cua.escalation.capture import find_page

    with httpx.Client(base_url=console, timeout=30) as http:
        request = http.get(f"/api/requests/{request_id}").json()["request"]
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(request["cdp_url"])
            try:
                page = find_page(browser.contexts, request["target_id"])
                if page is None:
                    raise ExampleError("handoff page missing")
                show_item(page)
                page.wait_for_timeout(500)
            finally:
                browser.close()
        http.post(
            f"/api/requests/{request_id}/resume", json={"by": "example-operator (scripted)"}
        ).raise_for_status()
    s.record(
        ["operator", "take control; Show item details; hand back (scripted)"],
        0,
        {"kind": "human_action", "outputs": {"action": "show_item_details"}},
    )
    done = s.cli("resume", first["resume_token"], secret=first["resume_token"])
    s.no_models(done)
    check_subset(done, {"kind": "success", "outputs": s.spec["outputs"]})
    require(
        done["handoffs"][0]["resumed_after_checkpoint"] == s.spec["checkpoint"],
        "hand-back did not validate the expected checkpoint",
    )
    events = [json.loads(line) for line in (run_dir / "log.jsonl").read_text().splitlines()]
    index = max(i for i, event in enumerate(events) if event["event"] == "handoff.resumed")
    require(
        not any(event["event"] == "step.start" for event in events[index:]),
        "hand-back repeated completed steps",
    )
    return {
        "automation_paused": True,
        "checkpoint_validated": True,
        "completed_steps_repeated": False,
        "kind": done["kind"],
    }
