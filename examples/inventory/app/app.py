"""Small inventory UI with an independent, read-only test oracle.

Bind only to loopback. Credentials, faults and the oracle are synthetic demo
features. State is process-local; restart the server for a fresh scenario.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

SEED = {"SKU-001": ("Canvas tote", 12), "SKU-002": ("Desk lamp", 4)}
FAULTS = {
    "renamed_control",
    "ambiguous_control",
    "session_expired",
    "validation_fault",
    "changed_screen",
}
COOKIE = "inventory_session"
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@dataclass
class Session:
    authed: bool = False
    fault: str | None = None
    fired: bool = False
    verified: bool = False
    pending: tuple[str, int] | None = None


@dataclass
class State:
    quantities: dict[str, int] = field(default_factory=lambda: {k: v[1] for k, v in SEED.items()})
    sessions: dict[str, Session] = field(default_factory=dict)
    adjustments: list[dict[str, Any]] = field(default_factory=list)
    commit_posts: int = 0


def create_app() -> FastAPI:
    app = FastAPI(title="Stockroom demo", docs_url=None, redoc_url=None, openapi_url=None)
    state = State()

    @app.middleware("http")
    async def session_middleware(request: Request, call_next: Any) -> Response:
        sid = request.cookies.get(COOKIE, "")
        is_new = sid not in state.sessions
        if is_new:
            sid = uuid.uuid4().hex
            state.sessions[sid] = Session()
        session = state.sessions[sid]
        fault = request.query_params.get("inject")
        if fault is not None:
            if fault not in FAULTS:
                return HTMLResponse("Unknown inventory fault", status_code=400)
            session.fault = fault
        request.state.inventory = session
        response: Response = await call_next(request)
        if is_new:
            response.set_cookie(COOKIE, sid, httponly=True, samesite="strict")
        return response

    def session(request: Request) -> Session:
        return request.state.inventory  # type: ignore[no-any-return]

    def screen(request: Request, view: str, **context: Any) -> Response:
        return TEMPLATES.TemplateResponse(
            request=request, name="screen.html", context={"view": view, **context}
        )

    def redirect(path: str) -> RedirectResponse:
        return RedirectResponse(path, status_code=303)

    @app.get("/login")
    def login_form(request: Request) -> Response:
        return screen(request, "login")

    @app.post("/login")
    def login(request: Request, username: str = Form(""), password: str = Form("")) -> Response:
        if (username, password) != ("clerk", "practice-password"):
            return screen(request, "login", error="Invalid inventory credentials")
        session(request).authed = True
        return redirect("/lookup")

    @app.get("/lookup")
    def lookup_form(request: Request) -> Response:
        if not session(request).authed:
            return redirect("/login")
        return screen(request, "lookup", fault=session(request).fault)

    @app.post("/lookup")
    def lookup(request: Request, item_code: str = Form("")) -> Response:
        current = session(request)
        if not current.authed:
            return redirect("/login")
        if current.fault == "session_expired" and not current.fired:
            current.fired = True
            current.authed = False
            return redirect("/login")
        if item_code not in SEED:
            return screen(request, "lookup", fault=current.fault, error="Item not found")
        return redirect(f"/items/{item_code}")

    @app.get("/items/{item_code}")
    def item(request: Request, item_code: str, verified: bool = False) -> Response:
        current = session(request)
        if not current.authed:
            return redirect("/login")
        if item_code not in SEED:
            return screen(request, "lookup", error="Item not found")
        current.verified |= verified
        changed = current.fault == "changed_screen" and not current.verified
        return screen(
            request,
            "item",
            code=item_code,
            title=SEED[item_code][0],
            quantity=state.quantities[item_code],
            changed=changed,
        )

    @app.get("/adjust/{item_code}")
    def adjustment_form(request: Request, item_code: str) -> Response:
        if not session(request).authed:
            return redirect("/login")
        if item_code not in SEED:
            return redirect("/lookup")
        return screen(request, "adjust", code=item_code)

    @app.post("/adjust/{item_code}")
    def prepare(request: Request, item_code: str, quantity_change: str = Form("")) -> Response:
        current = session(request)
        if not current.authed:
            return redirect("/login")
        current.pending = None
        if item_code not in SEED:
            return screen(request, "adjust", code=item_code, error="Item not found")
        try:
            delta = int(quantity_change)
        except ValueError:
            delta = 0
        if not delta or abs(delta) > 1000 or current.fault == "validation_fault":
            return screen(
                request,
                "adjust",
                code=item_code,
                error="Quantity change must be a nonzero integer between -1000 and 1000",
            )
        if state.quantities[item_code] + delta < 0:
            return screen(request, "adjust", code=item_code, error="Insufficient stock")
        current.pending = (item_code, delta)
        return redirect(f"/review/{item_code}")

    @app.get("/review/{item_code}")
    def review(request: Request, item_code: str) -> Response:
        current = session(request)
        if not current.authed:
            return redirect("/login")
        if current.pending is None or current.pending[0] != item_code:
            return redirect("/lookup")
        return screen(request, "review", code=item_code, delta=current.pending[1])

    @app.post("/review/{item_code}")
    def commit(request: Request, item_code: str) -> Response:
        current = session(request)
        if not current.authed:
            return redirect("/login")
        state.commit_posts += 1
        pending, current.pending = current.pending, None
        if pending is None or pending[0] != item_code:
            return HTMLResponse("No pending adjustment", status_code=409)
        delta = pending[1]
        # Recheck after review: another session may have changed stock meanwhile.
        if state.quantities[item_code] + delta < 0:
            return screen(request, "adjust", code=item_code, error="Insufficient stock")
        state.quantities[item_code] += delta
        receipt = f"ADJ-{len(state.adjustments) + 1:04d}"
        state.adjustments.append(
            {
                "receipt": receipt,
                "item_code": item_code,
                "quantity_change": delta,
                "quantity": state.quantities[item_code],
            }
        )
        return screen(
            request,
            "committed",
            code=item_code,
            receipt=receipt,
            quantity=state.quantities[item_code],
        )

    @app.get("/_test/state")
    def oracle() -> dict[str, Any]:
        # Automation policy excludes this route. Tests inspect state over HTTP,
        # independently of the UI extraction and replay side-effect claims.
        return {
            "quantities": dict(state.quantities),
            "adjustments": list(state.adjustments),
            "commit_count": len(state.adjustments),
            "commit_posts": state.commit_posts,
        }

    return app


app = create_app()
