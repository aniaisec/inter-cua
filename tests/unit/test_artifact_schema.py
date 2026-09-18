"""The capability schema: what parses, what is refused, and the exported JSON
Schema that non-Python consumers validate against.

Every rejection starts from the golden capability and breaks one thing, so a
failure here names exactly the rule that stopped holding.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

import jsonschema
import pytest

from cua.artifact.schema import SCHEMA_PATH, Capability, json_schema
from cua.artifact.store import ArtifactError, parse
from tests.unit import artifacts

Doc = dict[str, Any]


def golden() -> Doc:
    return json.loads(artifacts.GOLDEN.read_text(encoding="utf-8"))


def step(doc: Doc, step_id: str) -> Doc:
    return next(s for s in doc["steps"] if s["id"] == step_id)


# -- round trip -------------------------------------------------------------------


def test_the_golden_capability_round_trips_unchanged() -> None:
    cap = parse(golden())
    again = parse(json.loads(cap.to_json()))
    assert again == cap
    assert again.to_json() == artifacts.GOLDEN.read_text(encoding="utf-8")
    assert again.content_hash() == cap.content_hash()


def test_the_content_hash_ignores_bookkeeping_and_nothing_else() -> None:
    base = parse(golden()).content_hash()

    bookkeeping = golden() | {"version": 7, "approval_state": "approved", "approved_by": "x"}
    assert parse(bookkeeping).content_hash() == base

    changed = golden()
    step(changed, "search.submit")["target"][0]["name"] = "Find"
    assert parse(changed).content_hash() != base


# -- rejections ---------------------------------------------------------------------


def _unknown_action(doc: Doc) -> None:
    step(doc, "search.submit")["action"] = "hover"


def _unresolved_placeholder(doc: Doc) -> None:
    step(doc, "search.member_id")["value"] = "${member_number}"


def _duplicate_ids(doc: Doc) -> None:
    step(doc, "login.password")["id"] = "login.username"


def _retryable_irreversible(doc: Doc) -> None:
    s = step(doc, "search.submit")
    s["risk"] = "irreversible"
    s["retry"] = {"allowed": True}


def _bbox_without_recording_env(doc: Doc) -> None:
    del doc["recording_env"]


def _credential_outside_a_step_value(doc: Doc) -> None:
    doc["checkpoints"][0]["all_of"].append(
        {"kind": "text_present", "text": "${credentials.app_login.password}"}
    )


def _undeclared_credential_field(doc: Doc) -> None:
    step(doc, "login.password")["value"] = "${credentials.app_login.pin}"


def _checkpoint_after_unknown_step(doc: Doc) -> None:
    doc["checkpoints"][0]["after_step"] = "login.sign_on"


def _detector_scoped_to_unknown_step(doc: Doc) -> None:
    not_found = next(d for d in doc["outcome_detectors"] if d["code"] == "NOT_FOUND")
    not_found["scope"] = {"after_step": "search.go"}


def _business_outcome_not_in_contract(doc: Doc) -> None:
    del doc["contract"]["outcomes"]["NOT_FOUND"]


def _misspelt_field(doc: Doc) -> None:
    step(doc, "search.submit")["on_failure"] = "fail"


def _type_without_value(doc: Doc) -> None:
    del step(doc, "search.member_id")["value"]


REJECTIONS: dict[str, tuple[Callable[[Doc], None], str]] = {
    "unknown action": (_unknown_action, "steps.4.action: Input should be 'click'"),
    "unresolved placeholder": (_unresolved_placeholder, "${member_number} is not a declared input"),
    "duplicate step ids": (_duplicate_ids, "duplicate id 'login.username'"),
    "retryable irreversible step": (
        _retryable_irreversible,
        "retry.allowed is true on an irreversible step",
    ),
    "bbox without recording_env": (_bbox_without_recording_env, "a bbox rung needs recording_env"),
    "credential outside a step value": (
        _credential_outside_a_step_value,
        "a credential may only be typed as a step value",
    ),
    "undeclared credential field": (
        _undeclared_credential_field,
        "${credentials.app_login.pin} is not a declared credential field",
    ),
    "checkpoint after unknown step": (
        _checkpoint_after_unknown_step,
        "checkpoint cp.logged_in is after unknown step 'login.sign_on'",
    ),
    "detector scoped to unknown step": (
        _detector_scoped_to_unknown_step,
        "detector NOT_FOUND is scoped to unknown step 'search.go'",
    ),
    "business outcome not in contract": (
        _business_outcome_not_in_contract,
        "business detector NOT_FOUND is not a declared contract outcome",
    ),
    "misspelt field": (_misspelt_field, "on_failure: Extra inputs are not permitted"),
    "type without value": (_type_without_value, "a type step needs a value"),
}


@pytest.mark.parametrize("case", list(REJECTIONS))
def test_an_unsafe_or_malformed_capability_is_refused_with_a_clear_reason(case: str) -> None:
    breaker, message = REJECTIONS[case]
    doc = golden()
    breaker(doc)
    with pytest.raises(ArtifactError) as refused:
        parse(doc, source="bad.json")
    assert message in str(refused.value)
    assert str(refused.value).startswith("bad.json is not a valid capability:")


def test_several_problems_are_all_reported_at_once() -> None:
    doc = golden()
    _duplicate_ids(doc)
    _unresolved_placeholder(doc)
    with pytest.raises(ArtifactError) as refused:
        parse(doc)
    text = str(refused.value)
    assert "duplicate id" in text and "member_number" in text


# -- the exported JSON Schema ---------------------------------------------------------


def test_the_committed_json_schema_is_current() -> None:
    committed = json.loads((artifacts.REPO / SCHEMA_PATH).read_text(encoding="utf-8"))
    assert committed == json_schema(), "regenerate with: cua schema"


def test_the_json_schema_is_valid_and_accepts_the_golden_capability() -> None:
    schema = json_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(golden(), schema)


def test_the_json_schema_refuses_what_its_shape_can_express() -> None:
    schema = json_schema()
    for breaker in (_unknown_action, _misspelt_field):
        doc = copy.deepcopy(golden())
        breaker(doc)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)


def test_capability_is_the_model_the_schema_is_exported_from() -> None:
    assert json_schema()["title"] == Capability.__name__
