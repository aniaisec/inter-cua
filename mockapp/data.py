"""Seed data for the mock legacy credit-union core.

Everything here is synthetic. Member ids are 10001-10010 and names are
"Test Member NN" so that no screenshot or log in ``evidence/`` can ever be
mistaken for real PII.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

# The only login the mock app accepts. Replay resolves it through
# secret://<tenant>/mockcore/operator; it is never written to an artifact.
OPERATOR_USERNAME = "operator"
OPERATOR_PASSWORD = "operator"


@dataclass(frozen=True)
class Member:
    member_id: str
    name: str
    savings: Decimal
    checking: Decimal
    branch: str
    # 10007 is flagged restricted so the permission-denied path exists in the
    # data, not only behind an inject flag.
    restricted: bool = False
    subaccounts: list[str] = field(default_factory=list)

    def balance(self, account: str) -> Decimal:
        return {"Savings": self.savings, "Checking": self.checking}[account]


def _money(dollars: int, cents: int) -> Decimal:
    return Decimal(f"{dollars}.{cents:02d}")


BRANCHES = ["Northgate", "Ballard", "Rainier", "Fremont", "Lynnwood"]


def _seed() -> dict[str, Member]:
    members: dict[str, Member] = {}
    for i in range(1, 11):
        member_id = str(10000 + i)
        members[member_id] = Member(
            member_id=member_id,
            name=f"Test Member {i:02d}",
            savings=_money(1000 + i * 137, (i * 7) % 100),
            checking=_money(200 + i * 41, (i * 13) % 100),
            branch=BRANCHES[i % len(BRANCHES)],
            restricted=(member_id == "10007"),
        )
    return members


MEMBERS: dict[str, Member] = _seed()

ACCOUNT_TYPES = ["Savings", "Checking", "Money Market"]


def find_member(member_id: str) -> Member | None:
    return MEMBERS.get(member_id.strip())


def format_currency(value: Decimal) -> str:
    return f"${value:,.2f}"


def reference_number(member_id: str, seq: int) -> str:
    """Deterministic reference number, so evidence diffs stay readable."""
    return f"REF-{member_id}-{seq:04d}"
