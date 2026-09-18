"""``secret://`` references, resolved into memory and nowhere else."""

from cua.secrets.resolver import Credential, SecretError, parse_ref, resolve

__all__ = ["Credential", "SecretError", "parse_ref", "resolve"]
