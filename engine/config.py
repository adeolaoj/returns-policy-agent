""" Loads and validates the rules for the engine. """

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml

# Any leap year works: it lets Feb 29 through while still rejecting Feb 30.
_LEAP_YEAR = 2024


class StandardRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    section: str = Field(pattern=r"^S\d+$")  # e.g. "S1", matching the policy doc
    description: str = Field(min_length=1)

class WindowRule(StandardRule):
    # strict: reject `days: true`, `days: "30"` and `days: 30.0` instead of coercing them.
    days: int = Field(gt=0, strict=True)

class ThresholdRule(StandardRule):
    # Money: at most two decimal places, so 500.005 is rejected.
    threshold: Decimal = Field(gt=0, decimal_places=2)

class MonthDay(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    month: int = Field(ge=1, le=12, strict=True)
    day: int = Field(ge=1, le=31, strict=True)

    @model_validator(mode="after")
    def check_real_calendar_day(self) -> Self:
        """Reject days that don't exist in that month, like Feb 30 or Apr 31."""
        try:
            date(_LEAP_YEAR, self.month, self.day)
        except ValueError:
            raise ValueError(f"{self.month}/{self.day} is not a real calendar day")
        return self

    def as_tuple(self) -> tuple[int, int]:
        return (self.month, self.day)

class HolidayRule(StandardRule):
    order_start: MonthDay
    order_end: MonthDay
    deadline: MonthDay

    @model_validator(mode="after")
    def check_order_window(self) -> Self:
        """The order window runs forward within one year: start can't be after end."""
        if self.order_start.as_tuple() > self.order_end.as_tuple():
            raise ValueError("order_start must be on or before order_end")
        return self

class Rules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    standard_window: WindowRule
    loyalty_window: WindowRule
    holiday_extension: HolidayRule
    final_sale: StandardRule
    defective_window: WindowRule
    refund_method: StandardRule
    condition: StandardRule
    human_approval: ThresholdRule
    exchange: StandardRule

    @model_validator(mode="after")
    def check_unique_sections(self) -> Self:
        """Two rules claiming the same section is almost always a copy-paste error."""
        seen: dict[str, str] = {}
        for name in type(self).model_fields:
            section = getattr(self, name).section
            if section in seen:
                raise ValueError(f"{name} and {seen[section]} both use section {section}")
            seen[section] = name
        return self


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")  # e.g. "1.0.0"
    effective_date: date
    rules: Rules


class _UniqueKeyLoader(yaml.SafeLoader):
    """A SafeLoader that raises on duplicate keys instead of keeping the last one."""

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    f"found duplicate key {key!r}", key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_policy(path: Path) -> PolicyConfig:
    """Load and validate the policy config from a YAML file."""
    with open(path, "r", encoding="utf-8") as file:
        data = yaml.load(file, Loader=_UniqueKeyLoader)

    if not isinstance(data, dict):
        raise ValueError("Policy config is badly formatted. Expected a dictionary at the top level.")

    return PolicyConfig.model_validate(data)
