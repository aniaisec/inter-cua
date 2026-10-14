"""Generated boundary cases: caller data cannot gain code or approval authority."""

import itertools
import json
import re
from decimal import Decimal

import pytest

from cua import catalog
from cua.artifact.schema import InputSpec
from cua.artifact.store import ArtifactError, load, with_changes
from cua.project import ProjectContext
from cua.registry.store import Registry, RegistryError
from cua.replay.invocation import bind, check_input, fill, validate_inputs
from cua.surface.conditions import AllOf, LocationMatches, TextPresent
from tests.unit.test_replay import engine, goal1, review

VALUES = [
    None,
    True,
    False,
    [],
    {},
    12,
    -12,
    1.25,
    float("inf"),
    float("nan"),
    "12",
    "0012",
    "1e2",
    "",
]


@pytest.mark.parametrize(
    "kind,value", list(itertools.product(("string", "integer", "decimal"), VALUES))
)
def test_json_coercion_and_replay_validation_agree_on_acceptance(kind, value):
    cap = with_changes(goal1(), inputs={"member_id": InputSpec(type=kind).model_dump()})
    inputs, problems = catalog.coerce(cap, {"member_id": value})
    problems += validate_inputs(cap, inputs)
    if kind == "string":
        expected = isinstance(value, str)
    elif kind == "integer":
        expected = (isinstance(value, int) and not isinstance(value, bool)) or (
            isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value) is not None
        )
    else:
        expected = isinstance(value, str | int | float) and not isinstance(value, bool)
        if expected:
            try:
                expected = Decimal(str(value)).is_finite()
            except ArithmeticError:
                expected = False
    assert bool(problems) is not expected


@pytest.mark.parametrize("value", [".*", "a|b", "[x]", r"\d+", "${other}", "a(b)$", "雪 / space"])
def test_nested_regex_binding_matches_only_the_literal_input(value):
    original = AllOf(all_of=[LocationMatches(pattern="^${value}$"), TextPresent(text="${value}")])
    answer = bind(original, {"value": value, "other": "expanded"})
    assert re.fullmatch(answer.all_of[0].pattern, value)
    assert answer.all_of[1].text == value
    assert original.all_of[0].pattern == "^${value}$"
    assert (
        fill("${credentials.login.password}", {"value": value}) == "${credentials.login.password}"
    )


@pytest.mark.parametrize("value", ["1e2", "not-a-number", "NaN", "Infinity"])
def test_sensitive_type_and_pattern_failures_do_not_disclose_values(value):
    spec = InputSpec(type="integer", sensitive=True, pattern="^0$")
    problem = check_input("secret", value, spec)
    assert problem and value not in problem and "***" in problem


@pytest.mark.parametrize("name", ["project with spaces", "project.雪", "nested/a/b"])
@pytest.mark.parametrize("configured", ["evidence/runs", "./out/../results", "../shared results"])
def test_selected_root_remains_stable_across_unrelated_cwds(
    tmp_path, monkeypatch, name, configured
):
    root = tmp_path / name
    root.mkdir(parents=True)
    (root / "cua.toml").write_text(
        f"version = 1\n[paths]\nruns = {json.dumps(configured, ensure_ascii=False)}\n",
        encoding="utf-8",
    )
    project = ProjectContext.resolve(root, cwd=tmp_path, environ={})
    selected = (root / configured).resolve()
    other = tmp_path / "unrelated"
    other.mkdir(exist_ok=True)
    monkeypatch.chdir(other)
    assert project.path("runs") == selected
    assert project.explicit("caller file.json") == (tmp_path / "caller file.json").resolve()


@pytest.mark.parametrize("record", ["{", "null", "[]", "{}", '{"schema_version":"99"}'])
def test_malformed_capabilities_are_never_offered_as_tools(tmp_path, record):
    path = tmp_path / "broken.json"
    path.write_text(record, encoding="utf-8")
    with pytest.raises(ArtifactError):
        load(path)
    entries, errors = catalog.scan(tmp_path)
    assert entries == [] and errors and errors[0].path == path


@pytest.mark.parametrize("record", ['{"event":', "null", "[]", "{}"])
def test_malformed_authoritative_ledger_never_means_no_revocation(tmp_path, record):
    registry = Registry(tmp_path)
    registry.root.mkdir()
    registry.ledger_path.write_text(record + "\n", encoding="utf-8")
    with pytest.raises(RegistryError):
        registry.ledger()


@pytest.mark.parametrize(
    "value",
    [
        "${credentials.app_login.password}",
        "prefix ${credentials.app_login.username}",
        "${initial_deposit}",
    ],
)
def test_untrusted_input_is_literal_even_when_it_looks_like_a_placeholder(tmp_path, value):
    run, _ = engine(tmp_path, [review()])
    run.inputs["member_id"] = value
    step = goal1().steps[0]
    assert run._expand(step, "${member_id}") == value
    assert "hunter2pw" not in run._expand(step, "${member_id}")


def test_credential_contents_are_not_rebound_as_input_templates(tmp_path):
    from cua.secrets.resolver import Credential

    run, _ = engine(tmp_path, [review()])
    run.credentials["app_login"] = Credential("synthetic", {"password": "${member_id}"})
    assert run._expand(goal1().steps[0], "${credentials.app_login.password}") == "${member_id}"


def test_artifact_declared_missing_credential_fails_closed(tmp_path):
    from cua.replay.engine import _Stop

    run, _ = engine(tmp_path, [review()])
    with pytest.raises(_Stop) as stopped:
        run._expand(goal1().steps[0], "${credentials.missing.password}")
    assert stopped.value.result.code == "POLICY_BLOCKED"
