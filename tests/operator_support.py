"""Synthetic operator identities for console tests, with no real credentials."""

from pathlib import Path

from cua.escalation.access import OperatorConfig, OperatorGate

OPERATOR_KEY = "synthetic-operator-key-" + "a" * 32


def operator_gate(root: Path) -> OperatorGate:
    return OperatorGate(
        OperatorConfig.model_validate(
            {
                "version": 1,
                "operators": {
                    "ann": {
                        "key": {"provider": "env", "var": "ANN"},
                        "tenants": {"local": ["read", "operate", "approve"]},
                    },
                    "pat": {
                        "key": {"provider": "env", "var": "PAT"},
                        "tenants": {"local": ["read", "operate", "approve"]},
                    },
                    "reader": {
                        "key": {"provider": "env", "var": "READER"},
                        "tenants": {"local": ["read"]},
                    },
                    "worker": {
                        "key": {"provider": "env", "var": "WORKER"},
                        "tenants": {"local": ["read", "operate"]},
                    },
                    "beta": {
                        "key": {"provider": "env", "var": "BETA"},
                        "tenants": {"beta": ["read", "operate", "approve"]},
                    },
                },
            }
        ),
        root=root,
        environ={
            "ANN": OPERATOR_KEY,
            "PAT": OPERATOR_KEY + "p",
            "READER": OPERATOR_KEY + "r",
            "WORKER": OPERATOR_KEY + "w",
            "BETA": OPERATOR_KEY + "b",
        },
    )
