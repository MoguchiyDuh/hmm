import re
from pathlib import Path
from uuid import UUID

import pytest
from hmm_contracts import common
from hmm_contracts.common import (
    SEED_AREA_KEYS,
    AreaKey,
    Contract,
    ErrorCode,
    Page,
    Problem,
    UserId,
    ValidationIssue,
)
from pydantic import TypeAdapter, ValidationError

DOCS = Path(__file__).resolve().parents[2] / "docs"


def test_seed_area_keys_match_spec() -> None:
    expected = (
        "backend",
        "frontend",
        "mobile",
        "ml_ai",
        "data",
        "design",
        "product",
        "devops",
        "security",
        "gamedev",
        "hardware",
        "qa",
    )
    documented = tuple(
        re.findall(r"^\| `([a-z][a-z0-9_]*)` \|", (DOCS / "08-areas.md").read_text(), re.M)
    )
    adapter = TypeAdapter(AreaKey)
    assert SEED_AREA_KEYS == expected == documented
    assert tuple(adapter.validate_python(key) for key in SEED_AREA_KEYS) == expected
    assert adapter.validate_python("new_admin_area") == "new_admin_area"


@pytest.mark.parametrize("key", ["", "Backend", "1backend", "back-end", "a" * 33])
def test_area_key_rejects_invalid_slugs(key: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(AreaKey).validate_python(key)


def test_error_codes_are_unique_snake_case_and_sorted() -> None:
    values = [member.value for member in ErrorCode.__members__.values()]
    assert len(values) == len(set(values))
    assert values == sorted(values)
    assert all(re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", value) for value in values)


def test_error_codes_cover_documented_errors() -> None:
    documented: set[str] = set()
    for path in DOCS.glob("*.md"):
        text = path.read_text()
        documented.update(re.findall(r"\b[45]\d{2}\s+`([a-z][a-z0-9_]*)`", text))
        for group in re.findall(r"\b[45]\d{2}\s+codes:\s*([^\n]+)", text):
            documented.update(re.findall(r"`([a-z][a-z0-9_]*)`", group))
        documented.update(re.findall(r'"code":\s*"([a-z][a-z0-9_]*)"', text))
    assert documented
    assert documented <= {code.value for code in ErrorCode}


@pytest.mark.parametrize("public_url", ["https://hmm.example", "https://hmm.example///"])
def test_problem_builds_type_and_round_trips(public_url: str) -> None:
    issue = ValidationIssue(loc=["body", "items", 0], msg="Invalid item")
    problem = Problem.build(
        public_url=public_url,
        title="Invalid request",
        status=422,
        detail="An item is invalid",
        code=ErrorCode.VALIDATION_ERROR,
        errors=[issue],
    )
    assert problem.type == "https://hmm.example/errors/validation_error"
    assert Problem.model_validate_json(problem.model_dump_json()) == problem
    assert problem.model_dump(mode="json")["code"] == "validation_error"


def test_problem_defaults_are_independent() -> None:
    first = Problem.build(
        public_url="https://hmm.example", title="Not found", status=404, code=ErrorCode.NOT_FOUND
    )
    second = Problem.build(
        public_url="https://hmm.example", title="Not found", status=404, code=ErrorCode.NOT_FOUND
    )
    assert first.detail is None
    assert (
        Problem(type=first.type, title=first.title, status=first.status, code=first.code) == first
    )
    assert first.errors == []
    assert first.errors is not second.errors


def test_contract_forbids_extras() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Contract.model_validate({"unexpected": True})
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ValidationIssue.model_validate({"loc": [], "msg": "Invalid", "unexpected": True})


@pytest.mark.parametrize("attribute", ["loc", "msg"])
def test_contract_is_frozen(attribute: str) -> None:
    issue = ValidationIssue(loc=[], msg="Invalid")
    with pytest.raises(ValidationError, match="frozen_instance"):
        setattr(issue, attribute, "Changed")


def test_page_validates_typed_items() -> None:
    page = Page[ValidationIssue].model_validate(
        {"items": [{"loc": ["body", 0], "msg": "Invalid"}], "next_cursor": "opaque"}
    )
    assert page.items == [ValidationIssue(loc=["body", 0], msg="Invalid")]
    assert page.next_cursor == "opaque"
    assert Page[ValidationIssue].model_validate_json(page.model_dump_json()) == page
    assert Page[int](items=[], next_cursor=None).items == []
    with pytest.raises(ValidationError):
        Page[int].model_validate({"items": ["invalid"], "next_cursor": None})
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Page[int].model_validate({"items": [], "next_cursor": None, "total": 0})


class IdentifiedContract(Contract):
    user_id: UserId


def test_uuid_aliases_validate_and_generate_schemas() -> None:
    value = "019a78e5-9800-7000-8000-000000000001"
    model = IdentifiedContract.model_validate({"user_id": value})
    assert model.user_id == UUID(value)
    assert IdentifiedContract.model_validate_json(model.model_dump_json()) == model
    for name in common.__all__:
        alias = getattr(common, name)
        if name.endswith("Id") and name != "SourceId":
            adapter = TypeAdapter(alias)
            assert adapter.validate_python(value) == UUID(value)
            assert adapter.json_schema()["format"] == "uuid"


def test_public_exports_cover_local_definitions() -> None:
    assert len(common.__all__) == len(set(common.__all__))
    for name, value in vars(common).items():
        if getattr(value, "__module__", None) == common.__name__:
            assert name in common.__all__
