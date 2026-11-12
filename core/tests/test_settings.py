import re
from pathlib import Path

import pytest
from hmm_core.settings import (
    RuntimeSettings,
    SettingKey,
    setting_value_schema,
    validate_setting_value,
)
from pydantic import ValidationError


def test_settings_match_documented_defaults() -> None:
    path = Path(__file__).resolve().parents[2] / "docs/24-defaults.md"
    spec = path.read_text()
    defaults = {}
    for key, default in re.findall(r"^\| `([a-z_]+)` \| ([^|]+) \|", spec, re.M):
        value = default.split("(", 1)[0].strip().replace(" ", "")
        defaults[key] = (
            [int(item) for item in value.split(",")]
            if "," in value
            else float(value)
            if "." in value
            else int(value)
        )
    sizes = re.search(r"`team_size_default_min` / `max` \| (\d+) / (\d+)", spec)
    assert sizes is not None
    defaults.update(team_size_default_min=int(sizes[1]), team_size_default_max=int(sizes[2]))
    assert RuntimeSettings().model_dump() == defaults
    assert list(SettingKey) == list(RuntimeSettings.model_fields)
    assert all(field.description for field in RuntimeSettings.model_fields.values())
    assert all(setting_value_schema(key) for key in SettingKey)


def test_setting_value_validation_preserves_constraints() -> None:
    key = SettingKey("project_size_max")
    assert validate_setting_value(key, 100) == 100
    for value in (0, -1, "20", True, None):
        with pytest.raises(ValidationError):
            validate_setting_value(key, value)
    with pytest.raises(ValidationError):
        validate_setting_value(SettingKey("lead_fit_threshold"), 1.1)
    with pytest.raises(ValidationError):
        validate_setting_value(SettingKey("confirm_reminder_hours"), [24, "6", 1])
    assert setting_value_schema(key)["minimum"] == 1
