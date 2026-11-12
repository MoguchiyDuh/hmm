import re
from pathlib import Path

import pytest
from hmm_core.api.schemas.admin import JobName
from hmm_core.api.schemas.projects import CreateProjectBody
from hmm_core.app import create_app
from pydantic import ValidationError

DOCS = Path(__file__).resolve().parents[2] / "docs"

PROJECT = {
    "name": "  hmm  ",
    "description_md": "",
    "goal": "Ship it",
    "area_keys": ["backend"],
    "links": [],
}


def test_job_names_match_documented_jobs() -> None:
    documented = re.findall(r"^\| `([a-z_]+)", (DOCS / "17-jobs.md").read_text(), re.M)
    assert documented == [job.value for job in JobName]


def test_text_inputs_are_trimmed_and_bounded() -> None:
    assert CreateProjectBody.model_validate(PROJECT).name == "hmm"
    for change in (
        {"name": "   "},
        {"name": "x" * 101},
        {"goal": ""},
        {"description_md": "x" * 5001},
        {"links": [f"https://example.com/{index}" for index in range(11)]},
    ):
        with pytest.raises(ValidationError):
            CreateProjectBody.model_validate(PROJECT | change)


def test_allowed_actions_are_typed_enums() -> None:
    schemas = create_app().openapi()["components"]["schemas"]
    typed = {
        name: schema["properties"]["allowed_actions"]["items"]
        for name, schema in schemas.items()
        if "allowed_actions" in schema.get("properties", {})
    }
    assert typed
    for name, items in typed.items():
        action = items.get("$ref", "").removeprefix("#/components/schemas/")
        assert action.endswith("Action"), name
        assert schemas[action]["enum"], name
