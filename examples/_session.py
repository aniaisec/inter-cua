from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import httpx
import yaml

from cua.artifact.store import load
from cua.benchmark.environment import free_port
from cua.demo.runner import server
from cua.project import dotenv
from cua.surface.playwright_surface import BrowserProcess
from examples._support import ExampleError, check_subset, process_options, require, stop_process

EXAMPLES = Path(__file__).resolve().parent


class Session:
    def __init__(self, spec: dict[str, Any], out: Path) -> None:
        self.spec, self.out = spec, out.resolve()
        self.out.mkdir(parents=True, exist_ok=False)
        self.work = self.out / "project with spaces"
        self.cwd = self.out / "unrelated cwd"
        self.cwd.mkdir()
        self.env = {**os.environ, "PYTHONUTF8": "1"}
        self.commands: list[dict[str, Any]] = []
        self.recording: list[list[Any]] = []
        self.started = time.monotonic()
        self.base_url = ""

    def record(self, command: list[str], code: int, result: dict[str, Any]) -> None:
        # No stderr, signing keys, approval tokens or resume tokens in the transcript.
        safe = {
            key: result[key]
            for key in (
                "kind",
                "code",
                "reason",
                "outputs",
                "side_effect",
                "cached",
                "run_id",
                "evidence",
                "steps",
                "handoffs",
                "stdout",
                "passed",
                "gates",
                "name",
                "version",
                "approval_state",
            )
            if key in result
        }
        self.commands.append({"command": command, "exit_code": code, "result": safe})
        elapsed = round(time.monotonic() - self.started, 3)
        self.recording.append([elapsed, "o", "$ " + " ".join(command) + "\r\n"])
        self.recording.append([elapsed, "o", json.dumps(safe, indent=2) + "\r\n"])
        (self.out / "commands.json").write_text(json.dumps(self.commands, indent=2) + "\n")
        header = {"version": 2, "width": 100, "height": 30, "title": self.spec["name"]}
        (self.out / "demonstration.cast").write_text(
            "\n".join(json.dumps(row) for row in [header, *self.recording]) + "\n"
        )

    def execute(self, args: tuple[str, ...], timeout_s: float) -> tuple[int, str, str]:
        argv = [sys.executable, "-m", "cua.cli", "--root", str(self.work), *args]
        with subprocess.Popen(
            argv,
            cwd=self.cwd,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            **process_options(),
        ) as proc:
            try:
                stdout, stderr = proc.communicate(timeout=timeout_s)
            except BaseException:
                stop_process(proc)
                raise
        return proc.returncode, stdout, stderr

    def cli(self, *args: str, expected: int = 0, secret: str | None = None) -> dict[str, Any]:
        code, stdout, stderr = self.execute(args, 240)
        require(code == expected, f"{args[0]}: exit {code}; {stderr}")
        try:
            result: dict[str, Any] = json.loads(stdout)
        except json.JSONDecodeError:
            result = {"stdout": stdout}
        shown = [
            "cua",
            "--root",
            str(self.work),
            *("<private token>" if arg == secret else arg for arg in args),
        ]
        self.record(shown, code, result)
        return result

    def inputs(self, inputs: dict[str, Any]) -> list[str]:
        return [arg for name, value in inputs.items() for arg in ("--input", f"{name}={value}")]

    def token(self, capability: str, inputs: dict[str, Any]) -> str:
        code, stdout, _ = self.execute(
            (
                "approval-token",
                capability,
                *self.inputs(inputs),
                "--by",
                "example-reviewer (scripted)",
            ),
            30,
        )
        require(code == 0, "could not mint consent for the synthetic example")
        return stdout.strip()

    def oracle(self) -> dict[str, Any]:
        response = httpx.get(self.base_url + "/_test/state", timeout=5)
        response.raise_for_status()
        return dict(response.json())

    def replay(self, capability: str, inputs: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.cli("replay", capability, *self.inputs(inputs), **kwargs)

    def bootstrap(self, stack: ExitStack) -> None:
        template = self.spec["template"]
        self.cli("init", str(self.work), "--template", template)
        # Synthetic targets must not consume the caller's application credentials.
        self.env.update(dotenv(self.work / ".env.example"))
        if template == "demo":
            self.env["MOCKAPP_OPERATOR_PASSWORD"] = "operator"
        if template == "windows":
            self.env["DESKCALC_LEDGER"] = str(self.out / "ledger.txt")
        else:
            port = free_port()
            self.base_url = f"http://127.0.0.1:{port}"
            app = "mockapp.app:app" if template == "demo" else "examples.inventory.app.app:app"
            stack.enter_context(
                server(
                    [
                        "uvicorn",
                        app,
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--log-level",
                        "warning",
                    ],
                    self.base_url + "/login",
                    EXAMPLES.parent,
                    self.env,
                )
            )
            tenant_path = self.work / "tenants/local.yaml"
            tenant = yaml.safe_load(tenant_path.read_text())
            tenant["base_url"] = self.base_url
            tenant_path.write_text(yaml.safe_dump(tenant))
            if template == "inventory":
                (self.work / ".env").write_bytes((self.work / ".env.example").read_bytes())
        if template == "inventory":
            definitions = json.loads((self.work / "scenario.json").read_text())
            if self.spec["name"] == "workflow":
                # Extend the editable example configuration to expose the code as
                # a typed output for the next capability's input binding.
                definitions["item_lookup"]["outputs"].append("item_code:string")
                script_path = self.work / "scripts/discovery/item_lookup.yaml"
                script = yaml.safe_load(script_path.read_text())
                script["steps"][-1]["outputs"]["item_code"] = [
                    {
                        "strategy": "table_cell",
                        "row_contains": "Item code",
                        "column_header": "Value",
                    }
                ]
                script_path.write_text(yaml.safe_dump(script))
        else:
            definitions = self.spec.get("discovery", {})
        for name in self.spec["capabilities"]:
            if name in definitions:
                definition = definitions[name]
                args = [
                    "discover",
                    "--name",
                    name,
                    "--entry",
                    definition.get("entry", "/login"),
                    "--goal",
                    definition["goal"],
                    "--llm",
                    "scripted",
                    "--script",
                    str(self.work / "scripts/discovery" / f"{name}.yaml"),
                ]
                for param in definition["params"]:
                    args.extend(["--param", param])
                for output in definition["outputs"]:
                    args.extend(["--output", output])
                if name == "adjust_stock":
                    # Discovery changes only this session's synthetic stock; the scenario
                    # oracle baseline is captured after discovery, before approved replay.
                    args.append("--auto-approve-risky")
                self.cli(*args)
            path = self.work / "capabilities" / f"{name}.json"
            draft = load(path)
            require(draft.approval_state == "draft", "examples must start unapproved")
            self.cli("describe", name)
            self.cli("approve", name, "--by", "example-reviewer (scripted)")
            require(load(path).content_hash() == draft.content_hash(), "approval changed content")
        if self.spec["name"] in ("http_client", "mcp_setup"):
            access = self.work / "api/access.yaml"
            access.parent.mkdir(exist_ok=True)
            access.write_text(
                yaml.safe_dump(
                    {
                        "tenants": {"local": {}},
                        "clients": {
                            "local-agent": {
                                "key": {
                                    "provider": "file",
                                    "path": ".cua/api-keys/local-agent.key",
                                },
                                "tenants": ["local"],
                                "capabilities": self.spec["capabilities"],
                                "scopes": ["read", "invoke", "approve", "operate"],
                            }
                        },
                    }
                )
            )

    def no_models(self, result: dict[str, Any]) -> None:
        run_dir = (result.get("evidence") or {}).get("run_dir")
        if not isinstance(run_dir, str) or not Path(run_dir).is_dir():
            raise ExampleError("missing replay evidence")
        require(not (Path(run_dir) / "model_calls.jsonl").exists(), "replay made model calls")

    def cleanup_browsers(self) -> None:
        # A failed assertion after escalation still owns and must close its live browser.
        for path in self.work.rglob("session.json"):
            session = json.loads(path.read_text())
            browser = BrowserProcess(session["pid"], session["cdp_url"], Path(session["profile"]))
            if browser.alive():
                browser.kill()


def cli_cases(s: Session) -> dict[str, Any]:
    before = s.oracle()
    answers = []
    for case in s.spec["cases"]:
        result = s.cli(*case["args"], expected=case["exit_code"])
        check_subset(result, case["expected"])
        s.no_models(result)
        answers.append({key: result[key] for key in case["expected"]})
    require(s.oracle() == before, "read/business rejection changed target state")
    return {"answers": answers, "commits": 0, "replay_model_calls": 0}
