import textwrap
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError
import pytest
import yaml

from engine.config import PolicyConfig, load_policy

REAL_CONFIG = Path(__file__).parent.parent / "policy" / "rules.yaml"


def write_config(tmp_path: Path, text: str) -> Path:
    """Write YAML text to a temporary rules.yaml and return its path."""
    path = tmp_path / "rules.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


@pytest.fixture
def real_data() -> dict:
    """A fresh copy of the real rules.yaml as a dict, safe to modify per test."""
    return yaml.safe_load(REAL_CONFIG.read_text(encoding="utf-8"))


def write_data(tmp_path: Path, data: dict) -> Path:
    """Dump a (modified) config dict to a temporary rules.yaml and return its path."""
    return write_config(tmp_path, yaml.safe_dump(data))


def set_at(data: dict, keys: tuple, value) -> None:
    """Set a nested value, e.g. set_at(data, ("rules", "standard_window", "days"), 45)."""
    *parents, last = keys
    for key in parents:
        data = data[key]
    data[last] = value


def error_set(exc: ValidationError) -> set[tuple[tuple, str]]:
    """Every (location, error type) pair in a ValidationError."""
    return {(err["loc"], err["type"]) for err in exc.errors()}


# -- Test the real, valid config file
def test_load_real_config():
    """Test that the real rules.yaml file can be loaded and validated."""
    config = load_policy(REAL_CONFIG)
    assert isinstance(config, PolicyConfig)
    assert config.policy_version == "1.0.0"
    assert config.effective_date.isoformat() == "2026-01-01"
    assert config.rules.standard_window.days == 30
    assert config.rules.holiday_extension.deadline.month == 1
    assert config.rules.holiday_extension.deadline.day == 31
    assert config.rules.human_approval.threshold == Decimal("500.00")


def test_feb_29_allowed(tmp_path: Path, real_data: dict):
    """Feb 29 is a real day in leap years, so it must not be rejected."""
    set_at(real_data, ("rules", "holiday_extension", "deadline"), {"month": 2, "day": 29})
    config = load_policy(write_data(tmp_path, real_data))
    assert config.rules.holiday_extension.deadline.day == 29


# -- Invalid values: the real file with exactly one value broken.
# Each case asserts the *only* error is the expected one, so a test can't pass by accident.
HOLIDAY = ("rules", "holiday_extension")

INVALID_VALUES = [
    # (id, keys to change, new value, expected error location, expected error type)
    ("negative days", ("rules", "standard_window", "days"), -30,
     ("rules", "standard_window", "days"), "greater_than"),
    ("zero days", ("rules", "standard_window", "days"), 0,
     ("rules", "standard_window", "days"), "greater_than"),
    ("bool days", ("rules", "standard_window", "days"), True,
     ("rules", "standard_window", "days"), "int_type"),
    ("string days", ("rules", "standard_window", "days"), "30",
     ("rules", "standard_window", "days"), "int_type"),
    ("float days", ("rules", "standard_window", "days"), 30.0,
     ("rules", "standard_window", "days"), "int_type"),
    ("negative threshold", ("rules", "human_approval", "threshold"), -100.00,
     ("rules", "human_approval", "threshold"), "greater_than"),
    ("sub-cent threshold", ("rules", "human_approval", "threshold"), 500.005,
     ("rules", "human_approval", "threshold"), "decimal_max_places"),
    ("bad version", ("policy_version",), "v1",
     ("policy_version",), "string_pattern_mismatch"),
    ("bad section", ("rules", "final_sale", "section"), "Section 4",
     ("rules", "final_sale", "section"), "string_pattern_mismatch"),
    ("empty description", ("rules", "final_sale", "description"), "",
     ("rules", "final_sale", "description"), "string_too_short"),
    ("month 13", HOLIDAY + ("deadline", "month"), 13,
     HOLIDAY + ("deadline", "month"), "less_than_equal"),
    ("feb 30", HOLIDAY + ("deadline",), {"month": 2, "day": 30},
     HOLIDAY + ("deadline",), "value_error"),
    ("apr 31", HOLIDAY + ("deadline",), {"month": 4, "day": 31},
     HOLIDAY + ("deadline",), "value_error"),
    ("duplicate section", ("rules", "loyalty_window", "section"), "S1",
     ("rules",), "value_error"),
]


@pytest.mark.parametrize(
    "keys, value, loc, error_type",
    [case[1:] for case in INVALID_VALUES],
    ids=[case[0] for case in INVALID_VALUES],
)
def test_invalid_value_rejected(tmp_path, real_data, keys, value, loc, error_type):
    set_at(real_data, keys, value)
    path = write_data(tmp_path, real_data)

    with pytest.raises(ValidationError) as exc_info:
        load_policy(path)

    assert error_set(exc_info.value) == {(loc, error_type)}


def test_backwards_order_window_rejected(tmp_path: Path, real_data: dict):
    """An order window that starts after it ends (12/31 to 11/15) is rejected."""
    set_at(real_data, HOLIDAY + ("order_start",), {"month": 12, "day": 31})
    set_at(real_data, HOLIDAY + ("order_end",), {"month": 11, "day": 15})
    path = write_data(tmp_path, real_data)

    with pytest.raises(ValidationError, match="order_start must be on or before order_end") as exc_info:
        load_policy(path)

    assert error_set(exc_info.value) == {(HOLIDAY, "value_error")}


# -- Typos: caught by extra="forbid" instead of silently ignored
def test_misspelled_field_rejected(tmp_path: Path, real_data: dict):
    """A typo in a field name ('dayz') is reported."""
    window = real_data["rules"]["standard_window"]
    window["dayz"] = window.pop("days")
    path = write_data(tmp_path, real_data)

    with pytest.raises(ValidationError) as exc_info:
        load_policy(path)

    assert error_set(exc_info.value) == {
        (("rules", "standard_window", "dayz"), "extra_forbidden"),
        (("rules", "standard_window", "days"), "missing"),
    }


def test_misspelled_rule_name_rejected(tmp_path: Path, real_data: dict):
    """A typo in a rule name ('standard_windw') is reported."""
    rules = real_data["rules"]
    rules["standard_windw"] = rules.pop("standard_window")
    path = write_data(tmp_path, real_data)

    with pytest.raises(ValidationError) as exc_info:
        load_policy(path)

    assert error_set(exc_info.value) == {
        (("rules", "standard_windw"), "extra_forbidden"),
        (("rules", "standard_window"), "missing"),
    }


# -- Duplicate YAML keys: plain yaml.safe_load would silently keep the last one.
# These edit the raw text, since a Python dict can't hold a duplicate key.
@pytest.mark.parametrize(
    "old, new, duplicate",
    [
        ("    days: 30\n", "    days: 30\n    days: 45\n", "days"),
        ("rules:\n", 'rules:\n  standard_window:\n    section: "S1"\n'
                     "    description: Copy-pasted block.\n    days: 45\n", "standard_window"),
    ],
    ids=["duplicate field", "duplicate rule"],
)
def test_duplicate_key_rejected(tmp_path: Path, old: str, new: str, duplicate: str):
    text = REAL_CONFIG.read_text(encoding="utf-8")
    assert old in text  # guard: fail loudly if rules.yaml's layout changes
    path = write_config(tmp_path, text.replace(old, new, 1))

    with pytest.raises(yaml.YAMLError, match=f"duplicate key '{duplicate}'"):
        load_policy(path)


# -- Badly shaped files: rejected by load_policy's own check
@pytest.mark.parametrize(
    "text",
    [
        "- item1\n- item2\n",  # top level is a list
        "",                    # empty file loads as None
    ],
    ids=["list", "empty"],
)
def test_non_dict_file_rejected(tmp_path: Path, text: str):
    """A file whose top level isn't a mapping raises load_policy's ValueError."""
    path = write_config(tmp_path, text)

    with pytest.raises(ValueError, match="Expected a dictionary at the top level"):
        load_policy(path)
