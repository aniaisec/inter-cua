"""The operator console: open intervention requests, and what a person can do.

``cua operator`` serves it (default http://127.0.0.1:8100). It reads the queue
under ``<runs>/.interventions/`` and each run's ``control.json``; it never
talks to a run process directly. Per request:

* **Take control** — PAUSED → HUMAN_IN_CONTROL. The console attaches to the
  live browser over CDP and records what the person does
  (``human_actions.jsonl``). Locally the person works in the headed window;
  for a headless session the request links the page in the browser's own
  DevTools front end, which shows it and takes clicks and keys.
* **Resume** — hand back; optionally name where to carry on (checked by the
  run against that checkpoint, never taken on trust).
* **Retry step** — hand back and perform the stopped step again (not offered
  for a step that may already have happened).
* **Approve action** — consent to the step that needed approval, for this
  request only.
* **Abort** — end the run as ``ESCALATION_ABORTED``.

Resume, Retry and Approve on a request nobody has taken yet pass through
HUMAN_IN_CONTROL for the moment of deciding, so the transition log shows who
held the controls when the decision was made.

Every decision is also a line of ``human_actions.jsonl`` (``source: console``);
the count a run reports as ``human_actions_count`` is browser actions only.

No authentication: it binds to localhost, and anyone who can reach it can
drive the session. A remote console needs auth in front of this and of the
browser's debugging port; that is out of scope here and said so.
"""

from __future__ import annotations

import html
import json
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict

from cua.escalation.capture import CaptureError, CaptureSession
from cua.escalation.channel import HUMAN_ACTIONS_FILE, count_human_actions, waiter_alive
from cua.escalation.controller import (
    ControlRecord,
    ControlStore,
    IllegalTransition,
    OperatorDecision,
    StaleRequest,
)
from cua.escalation.requests import InterventionRequest, Queue, QueueEntry
from cua.evidence.logger import append_jsonl, utc_now
from cua.policy.allowlist import Policy
from cua.policy.redaction import Redactor
from cua.replay.runner import HANDOFF_STATE, ResumeContext, finalize_without_session

Action = Literal["take_control", "resume", "retry_step", "approve", "abort"]
ACTIONS: tuple[Action, ...] = ("take_control", "resume", "retry_step", "approve", "abort")


class ConsoleError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class View(BaseModel):
    """One request as the console shows it."""

    model_config = ConfigDict(frozen=True)

    request: InterventionRequest
    state: str
    holder: str
    current: bool
    """Still the run's open (or last) request."""
    status: str
    waiting: bool
    """A process is waiting on it: a handback resumes the run at once."""
    human_actions: int
    decision: OperatorDecision | None
    ended: str | None
    run_dir: str


class Console:
    def __init__(self, runs_dir: Path, *, policy: Policy | None = None) -> None:
        self.queue = Queue(runs_dir)
        self.redactor = Redactor.for_policy(policy) if policy is not None else Redactor([])
        self.captures: dict[str, CaptureSession] = {}
        self._lock = threading.Lock()

    # -- reading ---------------------------------------------------------------

    def views(self) -> list[View]:
        out = []
        for entry in self.queue.entries():
            try:
                out.append(self._view(entry))
            except (OSError, ValueError):
                continue  # a run directory that was removed under the queue
        return out

    def view(self, request_id: str) -> View:
        entry = self.queue.entry(request_id)
        if entry is None:
            raise ConsoleError(404, f"no request {request_id}")
        return self._view(entry)

    def _view(self, entry: QueueEntry) -> View:
        run_dir = self.queue.run_dir(entry)
        request = self.queue.request(entry)
        record = ControlStore(run_dir).read()
        current = record.request_id == entry.request_id
        return View(
            request=request,
            state=record.state if current else "CLOSED",
            holder=record.lease.holder if current else "-",
            current=current,
            status=_status(record, current),
            waiting=current and waiter_alive(run_dir),
            human_actions=count_human_actions(run_dir, entry.request_id),
            decision=record.decision if current else None,
            ended=record.ended,
            run_dir=run_dir.as_posix(),
        )

    def screenshot(self, request_id: str) -> Path:
        entry = self.queue.entry(request_id)
        if entry is None:
            raise ConsoleError(404, f"no request {request_id}")
        request = self.queue.request(entry)
        if request.masked_screenshot is None:
            raise ConsoleError(404, "this request has no screenshot")
        run_dir = self.queue.run_dir(entry).resolve()
        path = (run_dir / request.masked_screenshot).resolve()
        if run_dir not in path.parents or not path.is_file():
            raise ConsoleError(404, "no such screenshot")
        return path

    # -- deciding --------------------------------------------------------------

    def act(
        self,
        request_id: str,
        action: str,
        *,
        by: str,
        resume_at: str | None = None,
        why: str = "",
    ) -> View:
        by = by.strip()
        if not by:
            raise ConsoleError(400, "say who you are: every decision is recorded by name")
        if action not in ACTIONS:
            raise ConsoleError(404, f"no action {action!r}")
        entry = self.queue.entry(request_id)
        if entry is None:
            raise ConsoleError(404, f"no request {request_id}")
        request = self.queue.request(entry)
        if action not in request.options and action != "take_control":
            raise ConsoleError(409, f"{action} is not offered for this request")
        run_dir = self.queue.run_dir(entry)
        control = ControlStore(run_dir)
        resume_at = resume_at or None
        try:
            if action == "take_control":
                control.transition(
                    "HUMAN_IN_CONTROL", by=by, expect_request=request_id, note="took control"
                )
                self._record(run_dir, request_id, by=by, action=action)
                self._start_capture(request, run_dir)
            elif action == "abort":
                control.transition(
                    "ABORTED",
                    by=by,
                    expect_request=request_id,
                    decision=OperatorDecision(kind="abort", by=by, why=self.redactor.text(why)),
                    note="aborted from the console",
                )
                self._stop_capture(request_id)
                self._record(run_dir, request_id, by=by, action=action, why=why)
                self._finalize_if_orphaned(run_dir, why or f"aborted by {by}", by=by)
            else:
                if control.read().state == "PAUSED":
                    control.transition(
                        "HUMAN_IN_CONTROL",
                        by=by,
                        expect_request=request_id,
                        note=f"{action} from the console",
                    )
                control.transition(
                    "RESUMING",
                    by=by,
                    expect_request=request_id,
                    decision=OperatorDecision(
                        kind="hand_back",
                        by=by,
                        resume_at=resume_at,
                        retry=action == "retry_step",
                        approved=action == "approve",
                    ),
                    note=f"{action} from the console",
                )
                self._stop_capture(request_id)
                self._record(run_dir, request_id, by=by, action=action, resume_at=resume_at)
        except (IllegalTransition, StaleRequest) as exc:
            raise ConsoleError(409, str(exc)) from None
        return self.view(request_id)

    def sweep(self) -> None:
        """Expire requests past their time to live that no process is waiting
        on (a waiting process expires its own)."""
        now = utc_now()
        for view in self.views():
            if not view.current or view.state not in ("PAUSED", "HUMAN_IN_CONTROL"):
                continue
            if view.waiting or view.request.expires_at > now:
                continue
            run_dir = Path(view.run_dir)
            try:
                ControlStore(run_dir).transition(
                    "ABORTED",
                    by="timeout",
                    expect_request=view.request.id,
                    decision=OperatorDecision(
                        kind="abort", by="timeout", why="nobody answered in time"
                    ),
                    note="the request expired",
                )
            except (IllegalTransition, StaleRequest):
                continue
            self._stop_capture(view.request.id)
            self._finalize_if_orphaned(run_dir, "the request expired unanswered", by="timeout")

    def close(self) -> None:
        for request_id in list(self.captures):
            self._stop_capture(request_id)

    # -- helpers ---------------------------------------------------------------

    def _start_capture(self, request: InterventionRequest, run_dir: Path) -> None:
        def sink(record: dict[str, Any]) -> None:
            self._append(run_dir, {"request_id": request.id, "source": "browser", **record})

        capture = CaptureSession(cdp_url=request.cdp_url, target_id=request.target_id, sink=sink)
        try:
            capture.start()
        except CaptureError as exc:
            # The person has the controls either way; what they do just will
            # not be on record, and the record says so.
            self._record(run_dir, request.id, by="console", action="capture_failed", why=str(exc))
            return
        self.captures[request.id] = capture

    def _stop_capture(self, request_id: str) -> None:
        capture = self.captures.pop(request_id, None)
        if capture is not None:
            capture.stop()

    def _record(self, run_dir: Path, request_id: str, **fields: Any) -> None:
        clean = {k: v for k, v in fields.items() if v not in (None, "")}
        self._append(run_dir, {"request_id": request_id, "source": "console", **clean})

    def _append(self, run_dir: Path, record: dict[str, Any]) -> None:
        scrubbed = json.loads(self.redactor.text(json.dumps(record)))
        with self._lock:
            append_jsonl(run_dir / HUMAN_ACTIONS_FILE, {"ts": utc_now(), **scrubbed})

    def _finalize_if_orphaned(self, run_dir: Path, why: str, *, by: str) -> None:
        """An aborted replay nobody is waiting on is ended here, and its
        browser closed: nothing else would ever pick it up."""
        if waiter_alive(run_dir):
            return  # the waiting process ends the run itself
        state = run_dir / HANDOFF_STATE
        if not state.is_file():
            return
        ctx = ResumeContext.model_validate_json(state.read_text(encoding="utf-8"))
        finalize_without_session(run_dir, ctx, why=why, by=by)


def _status(record: ControlRecord, current: bool) -> str:
    if not current:
        return "closed: the run asked again since"
    if record.ended and record.ended != "escalated":
        return f"closed: the run ended ({record.ended})"
    return {
        "PAUSED": "open: waiting for someone to take it",
        "HUMAN_IN_CONTROL": "open: a person has the controls",
        "RESUMING": "handed back",
        "AUTOMATION": "resumed: the automation carried on",
        "ABORTED": "aborted",
    }.get(record.state, record.state)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Decide(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by: str
    resume_at: str | None = None
    why: str = ""


def create_app(runs_dir: Path, *, policy: Policy | None = None) -> FastAPI:
    console = Console(runs_dir, policy=policy)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        console.close()  # detach every capture; the sessions stay up

    app = FastAPI(title="cua operator console", lifespan=lifespan)
    app.state.console = console

    @app.exception_handler(ConsoleError)
    async def console_error(request: Request, exc: ConsoleError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    # -- JSON API (the scripted operator in the tests uses this) --------------

    @app.get("/api/requests")
    def api_list() -> list[dict[str, Any]]:
        console.sweep()
        return [v.model_dump(mode="json") for v in console.views()]

    @app.get("/api/requests/{request_id}")
    def api_get(request_id: str) -> dict[str, Any]:
        return console.view(request_id).model_dump(mode="json")

    @app.post("/api/requests/{request_id}/{action}")
    def api_act(request_id: str, action: str, body: Decide) -> dict[str, Any]:
        view = console.act(request_id, action, by=body.by, resume_at=body.resume_at, why=body.why)
        return view.model_dump(mode="json")

    # -- pages -----------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        console.sweep()
        return _page("Intervention requests", _list_html(console.views()), refresh=3)

    @app.get("/requests/{request_id}", response_class=HTMLResponse)
    def detail(request_id: str, said: str = "") -> str:
        try:
            view = console.view(request_id)
        except ConsoleError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        return _page(f"Request {request_id}", _detail_html(view, said))

    @app.get("/requests/{request_id}/screenshot")
    def screenshot(request_id: str) -> FileResponse:
        try:
            return FileResponse(console.screenshot(request_id), media_type="image/png")
        except ConsoleError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @app.post("/requests/{request_id}/{action}")
    def act(
        request_id: str,
        action: str,
        by: str = Form(default=""),
        resume_at: str = Form(default=""),
        why: str = Form(default=""),
    ) -> RedirectResponse:
        try:
            console.act(request_id, action, by=by, resume_at=resume_at, why=why)
            said = f"{action.replace('_', ' ')}: done"
        except ConsoleError as exc:
            said = f"refused: {exc}"
        return RedirectResponse(f"/requests/{request_id}?said={_q(said)}", status_code=303)

    return app


# --------------------------------------------------------------------------
# HTML (server-rendered; no framework, no external assets)
# --------------------------------------------------------------------------

_CSS = """
body{font:14px/1.45 system-ui,Segoe UI,sans-serif;margin:0;background:#f6f7f9;color:#1d2330}
header{background:#1d2330;color:#fff;padding:10px 20px}header a{color:#fff;text-decoration:none}
main{max-width:1100px;margin:0 auto;padding:16px 20px}
table{border-collapse:collapse;width:100%;background:#fff}
th,td{border-bottom:1px solid #e3e6eb;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#eef0f4;font-weight:600}
.card{background:#fff;border:1px solid #e3e6eb;border-radius:6px;padding:12px 16px;margin:12px 0}
.tag{display:inline-block;padding:1px 8px;border-radius:10px;background:#eef0f4;font-size:12px}
.open{background:#fff3cd}.human{background:#d9ecff}.done{background:#e3f5e1}.bad{background:#fde2e1}
pre{white-space:pre-wrap;background:#f3f4f6;padding:8px;border-radius:4px;max-height:320px;overflow:auto}
img{max-width:100%;border:1px solid #d0d4da}
form{display:inline-block;margin:4px 8px 4px 0}
button{padding:6px 12px;border:1px solid #9aa3b2;border-radius:4px;background:#fff;cursor:pointer}
button.primary{background:#1d62d8;color:#fff;border-color:#1d62d8}
button.danger{border-color:#c0392b;color:#c0392b}
input,select{padding:5px;border:1px solid #9aa3b2;border-radius:4px}
.said{padding:8px 12px;border-radius:4px;background:#eef0f4;margin:8px 0}
"""

_NAME_JS = """
<script>
(function(){
  var k='cua-operator-name', v='';
  try{v=localStorage.getItem(k)||''}catch(e){}
  document.querySelectorAll('input[name=by]').forEach(function(i){
    if(!i.value)i.value=v;
    i.addEventListener('change',function(){try{localStorage.setItem(k,i.value)}catch(e){}});
  });
})();
</script>
"""


def _page(title: str, body: str, *, refresh: int | None = None) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (
        f'<!doctype html><html><head><meta charset="utf-8">{meta}'
        f"<title>{_e(title)}</title><style>{_CSS}</style></head><body>"
        '<header><a href="/"><b>cua</b> operator console</a></header>'
        f"<main>{body}</main>{_NAME_JS}</body></html>"
    )


def _list_html(views: list[View]) -> str:
    if not views:
        return "<h2>Intervention requests</h2><p>No requests. This page refreshes itself.</p>"
    rows = "".join(
        "<tr>"
        f'<td><a href="/requests/{_e(v.request.id)}">{_e(v.request.id)}</a></td>'
        f"<td>{_e(v.request.created_at)}</td>"
        f"<td>{_e(v.request.capability)}</td>"
        f"<td>{_e(v.request.step_id or '-')}</td>"
        f"<td>{_e(v.request.reason_code)}<br><small>{_e(v.request.code)}</small></td>"
        f'<td><span class="tag {_css_for(v)}">{_e(v.status)}</span></td>'
        "</tr>"
        for v in views
    )
    return (
        "<h2>Intervention requests</h2>"
        "<table><tr><th>Request</th><th>Opened</th><th>Capability</th><th>Step</th>"
        f"<th>Reason</th><th>Status</th></tr>{rows}</table>"
    )


def _detail_html(v: View, said: str) -> str:
    r = v.request
    parts = [
        f'<p><a href="/">&larr; all requests</a></p><h2>{_e(r.capability)}'
        f"{f' v{r.capability_version}' if r.capability_version else ''} "
        f"&middot; {_e(r.step_id or '-')}</h2>"
    ]
    if said:
        parts.append(f'<div class="said">{_e(said)}</div>')
    parts.append(
        '<div class="card">'
        f'<p><span class="tag {_css_for(v)}">{_e(v.status)}</span> '
        f"state <b>{_e(v.state)}</b>, controls held by <b>{_e(v.holder)}</b>"
        f"{' &middot; the run is waiting for this answer' if v.waiting else ''}</p>"
        f"<p><b>{_e(r.reason_code)}</b> ({_e(r.code)}){_attempt(r)}: {_e(r.message)}</p>"
        f"<p>Expected: {_e(r.expected or '-')}</p>"
        f"<p>Side effect so far: <b>{_e(r.side_effect)}</b> &middot; human actions recorded: "
        f"{v.human_actions}</p>"
        f"<p>Run <code>{_e(r.run_id)}</code> &middot; tenant {_e(r.tenant)} &middot; "
        f"opened {_e(r.created_at)}, expires {_e(r.expires_at)}</p>"
        "</div>"
    )
    if v.current and v.state in ("PAUSED", "HUMAN_IN_CONTROL"):
        parts.append(_actions_html(v))
    elif v.current and v.state == "RESUMING" and not v.waiting and v.ended == "escalated":
        parts.append(
            '<div class="card">Handed back. No process is waiting on this run: its caller '
            "carries it on with the resume token it was given "
            "(<code>cua resume &lt;token&gt;</code>).</div>"
        )
    parts.append(
        '<div class="card"><h3>The live session</h3>'
        f"<p>DevTools endpoint: <code>{_e(r.cdp_url)}</code>"
        f"{f' &middot; page <code>{_e(r.target_id)}</code>' if r.target_id else ''}</p>"
        + (
            f'<p><a href="{_e(r.devtools_url)}" target="_blank">Open the page in DevTools</a> '
            "(for a headless session: shows the page and takes clicks and keys)</p>"
            if r.devtools_url
            else ""
        )
        + "</div>"
    )
    if r.masked_screenshot:
        parts.append(
            '<div class="card"><h3>Screen when it stopped (masked)</h3>'
            f'<img src="/requests/{_e(r.id)}/screenshot" alt="masked screenshot"></div>'
        )
    parts.append(
        f'<div class="card"><h3>What the automation saw</h3><pre>{_e(r.a11y_excerpt)}</pre></div>'
    )
    return "".join(parts)


def _actions_html(v: View) -> str:
    r = v.request
    rid = _e(r.id)
    name = '<input name="by" placeholder="your name" required size="14">'
    forms = []
    if v.state == "PAUSED":
        forms.append(
            f'<form method="post" action="/requests/{rid}/take_control">{name}'
            '<button class="primary">Take control</button></form>'
        )
    choices = "".join(
        f'<option value="{_e(c.step_id)}">{_e(c.label)}</option>' for c in r.resume_points
    )
    forms.append(
        f'<form method="post" action="/requests/{rid}/resume">{name} '
        '<select name="resume_at"><option value="">wherever the screen shows</option>'
        f"{choices}</select> <button>Resume</button></form>"
    )
    if "retry_step" in r.options:
        forms.append(
            f'<form method="post" action="/requests/{rid}/retry_step">{name}'
            "<button>Retry step</button></form>"
        )
    if "approve" in r.options:
        forms.append(
            f'<form method="post" action="/requests/{rid}/approve">{name}'
            '<button class="primary">Approve action</button></form>'
        )
    forms.append(
        f'<form method="post" action="/requests/{rid}/abort">{name} '
        '<input name="why" placeholder="why (optional)" size="18"> '
        '<button class="danger">Abort</button></form>'
    )
    hint = (
        "<p>Take control, then work in the browser window (or the DevTools link below). "
        "When you are done, <b>Resume</b>: the run checks the screen against its checkpoints "
        "and carries on from the newest one that holds.</p>"
    )
    return f'<div class="card"><h3>Decide</h3>{hint}{"".join(forms)}</div>'


def _attempt(r: InterventionRequest) -> str:
    return f", asked {r.attempt} times" if r.attempt > 1 else ""


def _css_for(v: View) -> str:
    if v.state == "PAUSED":
        return "open"
    if v.state == "HUMAN_IN_CONTROL":
        return "human"
    if v.state == "ABORTED":
        return "bad"
    return "done"


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _q(text: str) -> str:
    return quote(text, safe="")
