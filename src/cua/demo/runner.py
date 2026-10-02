"""Run the seven Phase 18 demonstrations through the real CLI.

Each session owns a workspace, mock app, console, credentials and signing key.
The operator is the evidence builder's explicitly scripted evidence-bot.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from ulid import ULID

from cua.benchmark.environment import free_port
from cua.evidence.build import BuildError, Call, Person, click_find, expect

ROOT = Path(__file__).resolve().parents[3]


@contextmanager
def server(args: list[str], url: str, cwd: Path, env: dict[str, str]) -> Iterator[None]:
    proc = subprocess.Popen(
        [sys.executable, "-m", *args],
        cwd=cwd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                raise BuildError("demo server exited during startup")
            try:
                if httpx.get(url, timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                raise BuildError("demo server did not become ready")
            time.sleep(0.1)
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def prepare(out: Path, base_url: str) -> tuple[Path, dict[str, str]]:
    """Refuse overwrites; keep all runtime state and approval receipts local."""
    out.mkdir(parents=True, exist_ok=False)
    work = out / "workspace"
    work.mkdir()
    for folder in ("capabilities", "policies", "tenants"):
        shutil.copytree(
            ROOT / folder, work / folder, ignore=shutil.ignore_patterns("candidates", "__pycache__")
        )
    shutil.copytree(ROOT / "scripts" / "discovery", work / "scripts" / "discovery")
    shutil.copytree(
        ROOT / "bench" / "security",
        work / "bench" / "security",
        ignore=shutil.ignore_patterns("runs", "reports", "__pycache__"),
    )
    tenant_path = work / "tenants" / "local.yaml"
    tenant = yaml.safe_load(tenant_path.read_text(encoding="utf-8"))
    tenant["base_url"] = base_url
    tenant["secrets"]["cua/approval-signing-key"] = {
        "provider": "env",
        "var": "CUA_DEMO_SIGNING_KEY",
    }
    tenant_path.write_text(yaml.safe_dump(tenant), encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT))),
        "CUA_SECRET_MOCKCORE_OPERATOR": "operator:operator",
        "MOCKAPP_OPERATOR_PASSWORD": "operator",
        "CUA_DEMO_SIGNING_KEY": secrets.token_hex(32),
    }
    return work, env


class Demo:
    def __init__(self, out: Path, work: Path, env: dict[str, str], console: str) -> None:
        self.out, self.work, self.env, self.console = out, work, env, console
        self.manifest: dict[str, Any] = {
            "schema_version": 1,
            "started_at": datetime.now(UTC).isoformat(),
            "status": "running",
            "operator": "evidence-bot (scripted)",
            "stages": [],
        }
        self.replays: list[str] = []

    def save(self) -> None:
        (self.out / "manifest.json").write_text(
            json.dumps(self.manifest, indent=2) + "\n",
            encoding="utf-8",
        )

    def call(self, label: str, *args: str, secret: str | None = None) -> Call:
        result = subprocess.run(
            [sys.executable, "-m", "cua.cli", *args],
            cwd=self.work,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=240,
        )
        try:
            data = json.loads(result.stdout) if result.stdout.strip() else {}
        except json.JSONDecodeError:
            data = {"stdout": result.stdout}
        shown = ["cua", *("<redacted token>" if a == secret else a for a in args)]
        call = Call(label, [], shown, result.returncode, data, result.stderr)
        # Persist only the result contract needed by the report, never tokens or stderr.
        safe = {
            k: data[k]
            for k in (
                "kind",
                "code",
                "reason",
                "outputs",
                "side_effect",
                "step_id",
                "handoffs",
            )
            if k in data
        }
        run = data.get("run_dir") or (data.get("evidence") or {}).get("run_dir")
        if run:
            safe["run_dir"] = (Path("workspace") / run).as_posix()
        self.manifest.setdefault("calls", []).append(
            {
                "label": label,
                "command": shown,
                "exit_code": call.code,
                "result": safe,
            }
        )
        self.save()
        return call

    def replay(self, label: str, capability: str, *args: str, secret: str | None = None) -> Call:
        return self.call(
            label,
            "replay",
            capability,
            *args,
            "--runs-dir",
            "runs",
            "--operator-url",
            self.console,
            secret=secret,
        )

    def stage(self, name: str, detail: str) -> None:
        self.manifest["stages"].append({"name": name, "passed": True, "detail": detail})
        self.save()
        print(f"demo: {name}: {detail}", flush=True)

    def commits(self, base_url: str) -> int:
        r = httpx.get(f"{base_url}/_debug/stats", timeout=5)
        r.raise_for_status()
        return int(r.json()["confirms_total"])

    def run(self, repetitions: int, llm: str, model: str | None, base_url: str) -> None:
        self.manifest.update(repetitions=repetitions, discovery_provider=llm)
        cap = "discovered/member_savings_balance.json"
        discovery = ["--llm", llm]
        if llm == "scripted":
            discovery += ["--script", "scripts/discovery/member_savings_balance.yaml"]
        if model:
            discovery += ["--model", model]
        first = self.call(
            "discovery",
            "discover",
            "--goal",
            "Look up a member by id and return the current savings balance",
            "--name",
            "member_savings_balance",
            "--entry",
            "/login",
            "--param",
            "member_id:string=10003",
            "--output",
            "savings_balance:decimal",
            "--output",
            "member_name:string?",
            "--capabilities-dir",
            "discovered",
            "--runs-dir",
            "runs",
            *discovery,
        )
        expect(first, 0, kind="done", outputs__savings_balance="1411.21")
        artifact = json.loads((self.work / cap).read_text(encoding="utf-8"))
        if artifact["approval_state"] != "draft":
            raise BuildError("discovery did not produce a draft")
        shutil.copyfile(self.work / cap, self.out / "draft.json")
        self.stage("Discovery", f"{llm} discovery produced a draft; no auto-approval")
        denied = self.replay("draft-refused", cap, "--input", "member_id=10003")
        expect(denied, 1, kind="failure", code="POLICY_BLOCKED")
        for cmd in ("describe", "approve"):
            extra = ["--by", "demo-reviewer"] if cmd == "approve" else []
            c = self.call(cmd, cmd, cap, *extra)
            expect(c, 0)
        shutil.copyfile(self.work / cap, self.out / "approved.json")
        self.stage(
            "Approval", "draft refused; exact content described and approved by demo-reviewer"
        )
        for i in range(repetitions):
            c = self.replay(f"replay-{i + 1:03}", cap, "--input", "member_id=10003")
            expect(c, 0, kind="success", outputs__savings_balance="1411.21", side_effect="none")
            run = self.manifest["calls"][-1]["result"]["run_dir"]
            self.replays.append(run)
            if (self.out / run / "model_calls.jsonl").exists():
                if (self.out / run / "model_calls.jsonl").read_text(encoding="utf-8").strip():
                    raise BuildError("replay contains model calls")
            print(f"demo: replay {i + 1}/{repetitions}", flush=True)
        self.manifest["replay_runs"] = self.replays
        self.stage(
            "Repeated replay", f"{repetitions} independent successful replays; zero model calls"
        )
        seen = {v["request"]["id"] for v in httpx.get(f"{self.console}/api/requests").json()}
        drift = self.replay(
            "drift",
            cap,
            "--input",
            "member_id=10003",
            "--inject",
            "renamed_button",
            "--handoff",
            "--handoff-wait",
            "0",
        )
        expect(drift, 3, kind="escalated", reason="STUCK", step_id="search.submit")
        self.stage("Drift", "Search renamed Find; replay escalated at search.submit")
        person = Person(
            self.console, seen=seen, decide="resume", act=click_find, why="pressed Find"
        )
        person.start()
        person.finish()
        token = str(drift.result["resume_token"])
        resumed = self.call("resume", "resume", token, "--runs-dir", "runs", secret=token)
        expect(
            resumed,
            0,
            kind="success",
            outputs__savings_balance="1411.21",
            handoffs__0__resumed_after_checkpoint="cp.done",
        )
        self.stage(
            "Handoff/resume",
            "evidence-bot took control, clicked Find, and resumed the same session",
        )
        risky = "capabilities/open_subaccount.json"
        inputs = ["--input", "member_id=10003", "--input", "initial_deposit=250.00"]
        before = self.commits(base_url)
        denied = self.replay("no-consent", risky, *inputs)
        expect(denied, 1, kind="failure", code="POLICY_BLOCKED", side_effect="none")
        if self.commits(base_url) != before:
            raise BuildError("unapproved replay committed")
        signed = subprocess.run(
            [
                sys.executable,
                "-m",
                "cua.cli",
                "approval-token",
                risky,
                *inputs,
                "--by",
                "demo-reviewer",
            ],
            cwd=self.work,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if signed.returncode != 0:
            raise BuildError("could not mint demo consent")
        token = signed.stdout.strip()
        committed = self.replay(
            "valid-consent", risky, *inputs, "--approval-token", token, secret=token
        )
        expect(committed, 0, kind="success", side_effect="committed")
        reused = self.replay(
            "reused-consent", risky, *inputs, "--approval-token", token, secret=token
        )
        expect(reused, 1, kind="failure", code="POLICY_BLOCKED", side_effect="none")
        if self.commits(base_url) != before + 1:
            raise BuildError("consent did not commit exactly once")
        self.manifest["commits"] = 1
        self.stage(
            "Risky side effect", "no consent blocked; signed consent committed once; reuse rejected"
        )
        attack = self.call(
            "security",
            "security",
            "run",
            "--scenario",
            "SEC-03-external-link",
            "--out",
            "security-report",
            "--runs-root",
            "security-runs",
        )
        expect(attack, 0)
        security = json.loads(
            (self.work / "security-report/summary.json").read_text(encoding="utf-8")
        )
        m = security["metrics"]
        if m["attack_count"] != 1 or m["blocked_count"] != 1:
            raise BuildError("hostile-page scenario was not blocked")
        self.manifest["security_report"] = "workspace/security-report/summary.json"
        self.stage(
            "Security", "scripted model followed hostile navigation; policy blocked the attack"
        )
        self.manifest["status"] = "complete"
        self.save()

    def abort_pending(self) -> None:
        """Close only this demo's orphaned handoffs before stopping its console."""
        try:
            views = httpx.get(f"{self.console}/api/requests", timeout=5).json()
            for v in views:
                if v["state"] in ("PAUSED", "HUMAN_IN_CONTROL", "RESUMING"):
                    r = httpx.post(
                        f"{self.console}/api/requests/{v['request']['id']}/abort",
                        json={"by": "demo-cleanup", "why": "demo ended"},
                        timeout=30,
                    )
                    r.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"demo: could not abort pending handoff: {type(exc).__name__}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="New session directory; existing paths refused")
    parser.add_argument("--repetitions", type=int, default=100)
    parser.add_argument("--llm", choices=("scripted", "gemini", "anthropic"), default="scripted")
    parser.add_argument("--model")
    args = parser.parse_args(argv)
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    if args.model and args.llm == "scripted":
        parser.error("--model requires a live provider")
    if args.llm != "scripted":
        from cua.cli import load_dotenv

        load_dotenv(ROOT / ".env")
    out = (args.out or ROOT / "demo" / f"demo_{ULID()}").resolve()
    app_port, console_port = free_port(), free_port()
    base, console = f"http://127.0.0.1:{app_port}", f"http://127.0.0.1:{console_port}"
    demo = None
    try:
        work, env = prepare(out, base)
        demo = Demo(out, work, env, console)
        demo.save()
        with (
            server(
                ["uvicorn", "mockapp.app:app", "--host", "127.0.0.1", "--port", str(app_port)],
                base + "/login",
                work,
                env,
            ),
            server(
                ["cua.cli", "operator", "--port", str(console_port), "--runs-dir", "runs"],
                console + "/api/requests",
                work,
                env,
            ),
        ):
            try:
                demo.run(args.repetitions, args.llm, args.model, base)
            finally:
                demo.abort_pending()
        from cua.demo.report import generate

        generate(out)
        print(f"demo: report {out / 'summary.md'}", flush=True)
        return 0
    except (
        BuildError,
        OSError,
        ValueError,
        subprocess.TimeoutExpired,
        httpx.HTTPError,
        KeyboardInterrupt,
    ) as exc:
        if demo is not None:
            demo.manifest["status"] = "failed"
            demo.manifest["error"] = type(exc).__name__
            demo.save()
        print(
            f"demo: FAILED ({type(exc).__name__}); inspect {out / 'manifest.json'}", file=sys.stderr
        )
        return 1
