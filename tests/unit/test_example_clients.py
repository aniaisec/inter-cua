"""Caller contracts that cannot safely be simulated by killing a real write."""

import json
import re
from pathlib import Path

import httpx
import pytest

from examples._support import ExampleError, check_subset
from examples.http_client.client import Conflict, RunClient, Submission, UnknownSubmission
from examples.mcp_setup.client import configuration
from examples.run import EXAMPLES, NAMES, Session, scenario


@pytest.mark.parametrize("name", NAMES)
def test_documented_command_and_expected_contract_share_the_scenario(name):
    spec = scenario(name)
    doc = (EXAMPLES / name / "README.md").read_text()
    assert f"python -m examples.run {name} --out " in doc
    expected = re.search(r"```json\n(.*?)\n```", doc, re.DOTALL)
    assert expected and json.loads(expected[1]) == spec["expected"]
    assert spec["capabilities"] and spec["purpose"]


def test_contract_matching_checks_json_types():
    with pytest.raises(ExampleError):
        check_subset({"quantity": "4"}, {"quantity": 4})
    with pytest.raises(ExampleError):
        check_subset({"quantity": True}, {"quantity": 1})


@pytest.mark.parametrize("state", ["escalated", "finished", "error", "lost"])
def test_http_polls_queued_and_running_then_returns_every_stopped_state(state):
    requests = []
    records = iter(
        [
            {"run_id": "run_1", "state": "running"},
            {"run_id": "run_1", "state": state, "result": {"side_effect": "unknown"}},
        ]
    )

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=next(records))

    with httpx.Client(base_url="http://example", transport=httpx.MockTransport(handle)) as http:
        client = RunClient(http, api_key="synthetic", tenant="local")
        result = client.poll({"run_id": "run_1", "state": "queued"}, interval_s=0)
    assert result["state"] == state
    assert [request.method for request in requests] == ["GET", "GET"]


def test_transport_retry_preserves_body_key_request_and_does_not_use_mutated_inputs():
    requests = []
    body = {"capability": "adjust_stock", "inputs": {"quantity_change": -1}}
    submission = Submission.create(body, key="persisted-key", request_id="request-1")
    body["inputs"]["quantity_change"] = -2

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("lost answer", request=request)
        return httpx.Response(202, json={"run_id": "run_1", "state": "running"})

    with httpx.Client(base_url="http://example", transport=httpx.MockTransport(handle)) as http:
        assert (
            RunClient(http, api_key="synthetic", tenant="local").submit(submission)["run_id"]
            == "run_1"
        )
    assert len(requests) == 2
    assert requests[0].content == requests[1].content == submission.payload
    assert json.loads(requests[1].content)["inputs"]["quantity_change"] == -1
    for request in requests:
        assert request.headers["Idempotency-Key"] == "persisted-key"
        assert request.headers["X-Request-Id"] == "request-1"


def test_conflict_never_rekeys_or_retries():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(409, json={"error": {"code": "idempotency_conflict"}})

    with httpx.Client(base_url="http://example", transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(Conflict):
            RunClient(http, api_key="synthetic", tenant="local").submit(
                Submission.create({}, key="original-key", request_id="request-1")
            )
    assert len(requests) == 1 and requests[0].headers["Idempotency-Key"] == "original-key"


def test_transport_exhaustion_retains_identity_and_stops():
    requests = []

    def handle(request):
        requests.append(request)
        raise httpx.ConnectError("unavailable", request=request)

    with httpx.Client(base_url="http://example", transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(UnknownSubmission):
            RunClient(http, api_key="synthetic", tenant="local").submit(
                Submission.create({}, key="original-key", request_id="request-1")
            )
    assert len(requests) == 2
    assert all(request.headers["Idempotency-Key"] == "original-key" for request in requests)


def test_poll_timeout_never_starts_a_replacement(monkeypatch):
    times = iter([0, 2])
    monkeypatch.setattr("examples.http_client.client.time.monotonic", lambda: next(times))
    with httpx.Client(base_url="http://example") as http:
        with pytest.raises(TimeoutError):
            RunClient(http, api_key="synthetic", tenant="local").poll(
                {"run_id": "run_1", "state": "running"}, timeout_s=1
            )


def test_unknown_state_fails_closed_without_a_request():
    with httpx.Client(base_url="http://example") as http:
        with pytest.raises(ValueError):
            RunClient(http, api_key="synthetic", tenant="local").poll(
                {"run_id": "run_1", "state": "future-state"}
            )


def test_existing_output_is_refused(tmp_path):
    with pytest.raises(FileExistsError):
        Session(scenario("lookup"), tmp_path)


def test_recording_omits_private_tokens(tmp_path):
    session = Session(scenario("lookup"), tmp_path / "output")
    session.record(
        ["cua", "replay"],
        3,
        {
            "kind": "escalated",
            "resume_token": "private-resume-token",
            "approval": {"token": "private-consent-token"},
            "outputs": {},
        },
    )
    for path in (session.out / "commands.json", session.out / "demonstration.cast"):
        text = Path(path).read_text()
        assert "private-resume-token" not in text and "private-consent-token" not in text


def test_mcp_configuration_keeps_the_virtualenv_interpreter(monkeypatch, tmp_path):
    project = tmp_path / "project with spaces"
    project.mkdir()
    (project / "cua.toml").write_text("version = 1\n")
    interpreter = tmp_path / "venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("synthetic interpreter path")
    monkeypatch.setattr("examples.mcp_setup.client.sys.executable", str(interpreter))
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == interpreter:
            return tmp_path / "system-python"
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    config = configuration(project)
    assert config["command"] == str(interpreter.absolute())
    assert config["args"] == ["-m", "cua.cli", "mcp", "--root", str(project.resolve())]


def test_recorded_handoff_includes_review_result_and_checkpoint_without_tokens():
    text = (EXAMPLES / "human_takeover/demonstration.cast").read_text()
    header, *frames = [json.loads(line) for line in text.splitlines()]
    assert header["version"] == 2
    assert [frame[0] for frame in frames] == sorted(frame[0] for frame in frames)
    output = "".join(frame[2] for frame in frames)
    for event in ("describe", "approve", '"escalated"', '"human_action"', "cp.done", '"success"'):
        assert event in output
    assert "cat1." not in text and "rsm_" not in text and "practice-password" not in text


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["timeout_s", "interval_s"])
def test_poll_requires_finite_limits(value, field):
    with httpx.Client(base_url="http://example") as http:
        with pytest.raises(ValueError, match="finite"):
            RunClient(http, api_key="synthetic", tenant="local").poll(
                {"run_id": "run_1", "state": "running"}, **{field: value}
            )


def test_poll_does_not_dispatch_after_the_sleep_uses_the_deadline(monkeypatch):
    times = iter([0, 0, 2])
    monkeypatch.setattr("examples.http_client.client.time.monotonic", lambda: next(times))
    monkeypatch.setattr("examples.http_client.client.time.sleep", lambda _: None)
    with httpx.Client(base_url="http://example") as http:
        with pytest.raises(TimeoutError):
            RunClient(http, api_key="synthetic", tenant="local").poll(
                {"run_id": "run_1", "state": "running"}, timeout_s=1, interval_s=1
            )


def test_poll_bounds_the_http_timeout_by_the_remaining_budget(monkeypatch):
    times = iter([0, 0.5, 0.75])
    monkeypatch.setattr("examples.http_client.client.time.monotonic", lambda: next(times))
    monkeypatch.setattr("examples.http_client.client.time.sleep", lambda _: None)

    def handle(request):
        assert all(limit == 0.25 for limit in request.extensions["timeout"].values())
        return httpx.Response(200, json={"run_id": "run_1", "state": "finished"})

    with httpx.Client(base_url="http://example", transport=httpx.MockTransport(handle)) as http:
        assert (
            RunClient(http, api_key="synthetic", tenant="local").poll(
                {"run_id": "run_1", "state": "running"}, timeout_s=1, interval_s=0
            )["state"]
            == "finished"
        )


@pytest.mark.parametrize("record", [None, [], {}, {"run_id": "", "state": "running"}])
def test_malformed_http_response_is_refused_without_retry(record):
    calls = []

    def handle(request):
        calls.append(request)
        return (
            httpx.Response(200, json=record)
            if record is not None
            else httpx.Response(200, content="null")
        )

    with httpx.Client(base_url="http://example", transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(ValueError, match="unrecognized"):
            RunClient(http, api_key="synthetic", tenant="local").submit(
                Submission.create({}, key="same-key", request_id="request-1")
            )
    assert len(calls) == 1
