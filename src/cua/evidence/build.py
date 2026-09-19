"""Regenerate the replay half of ``evidence/``: ``python -m cua.evidence.build``.

Everything here goes through the same doors a caller would: a fresh mock app
on the tenant's port, the real operator console (``cua operator``) as a
server, and every replay, resume and approval token as a ``cua`` subprocess.
What each call printed is kept next to the run it made, as ``cli-*.json``,
with the command lines in ``commands.txt``.

The person at the console is a script, and the evidence says so: a thread
that watches the console's API for a request, takes control over the same
API a person's browser posts to, works the live page over the request's CDP
endpoint with a Playwright connection of its own, and decides. It signs
itself ``evidence-bot``.

Nothing is replaced unless every scenario came out as expected: runs are made
under ``evidence/runs/_build/`` (gitignored), checked, and only then copied
over ``evidence/<scenario>/``. A redaction scan of the whole package runs
last and fails the build on a hit.

The two discovery runs are not regenerated: they were real model runs. They
were brought in once with ``--adopt RUN_DIR FOLDER``, which copies a run as
it is.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import Page, sync_playwright

from cua.escalation.capture import find_page

EVIDENCE = Path("evidence")
STAGING = EVIDENCE / "runs" / "_build"
TENANT = Path("tenants/local.yaml")
GOAL1 = Path("capabilities/member_savings_balance.json")
GOAL2 = Path("capabilities/open_subaccount.json")
GOAL2_INPUTS = ("member_id=10003", "initial_deposit=250.00")
PERSON = "evidence-bot"

MOCK_CREDENTIAL = "operator:operator"
"""The mock app's published login (``.env.example``); used only if unset."""

LEFT_BEHIND = frozenset({"session.json", "waiter.json"})
"""Live-session plumbing: a pid, a port, a temp profile path. Not evidence."""

SCREENSHOT_MAX = 200_000
TRACE_MAX = 5_000_000
STARTUP_S = 30.0
REQUEST_WAIT_S = 90.0
CALL_TIMEOUT_S = 240.0


class BuildError(Exception):
    pass


# --------------------------------------------------------------------------
# Servers
# --------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


@contextmanager
def _server(argv: list[str], ready_url: str, env: dict[str, str]) -> Iterator[None]:
    proc = subprocess.Popen(
        argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=Path.cwd()
    )
    try:
        deadline = time.monotonic() + STARTUP_S
        while True:
            if proc.poll() is not None:
                raise BuildError(f"{' '.join(argv[2:4])} exited during startup")
            try:
                if httpx.get(ready_url, timeout=1.0).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                raise BuildError(f"{ready_url} did not come up within {STARTUP_S:.0f} s")
            time.sleep(0.1)
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _tenant_base_url() -> str:
    for line in TENANT.read_text(encoding="utf-8").splitlines():
        if line.startswith("base_url:"):
            return line.partition(":")[2].strip()
    raise BuildError(f"{TENANT} has no base_url")


# --------------------------------------------------------------------------
# The person at the console
# --------------------------------------------------------------------------


def _main_frame(page: Page) -> Any:
    frame = page.frame(name="main")
    if frame is None:
        raise BuildError("the run's page has no main frame")
    return frame


def click_find(page: Page) -> None:
    """The Search button was renamed Find; press it and wait for the member."""
    main = _main_frame(page)
    main.get_by_role("button", name="Find").click()
    main.wait_for_url("**/member/**")


def dismiss_notice(page: Page) -> None:
    """Close the notice the capability does not know about."""
    _main_frame(page).get_by_role("button", name="OK").click()


class Person(threading.Thread):
    """Waits for a new request on the console, maybe takes control and acts in
    the browser, then decides. Errors are kept for ``finish``."""

    def __init__(
        self,
        console: str,
        *,
        seen: set[str],
        decide: str,
        act: Callable[[Page], None] | None = None,
        take_control: bool = True,
        why: str = "",
    ) -> None:
        super().__init__(daemon=True)
        self.console = console
        self.seen = set(seen)
        self.decide = decide
        self.act = act
        self.take_control = take_control
        self.why = why
        self.error: BaseException | None = None
        self.request: dict[str, Any] = {}

    def run(self) -> None:
        try:
            self._run()
        except BaseException as exc:
            self.error = exc

    def _run(self) -> None:
        with httpx.Client(base_url=self.console, timeout=30) as http:
            deadline = time.monotonic() + REQUEST_WAIT_S
            while True:
                fresh = [
                    v
                    for v in http.get("/api/requests").json()
                    if v["state"] == "PAUSED" and v["request"]["id"] not in self.seen
                ]
                if fresh:
                    break
                if time.monotonic() > deadline:
                    raise BuildError("no intervention request was opened")
                time.sleep(0.2)
            self.request = fresh[0]["request"]
            rid = self.request["id"]
            if self.take_control:
                _ok(http.post(f"/api/requests/{rid}/take_control", json={"by": PERSON}))
            if self.act is not None:
                with sync_playwright() as pw:
                    browser = pw.chromium.connect_over_cdp(self.request["cdp_url"])
                    try:
                        page = find_page(browser.contexts, self.request["target_id"])
                        if page is None:
                            raise BuildError("the run's page is not in its browser")
                        self.act(page)
                        page.wait_for_timeout(500)  # let the console's capture see it
                    finally:
                        browser.close()
            body = {"by": PERSON, "why": self.why}
            _ok(http.post(f"/api/requests/{rid}/{self.decide}", json=body))

    def finish(self) -> None:
        self.join(timeout=REQUEST_WAIT_S)
        if self.is_alive():
            raise BuildError("the scripted person never finished")
        if self.error is not None:
            raise BuildError(f"the scripted person failed: {self.error}") from self.error


def _ok(response: httpx.Response) -> None:
    if response.status_code != 200:
        raise BuildError(f"console refused {response.request.url.path}: {response.text}")


# --------------------------------------------------------------------------
# Calls and scenarios
# --------------------------------------------------------------------------


@dataclass
class Call:
    label: str
    argv: list[str]
    shown: list[str]
    code: int
    result: dict[str, Any]
    stderr: str


@dataclass
class Build:
    env: dict[str, str]
    console: str
    runs: Path
    calls: list[Call] = field(default_factory=list)

    def cua(self, label: str, *args: str, secret: str | None = None) -> Call:
        argv = [sys.executable, "-m", "cua.cli", *args]
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.env,
            cwd=Path.cwd(),
            timeout=CALL_TIMEOUT_S,
        )
        shown = ["cua", *("<approval token>" if a == secret else a for a in args)]
        try:
            result: dict[str, Any] = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except json.JSONDecodeError:
            result = {"stdout": proc.stdout}
        call = Call(label, argv, shown, proc.returncode, result, proc.stderr)
        self.calls.append(call)
        return call

    def replay(self, label: str, capability: Path, *args: str, secret: str | None = None) -> Call:
        console = ["--operator-url", self.console] if "--handoff" in args else []
        return self.cua(
            label,
            "replay",
            capability.as_posix(),
            *args,
            "--runs-dir",
            self.runs.as_posix(),
            *console,
            secret=secret,
        )

    def token(self, capability: Path, inputs: Sequence[str]) -> str:
        args = [a for i in inputs for a in ("--input", i)]
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "cua.cli",
                "approval-token",
                capability.as_posix(),
                *args,
                "--by",
                PERSON,
            ],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=Path.cwd(),
            timeout=60,
        )
        if proc.returncode != 0:
            raise BuildError(f"cua approval-token failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def seen(self) -> set[str]:
        return {v["request"]["id"] for v in httpx.get(f"{self.console}/api/requests").json()}


def _inputs(*pairs: str) -> list[str]:
    return [a for p in pairs for a in ("--input", p)]


def expect(call: Call, exit_code: int, /, **fields: Any) -> None:
    """The call exited ``exit_code`` and its result has ``fields`` (dotted paths)."""
    problems = []
    if call.code != exit_code:
        problems.append(f"exit {call.code}, expected {exit_code}")
    for dotted, want in fields.items():
        got: Any = call.result
        for part in dotted.split("__"):
            got = got[int(part)] if isinstance(got, list) else (got or {}).get(part)
        if callable(want) and not want(got):
            problems.append(f"{dotted.replace('__', '.')} = {got!r}")
        elif not callable(want) and got != want:
            problems.append(f"{dotted.replace('__', '.')} = {got!r}, expected {want!r}")
    if problems:
        tail = call.stderr.strip().splitlines()[-3:]
        raise BuildError(f"{call.label}: {'; '.join(problems)}\n  " + "\n  ".join(tail))


def _run_dir(call: Call) -> Path:
    run_dir = (call.result.get("evidence") or {}).get("run_dir")
    if not run_dir:
        raise BuildError(f"{call.label}: the result names no run directory")
    return Path(run_dir)


Scenario = Callable[[Build], tuple[Path, list[Call]]]


def replay_success(b: Build) -> tuple[Path, list[Call]]:
    c = b.replay("replay", GOAL1, *_inputs("member_id=10003"))
    expect(c, 0, kind="success", outputs__savings_balance="1411.21", side_effect="none")
    return _run_dir(c), [c]


def replay_not_found(b: Build) -> tuple[Path, list[Call]]:
    c = b.replay("replay", GOAL1, *_inputs("member_id=99999"), "--inject", "not_found")
    expect(c, 2, kind="business_outcome", code="NOT_FOUND", step_id="search.submit")
    return _run_dir(c), [c]


def replay_server_error(b: Build) -> tuple[Path, list[Call]]:
    c = b.replay("replay", GOAL1, *_inputs("member_id=10003"), "--inject", "server_error")
    expect(c, 1, kind="failure", code="APP_ERROR", evidence__trace="trace.zip")
    return _run_dir(c), [c]


def replay_slow_confirm(b: Build) -> tuple[Path, list[Call]]:
    token = b.token(GOAL2, GOAL2_INPUTS)
    c = b.replay(
        "replay",
        GOAL2,
        *_inputs(*GOAL2_INPUTS),
        "--approval-token",
        token,
        "--inject",
        "slow_confirm",
        secret=token,
    )
    expect(c, 1, kind="failure", code="TIMEOUT", side_effect="unknown")
    return _run_dir(c), [c]


def replay_needs_approval(b: Build) -> tuple[Path, list[Call]]:
    """No token: the commit waits for a person, who approves on the console."""
    person = Person(b.console, seen=b.seen(), decide="approve", take_control=False)
    person.start()
    c = b.replay("replay", GOAL2, *_inputs(*GOAL2_INPUTS), "--handoff", "--handoff-wait", "120")
    person.finish()
    expect(
        c,
        0,
        kind="success",
        side_effect="committed",
        handoffs__0__reason="NEEDS_APPROVAL",
        handoffs__0__decision="approve",
        handoffs__0__decided_by=PERSON,
    )
    return _run_dir(c), [c]


def replay_escalation(b: Build) -> tuple[Path, list[Call]]:
    """A notice nobody recorded blocks the member page; a person takes the
    live session, closes it, and hands back. The run checks the screen and
    carries on from the checkpoint that holds."""
    token = b.token(GOAL2, GOAL2_INPUTS)
    person = Person(
        b.console, seen=b.seen(), decide="resume", act=dismiss_notice, why="closed the notice"
    )
    person.start()
    c = b.replay(
        "replay",
        GOAL2,
        *_inputs(*GOAL2_INPUTS),
        "--approval-token",
        token,
        "--inject",
        "modal_dialog",
        "--handoff",
        "--handoff-wait",
        "120",
        secret=token,
    )
    person.finish()
    expect(
        c,
        0,
        kind="success",
        side_effect="committed",
        handoffs__0__decision="hand_back",
        handoffs__0__resumed_after_checkpoint="cp.member_detail",
        handoffs__0__human_actions_count=lambda n: isinstance(n, int) and n >= 1,
    )
    return _run_dir(c), [c]


def replay_resume_after_human(b: Build) -> tuple[Path, list[Call]]:
    """The run returns ``escalated`` at once and its process exits; a person
    finishes the lookup in the still-open browser; ``cua resume`` then finds
    the screen already at ``cp.done`` and does nothing again."""
    seen = b.seen()
    first = b.replay(
        "replay",
        GOAL1,
        *_inputs("member_id=10003"),
        "--inject",
        "renamed_button",
        "--handoff",
        "--handoff-wait",
        "0",
    )
    expect(first, 3, kind="escalated", reason="STUCK", step_id="search.submit")
    person = Person(b.console, seen=seen, decide="resume", act=click_find, why="pressed Find")
    person.start()
    person.finish()
    token = str(first.result["resume_token"])
    second = b.cua("resume", "resume", token, "--runs-dir", b.runs.as_posix())
    expect(
        second,
        0,
        kind="success",
        outputs__savings_balance="1411.21",
        handoffs__0__resumed_after_checkpoint="cp.done",
    )
    return _run_dir(first), [first, second]


SCENARIOS: dict[str, Scenario] = {
    "replay-success": replay_success,
    "replay-not-found": replay_not_found,
    "replay-server-error": replay_server_error,
    "replay-slow-confirm": replay_slow_confirm,
    "replay-needs-approval": replay_needs_approval,
    "replay-escalation": replay_escalation,
    "replay-resume-after-human": replay_resume_after_human,
}

CAPABILITIES = (GOAL1, GOAL2)


# --------------------------------------------------------------------------
# Packaging
# --------------------------------------------------------------------------


def copy_run(run_dir: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(run_dir, dest, ignore=lambda _d, names: [n for n in names if n in LEFT_BEHIND])
    # What `cua resume` needed, kept for its inputs-as-hashes and saved state;
    # the browser's temp profile path in it is plumbing, and names a home dir.
    state = dest / "handoff_state.json"
    if state.is_file():
        data = json.loads(state.read_text(encoding="utf-8"))
        if isinstance(data.get("session"), dict) and data["session"].get("profile"):
            data["session"]["profile"] = "<temp profile>"
            state.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def write_calls(dest: Path, calls: list[Call]) -> None:
    lines = []
    for call in calls:
        name = f"cli-{call.label}.json"
        (dest / name).write_text(json.dumps(call.result, indent=2) + "\n", encoding="utf-8")
        lines.append(f"{' '.join(call.shown)}\n  -> exit {call.code}, stdout in {name}")
    (dest / "commands.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build(names: Sequence[str]) -> int:
    from cua.cli import load_dotenv

    load_dotenv(Path(".env"))
    env = {**os.environ, "PYTHONUTF8": "1"}
    env.setdefault("CUA_SECRET_MOCKCORE_OPERATOR", MOCK_CREDENTIAL)

    base = urlsplit(_tenant_base_url())
    host, port = base.hostname or "127.0.0.1", base.port or 80
    if _port_open(host, port):
        raise BuildError(
            f"something is already listening on {host}:{port}. The evidence needs a fresh mock "
            "app there (reference numbers, one-shot injects): stop it and run again."
        )
    if STAGING.exists():
        shutil.rmtree(STAGING)
    STAGING.mkdir(parents=True)
    out = STAGING / "out"

    console_port = _free_port()
    console = f"http://127.0.0.1:{console_port}"
    py = [sys.executable, "-m"]
    mock = [*py, "uvicorn", "mockapp.app:app", "--host", host, "--port", str(port)]
    operator = [*py, "cua.cli", "operator", "--port", str(console_port)]
    operator += ["--runs-dir", STAGING.as_posix()]
    b = Build(env=env, console=console, runs=STAGING)
    with (
        _server(mock, f"{base.scheme}://{host}:{port}/login", env),
        _server(operator, f"{console}/api/requests", env),
    ):
        for name in names:
            print(f"evidence: {name} ...", file=sys.stderr, flush=True)
            run_dir, calls = SCENARIOS[name](b)
            copy_run(run_dir, out / name)
            write_calls(out / name, calls)

    for name in names:
        copy_run(out / name, EVIDENCE / name)
    for cap in CAPABILITIES:
        shutil.copyfile(cap, EVIDENCE / f"capability-{cap.name}")
    print(f"evidence: wrote {', '.join(names)} and the capabilities", file=sys.stderr)
    return 0


def adopt(run_dir: Path, folder: str) -> int:
    """Bring a finished (discovery) run into the package as it is."""
    if not (run_dir / "run.json").is_file():
        raise BuildError(f"{run_dir.as_posix()} is not a run directory")
    copy_run(run_dir, EVIDENCE / folder)
    print(f"evidence: {run_dir.as_posix()} -> {(EVIDENCE / folder).as_posix()}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------
# The redaction scan
# --------------------------------------------------------------------------

_EVERYWHERE = [
    (re.compile(rb"F_PWD=operator"), "the mock password in a form body"),
    (re.compile(rb'"operator"'), "the mock credential as a JSON string"),
    (re.compile(rb"sk-ant-[A-Za-z0-9_-]{8,}"), "an Anthropic key"),
    (re.compile(rb"AIza[0-9A-Za-z_-]{30,}"), "a Google API key"),
    (re.compile(rb"cat1\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"), "an approval token"),
    (re.compile(rb"[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2}"), "a local home directory"),
]
_TEXT_ONLY = [
    (re.compile(rb"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"), "an SSN-shaped number"),
    (re.compile(rb"(?<![\d.])\d{12,16}(?![\d.])"), "a 12-16 digit account-shaped number"),
]


def _key_values(env: dict[str, str]) -> list[bytes]:
    names = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
    return [env[n].encode() for n in names if len(env.get(n, "")) >= 8]


def scan(root: Path = EVIDENCE) -> list[str]:
    """Every hit of a secret or PII shape under ``root``, outside the local
    ``runs/`` scratch area. Screenshots are bytes and are checked by size here
    and by eye; everything else, including inside trace zips, by pattern."""
    from cua.cli import load_dotenv

    load_dotenv(Path(".env"))
    keys = _key_values(dict(os.environ))
    hits: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or (root / "runs") in path.parents:
            continue
        rel = path.as_posix()
        if path.suffix == ".png":
            if path.stat().st_size > SCREENSHOT_MAX:
                hits.append(f"{rel}: screenshot over {SCREENSHOT_MAX // 1000} KB")
            continue
        if path.suffix == ".mp4":
            continue
        blobs: list[tuple[str, bytes, bool]]
        if path.suffix == ".zip":
            if path.stat().st_size > TRACE_MAX:
                hits.append(f"{rel}: trace over {TRACE_MAX // 1_000_000} MB")
            with zipfile.ZipFile(path) as z:
                blobs = [(f"{rel}!{n}", z.read(n), False) for n in z.namelist()]
        else:
            blobs = [(rel, path.read_bytes(), True)]
        for where, data, text in blobs:
            rules = _EVERYWHERE + (_TEXT_ONLY if text else [])
            hits += [f"{where}: {why}" for rx, why in rules if rx.search(data)]
            hits += [f"{where}: a model API key from the environment" for k in keys if k in data]
    return hits


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cua.evidence.build", description=__doc__)
    parser.add_argument(
        "--only", action="append", choices=sorted(SCENARIOS), help="Just these scenarios"
    )
    parser.add_argument("--scan", action="store_true", help="Only run the redaction scan")
    parser.add_argument(
        "--adopt", nargs=2, metavar=("RUN_DIR", "FOLDER"), help="Copy a run in as it is"
    )
    args = parser.parse_args(argv)
    try:
        if args.adopt:
            adopt(Path(args.adopt[0]), args.adopt[1])
        elif not args.scan:
            build(args.only or list(SCENARIOS))
    except (BuildError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"evidence: FAILED: {exc}", file=sys.stderr)
        return 1
    hits = scan()
    for hit in hits:
        print(f"evidence: redaction scan: {hit}", file=sys.stderr)
    if hits:
        return 1
    print("evidence: redaction scan clean", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
