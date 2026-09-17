"""A deliberately hostile mock of a legacy credit-union core banking UI.

Design notes (these are the point of the app, not accidents):

* The signed-in UI is a ``<frameset>``: a nav frame and a main frame. Perception
  has to walk every frame, not just the top document.
* Every layout is a nested ``<table>``. There are no ``id``, ``data-*`` or ARIA
  attributes, and no ``<label for>``, so text inputs have **no accessible
  name** — the ``near_text`` locator rung has to earn its keep.
* Buttons are ``<input type=submit>``, so their accessible name is the value
  attribute and it can change (see the ``renamed_button`` inject).
* Form fields do carry cryptic ``name`` attributes (``F_USRID``, ``F_MBRID``).
  HTML form submission requires them, and real legacy apps have exactly this
  kind of name. They contribute nothing to the accessibility tree and nothing
  in ``src/cua`` is allowed to use them as selectors.
* Screen titles are real headings, so ``region_present`` has something honest
  to resolve against. Headings never help locate an input.

Sessions are a server-side dict keyed by a cookie. Nothing is persisted.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from mockapp import injects
from mockapp.data import (
    ACCOUNT_TYPES,
    OPERATOR_PASSWORD,
    OPERATOR_USERNAME,
    find_member,
    format_currency,
    reference_number,
)
from mockapp.injects import Inject

COOKIE_NAME = "MCSESSID"
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="MockCore 1.0", docs_url=None, redoc_url=None, openapi_url=None)

# Session id -> session state. Process-local; the app is single-tenant and
# single-user by design.
SESSIONS: dict[str, dict[str, Any]] = {}


def _new_session() -> dict[str, Any]:
    return {
        "authed": False,
        "inject": None,
        "fired": [],  # inject modes that have fired, in order — read by tests
        "pending": None,  # in-flight sub-account application
        "confirm_seq": 0,
    }


@app.middleware("http")
async def session_middleware(request: Request, call_next: Any) -> Response:
    existing = request.cookies.get(COOKIE_NAME)
    is_new = existing is None or existing not in SESSIONS
    sid = uuid.uuid4().hex if is_new else str(existing)
    if is_new:
        SESSIONS[sid] = _new_session()
    session = SESSIONS[sid]

    raw = request.query_params.get("inject") or request.headers.get("X-Inject")
    if raw is not None:
        try:
            session["inject"] = injects.parse(raw)
        except ValueError:
            return HTMLResponse(
                f"<html><body><h3>Unknown inject mode: {raw}</h3></body></html>",
                status_code=400,
            )

    request.state.session = session
    response: Response = await call_next(request)
    if is_new:
        response.set_cookie(COOKIE_NAME, sid, samesite="lax")
    return response


def _session(request: Request) -> dict[str, Any]:
    return request.state.session  # type: ignore[no-any-return]


def _armed(session: dict[str, Any], mode: Inject) -> bool:
    """Is this mode armed? Records nothing and consumes nothing.

    Only for presentation-only modes (renamed_button, ambiguous_button), where
    nothing "happens" — a screen simply renders differently.
    """
    return bool(session["inject"] == mode)


def _fire(session: dict[str, Any], mode: Inject) -> bool:
    """Fire the mode if armed: record it, and consume it when it is one-shot."""
    if session["inject"] != mode:
        return False
    session["fired"].append(mode.value)
    if mode in injects.ONE_SHOT:
        session["inject"] = None
    return True


def _render(request: Request, template: str, **ctx: Any) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request=request, name=template, context=ctx)


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


# --------------------------------------------------------------------------
# Sign on
# --------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    return _render(request, "login.html", error=None)


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    F_USRID: str = Form(default=""),
    F_PWD: str = Form(default=""),
) -> Response:
    session = _session(request)
    if F_USRID.strip() != OPERATOR_USERNAME or F_PWD != OPERATOR_PASSWORD:
        session["authed"] = False
        return _render(request, "login.html", error="Invalid user id or password.")

    session["authed"] = True
    if _fire(session, Inject.INTERSTITIAL_DIALOG):
        return RedirectResponse("/notice", status_code=303)
    return RedirectResponse("/", status_code=303)


@app.get("/notice", response_class=HTMLResponse)
async def notice(request: Request) -> Response:
    if not _session(request)["authed"]:
        return _login_redirect()
    return _render(request, "notice.html")


@app.get("/logoff", response_class=HTMLResponse)
async def logoff(request: Request) -> Response:
    _session(request)["authed"] = False
    return _login_redirect()


# --------------------------------------------------------------------------
# Frameset shell
# --------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def shell(request: Request) -> Response:
    if not _session(request)["authed"]:
        return _login_redirect()
    return _render(request, "frameset.html")


@app.get("/nav", response_class=HTMLResponse)
async def nav(request: Request) -> Response:
    session = _session(request)
    if not session["authed"]:
        return _login_redirect()
    # ambiguous_button puts a second control named "Search" in the nav frame,
    # so the role_name rung resolves to two nodes and has to fall through.
    return _render(
        request,
        "nav.html",
        ambiguous=_armed(session, Inject.AMBIGUOUS_BUTTON),
    )


# --------------------------------------------------------------------------
# Member search
# --------------------------------------------------------------------------


def _search_button_label(session: dict[str, Any]) -> str:
    return "Find" if _armed(session, Inject.RENAMED_BUTTON) else "Search"


@app.get("/search", response_class=HTMLResponse)
async def search_form(request: Request) -> Response:
    session = _session(request)
    if not session["authed"]:
        return _login_redirect()
    return _render(
        request,
        "search.html",
        submit_label=_search_button_label(session),
        error=None,
        member_id="",
    )


@app.post("/search", response_class=HTMLResponse)
async def search_submit(request: Request, F_MBRID: str = Form(default="")) -> Response:
    session = _session(request)
    if not session["authed"]:
        return _login_redirect()

    member_id = F_MBRID.strip()
    forced_miss = _fire(session, Inject.NOT_FOUND)
    member = None if forced_miss else find_member(member_id)
    if member is None:
        return _render(
            request,
            "search.html",
            submit_label=_search_button_label(session),
            error="No matching member",
            member_id=member_id,
        )
    return RedirectResponse(f"/member/{member.member_id}", status_code=303)


# --------------------------------------------------------------------------
# Member detail
# --------------------------------------------------------------------------


@app.get("/member/{member_id}", response_class=HTMLResponse)
async def member_detail(request: Request, member_id: str) -> Response:
    session = _session(request)
    if not session["authed"]:
        return _login_redirect()

    # Order matters: session expiry unseats the request before anything else
    # can render, and a 500 never gets to be slow.
    if _fire(session, Inject.SESSION_EXPIRED):
        session["authed"] = False
        return _login_redirect()

    if _fire(session, Inject.SERVER_ERROR):
        return _render_error(request)

    if _fire(session, Inject.SLOW_LOAD):
        await asyncio.sleep(injects.SLOW_LOAD_SECONDS)

    member = find_member(member_id)
    if member is None:
        return _render(
            request,
            "search.html",
            submit_label=_search_button_label(session),
            error="No matching member",
            member_id=member_id,
        )

    forced_denial = _fire(session, Inject.PERMISSION_DENIED)
    if member.restricted or forced_denial:
        return _render(request, "denied.html", member=member)

    return _render(
        request,
        "member.html",
        member=member,
        rows=[
            ("Savings", format_currency(member.savings)),
            ("Checking", format_currency(member.checking)),
        ],
    )


def _render_error(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request=request, name="error500.html", context={}, status_code=500
    )


# --------------------------------------------------------------------------
# Open sub-account: form -> review -> confirmation
# --------------------------------------------------------------------------


@app.get("/subaccount/{member_id}", response_class=HTMLResponse)
async def subaccount_form(request: Request, member_id: str) -> Response:
    session = _session(request)
    if not session["authed"]:
        return _login_redirect()
    member = find_member(member_id)
    if member is None or member.restricted:
        return _render(request, "denied.html", member=member)
    return _render(
        request,
        "subaccount.html",
        member=member,
        account_types=ACCOUNT_TYPES,
        deposit="",
        error=None,
    )


@app.post("/subaccount/{member_id}", response_class=HTMLResponse)
async def subaccount_submit(
    request: Request,
    member_id: str,
    F_ACCTTYP: str = Form(default="Savings"),
    F_DEPAMT: str = Form(default=""),
) -> Response:
    session = _session(request)
    if not session["authed"]:
        return _login_redirect()
    member = find_member(member_id)
    if member is None or member.restricted:
        return _render(request, "denied.html", member=member)

    deposit = F_DEPAMT.strip()
    forced_invalid = _fire(session, Inject.VALIDATION_ERROR)
    if forced_invalid or deposit == "":
        return _render(
            request,
            "subaccount.html",
            member=member,
            account_types=ACCOUNT_TYPES,
            deposit=deposit,
            error="Initial deposit is required",
        )

    session["pending"] = {"account_type": F_ACCTTYP, "deposit": deposit}
    return RedirectResponse(f"/review/{member.member_id}", status_code=303)


@app.get("/review/{member_id}", response_class=HTMLResponse)
async def review(request: Request, member_id: str) -> Response:
    session = _session(request)
    if not session["authed"]:
        return _login_redirect()
    member = find_member(member_id)
    pending = session["pending"]
    if member is None or pending is None:
        return RedirectResponse(f"/subaccount/{member_id}", status_code=303)
    return _render(request, "review.html", member=member, pending=pending)


@app.post("/review/{member_id}", response_class=HTMLResponse)
async def confirm(request: Request, member_id: str) -> Response:
    session = _session(request)
    if not session["authed"]:
        return _login_redirect()
    member = find_member(member_id)
    pending = session["pending"]
    if member is None or pending is None:
        return RedirectResponse(f"/subaccount/{member_id}", status_code=303)

    # slow_confirm delays the irreversible step past the engine's step timeout.
    # The write still lands, which is exactly why replay must report
    # side_effect: unknown rather than retry the click.
    if _fire(session, Inject.SLOW_CONFIRM):
        await asyncio.sleep(injects.SLOW_CONFIRM_SECONDS)

    session["confirm_seq"] += 1
    ref = reference_number(member.member_id, session["confirm_seq"])
    session["pending"] = None
    return _render(request, "confirm.html", member=member, pending=pending, reference=ref)


# --------------------------------------------------------------------------
# Test/debug surface. Not part of the automated UI; used by fixtures to assert
# which inject modes actually fired.
# --------------------------------------------------------------------------


@app.get("/_debug/session")
async def debug_session(request: Request) -> dict[str, Any]:
    session = _session(request)
    inject = session["inject"]
    return {
        "authed": session["authed"],
        "inject": inject.value if inject else None,
        "fired": list(session["fired"]),
        "pending": session["pending"],
    }


@app.post("/_debug/reset")
async def debug_reset(request: Request) -> dict[str, str]:
    session = _session(request)
    session.update(_new_session())
    return {"status": "reset"}


def main() -> None:  # pragma: no cover - convenience entry point
    import uvicorn

    uvicorn.run(
        "mockapp.app:app",
        host="127.0.0.1",
        port=int(os.environ.get("MOCKAPP_PORT", "8000")),
        log_level=os.environ.get("MOCKAPP_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
