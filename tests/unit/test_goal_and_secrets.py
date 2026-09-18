"""Declared params, outputs and credentials; secret:// resolution; tenants."""

from __future__ import annotations

from pathlib import Path

import pytest

from cua.agent.goal import SpecError, parse_credential, parse_output, parse_param
from cua.secrets.resolver import SecretError, parse_ref, resolve
from cua.tenant import load_tenant

REPO = Path(__file__).resolve().parents[2]


def test_a_param_is_name_type_and_value() -> None:
    p = parse_param("member_id:string=10003")
    assert (p.name, p.type, p.value) == ("member_id", "string", "10003")
    assert parse_param("member_id=10003").type == "string"


@pytest.mark.parametrize("bad", ["member_id", "Member=1", "member_id:money=1"])
def test_a_malformed_param_is_refused(bad: str) -> None:
    with pytest.raises(SpecError):
        parse_param(bad)


def test_an_output_may_be_optional_and_described() -> None:
    o = parse_output("member_name:string?=Name as printed on the detail screen")
    assert (o.name, o.type, o.optional) == ("member_name", "string", True)
    assert o.description == "Name as printed on the detail screen"
    assert parse_output("savings_balance:decimal").optional is False


def test_a_credential_names_a_secret_ref() -> None:
    assert parse_credential("app_login=secret://local/mockcore/operator") == (
        "app_login",
        "secret://local/mockcore/operator",
    )


# -- secrets -----------------------------------------------------------------

TENANT = load_tenant("local", root=REPO / "tenants")
REF = "secret://local/mockcore/operator"


def test_the_local_tenant_binds_the_mock_operator_secret() -> None:
    assert TENANT.base_url == "http://127.0.0.1:8000"
    assert TENANT.url("/login") == "http://127.0.0.1:8000/login"
    assert "mockcore/operator" in TENANT.secrets


def test_a_ref_resolves_through_the_tenant_into_fields() -> None:
    cred = resolve(REF, TENANT, environ={"CUA_SECRET_MOCKCORE_OPERATOR": "op:pa:ss"})
    assert cred.field("username") == "op"
    assert cred.field("password") == "pa:ss"  # only the first colon splits


def test_a_credential_never_shows_its_values() -> None:
    cred = resolve(REF, TENANT, environ={"CUA_SECRET_MOCKCORE_OPERATOR": "op:hunter2"})
    assert "hunter2" not in repr(cred)
    assert "hunter2" not in str(cred)


@pytest.mark.parametrize(
    ("ref", "environ", "complaint"),
    [
        ("env:CUA_X", {}, "not a secret reference"),
        ("secret://other/mockcore/operator", {}, "belongs to tenant"),
        ("secret://local/nope", {}, "binds no secret"),
        (REF, {}, "is not set"),
        (REF, {"CUA_SECRET_MOCKCORE_OPERATOR": "no-colon"}, "does not have the shape"),
    ],
)
def test_an_unresolvable_ref_says_why_without_a_value(
    ref: str, environ: dict[str, str], complaint: str
) -> None:
    with pytest.raises(SecretError, match=complaint) as err:
        resolve(ref, TENANT, environ=environ)
    assert "no-colon" not in str(err.value)


def test_parse_ref_splits_tenant_and_key() -> None:
    assert parse_ref(REF) == ("local", "mockcore/operator")
