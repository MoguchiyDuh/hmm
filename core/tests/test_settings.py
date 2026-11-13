import re
from pathlib import Path

import pytest
from hmm_core.platform.settings import (
    RuntimeSettings,
    SettingKey,
    apply_setting,
    setting_value_schema,
)
from pydantic import JsonValue, ValidationError


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
    settings = RuntimeSettings()
    key = SettingKey("project_size_max")
    assert apply_setting(settings, key, 100).project_size_max == 100
    for value in (0, -1, "20", True, None):
        with pytest.raises(ValidationError):
            apply_setting(settings, key, value)
    with pytest.raises(ValidationError):
        apply_setting(settings, SettingKey("lead_fit_threshold"), 1.1)
    with pytest.raises(ValidationError):
        apply_setting(settings, SettingKey("confirm_reminder_hours"), [24, "6", 1])
    assert setting_value_schema(key)["minimum"] == 1


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("team_size_default_min", 9),
        ("team_size_default_max", 1),
        ("formation_min_notice_hours", 100),
        ("review_min_hours", 49),
        ("presence_confirm_hours", 48),
        ("results_due_days", 30),
        ("confirm_reminder_hours", [1, 1, 24]),
        ("confirm_reminder_hours", [1, 6, 24]),
        ("confirm_reminder_hours", []),
    ],
)
def test_setting_rejects_inconsistent_combinations(key: str, value: JsonValue) -> None:
    with pytest.raises(ValidationError):
        apply_setting(RuntimeSettings(), SettingKey(key), value)


def test_setting_accepts_consistent_related_changes() -> None:
    settings = apply_setting(RuntimeSettings(), SettingKey("team_size_default_max"), 9)
    settings = apply_setting(settings, SettingKey("team_size_default_min"), 9)
    assert (settings.team_size_default_min, settings.team_size_default_max) == (9, 9)
    assert RuntimeSettings().team_size_default_min == 2
