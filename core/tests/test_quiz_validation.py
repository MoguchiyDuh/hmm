from datetime import UTC, datetime

import pytest
from hmm_contracts.algo import (
    QuizQuestion,
    QuizSection,
    VisibilityCondition,
    VisibilityPredicate,
)
from hmm_contracts.common import ValidationIssue
from hmm_core import models
from hmm_core.onboarding.validation import _contradicts, _unsatisfiable, validate_definition
from pydantic import TypeAdapter

DEFINITION = {
    "title": "Onboarding",
    "sections": [
        {"key": "general", "position": 0, "visible_if": None},
        {
            "key": "advanced",
            "position": 1,
            "visible_if": {
                "any": [
                    {"q": "languages", "op": "includes", "value": "python"},
                    {"q": "years", "op": "gte", "value": 2},
                ]
            },
        },
    ],
    "questions": [
        {
            "id": "019b0000-0000-7000-8000-000000000001",
            "section": "general",
            "key": "languages",
            "type": "multi_choice",
            "text": "Languages",
            "help": None,
            "required": True,
            "position": 0,
            "visible_if": None,
            "config": {"min": 0, "max": 5, "allow_other": True},
            "options": [
                {
                    "id": "019b0000-0000-7000-8000-000000000002",
                    "key": "python",
                    "label": "Python",
                    "position": 0,
                    "area_hints": {"backend": 1.0},
                },
                {
                    "id": "019b0000-0000-7000-8000-000000000003",
                    "key": "react",
                    "label": "React",
                    "position": 1,
                    "area_hints": {"frontend": 0.8},
                },
            ],
        },
        {
            "id": "019b0000-0000-7000-8000-000000000004",
            "section": "general",
            "key": "years",
            "type": "number",
            "text": "Years",
            "help": "Total years",
            "required": False,
            "position": 1,
            "visible_if": None,
            "config": {"min": 0, "max": 50, "unit": "years"},
            "options": [],
        },
        {
            "id": "019b0000-0000-7000-8000-000000000005",
            "section": "general",
            "key": "direction",
            "type": "single_choice",
            "text": "Direction",
            "help": None,
            "required": True,
            "position": 2,
            "visible_if": {"q": "languages", "op": "includes", "value": "python"},
            "config": {"allow_other": False},
            "options": [
                {
                    "id": "019b0000-0000-7000-8000-000000000006",
                    "key": "backend",
                    "label": "Backend",
                    "position": 0,
                    "area_hints": {"backend": 1.0},
                },
                {
                    "id": "019b0000-0000-7000-8000-000000000007",
                    "key": "frontend",
                    "label": "Frontend",
                    "position": 1,
                    "area_hints": {"frontend": 1.0},
                },
            ],
        },
        {
            "id": "019b0000-0000-7000-8000-000000000008",
            "section": "general",
            "key": "rating",
            "type": "scale",
            "text": "Self rating",
            "help": None,
            "required": False,
            "position": 3,
            "visible_if": None,
            "config": {"min": 1, "max": 10, "min_label": "new", "max_label": "expert"},
            "options": [],
        },
        {
            "id": "019b0000-0000-7000-8000-000000000009",
            "section": "advanced",
            "key": "about",
            "type": "text",
            "text": "About",
            "help": None,
            "required": False,
            "position": 4,
            "visible_if": {"not": {"q": "years", "op": "gte", "value": 2}},
            "config": {"max_length": 2000, "multiline": True},
            "options": [],
        },
        {
            "id": "019b0000-0000-7000-8000-000000000010",
            "section": "advanced",
            "key": "link",
            "type": "url",
            "text": "Link",
            "help": None,
            "required": False,
            "position": 5,
            "visible_if": None,
            "config": {},
            "options": [],
        },
    ],
}
SECTIONS = [QuizSection.model_validate(section) for section in DEFINITION["sections"]]
QUESTIONS = TypeAdapter(list[QuizQuestion]).validate_python(DEFINITION["questions"])
CONDITION = TypeAdapter(VisibilityCondition)
AREAS = [
    models.Area(key=key, name=key.title(), description=key, sort=i, archived_at=None)
    for i, key in enumerate(("backend", "frontend"))
]


def with_condition(index: int, condition: object) -> list[QuizQuestion]:
    questions = list(QUESTIONS)
    questions[index] = questions[index].model_copy(
        update={"visible_if": CONDITION.validate_python(condition)}
    )
    return questions


def test_canonical_definition() -> None:
    result = validate_definition(SECTIONS, QUESTIONS, AREAS)
    assert result.errors == []
    assert result.warnings == []


@pytest.mark.parametrize(
    ("index", "condition", "message"),
    [
        (5, {"q": "nope", "op": "answered"}, "Question nope does not exist"),
        (1, {"q": "rating", "op": "eq", "value": 1}, "Question rating is not positioned earlier"),
        (2, {"q": "languages", "op": "includes", "value": "rust"}, "Option rust does not exist"),
        (
            5,
            {"q": "years", "op": "includes", "value": "python"},
            "Operator includes is not valid for number",
        ),
        (5, {"q": "rating", "op": "eq", "value": "high"}, "Value must be an integer"),
        (5, {"q": "years", "op": "answered", "value": 2}, "Operator does not take a value"),
        (5, {"q": "years", "op": "not_answered", "value": 2}, "Operator does not take a value"),
        (5, {"q": "languages", "op": "includes", "value": 1}, "Value must be an option key"),
        (5, {"q": "direction", "op": "eq", "value": "mobile"}, "Option mobile does not exist"),
        (5, {"q": "direction", "op": "neq", "value": 1}, "Value must be an option key"),
        (
            5,
            {"q": "languages", "op": "includes_any", "value": []},
            "Value must be a non-empty list of option keys",
        ),
        (
            5,
            {"q": "languages", "op": "includes_any", "value": "python"},
            "Value must be a non-empty list of option keys",
        ),
        (
            5,
            {"q": "languages", "op": "includes_any", "value": ["rust"]},
            "Option rust does not exist",
        ),
        (5, {"q": "about", "op": "eq", "value": 2}, "Value must be a string"),
        (5, {"q": "rating", "op": "lte", "value": 2.5}, "Value must be an integer"),
        (5, {"q": "years", "op": "neq", "value": "2"}, "Value must be a number"),
        (5, {"q": "years", "op": "lte", "value": "2"}, "Value must be a number"),
    ],
)
def test_predicate_errors(index: int, condition: object, message: str) -> None:
    result = validate_definition(SECTIONS, with_condition(index, condition), AREAS)
    assert result.errors == [ValidationIssue(loc=["questions", index, "visible_if"], msg=message)]
    assert result.warnings == []


@pytest.mark.parametrize("combinators", [4, 5])
def test_condition_depth(combinators: int) -> None:
    condition: object = {"q": "years", "op": "answered"}
    for _ in range(combinators):
        condition = {"not": condition}
    result = validate_definition(SECTIONS, with_condition(5, condition), AREAS)
    assert result.errors == (
        [
            ValidationIssue(
                loc=["questions", 5, "visible_if"], msg="Visibility condition exceeds depth 5"
            )
        ]
        if combinators == 5
        else []
    )


@pytest.mark.parametrize(
    ("key", "operator", "message"),
    [
        ("rating", "gte", "Value must be an integer"),
        ("years", "eq", "Value must be a number"),
        ("years", "gte", "Value must be a number"),
    ],
)
def test_boolean_is_not_numeric(key: str, operator: str, message: str) -> None:
    pred = VisibilityPredicate.model_validate({"q": key, "op": operator}).model_copy(
        update={"value": True}
    )
    result = validate_definition(SECTIONS, with_condition(5, pred), AREAS)
    assert result.errors == [ValidationIssue(loc=["questions", 5, "visible_if"], msg=message)]


@pytest.mark.parametrize(
    ("kind", "loc", "message"),
    [
        ("section_key", ["sections", 2, "key"], "Section key is duplicated"),
        ("section_position", ["sections", 1, "position"], "Section position is duplicated"),
        ("question_key", ["questions", 5, "key"], "Question key is duplicated"),
        ("question_position", ["questions", 5, "position"], "Question position is duplicated"),
        ("section_missing", ["questions", 5, "section"], "Section does not exist"),
        ("option_key", ["questions", 0, "options", 2, "key"], "Option key is duplicated"),
    ],
)
def test_structural_errors(kind: str, loc: list[str | int], message: str) -> None:
    sections = list(SECTIONS)
    questions = list(QUESTIONS)
    match kind:
        case "section_key":
            sections.append(sections[0].model_copy(update={"position": 2}))
        case "section_position":
            sections[1] = sections[1].model_copy(update={"position": 0})
        case "question_key":
            questions[5] = questions[5].model_copy(update={"key": "about"})
        case "question_position":
            questions[5] = questions[5].model_copy(update={"position": 4})
        case "section_missing":
            questions[5] = questions[5].model_copy(update={"section": "missing"})
        case "option_key":
            questions[0] = questions[0].model_copy(
                update={"options": [*questions[0].options, questions[0].options[0]]}
            )
    result = validate_definition(sections, questions, AREAS)
    assert result.errors == [ValidationIssue(loc=loc, msg=message)]


@pytest.mark.parametrize("archived", [False, True])
def test_unknown_or_archived_area(archived: bool) -> None:
    questions = list(QUESTIONS)
    options = list(questions[0].options)
    options[0] = options[0].model_copy(update={"area_hints": {"backend": 1.0, "mobile": 0.5}})
    questions[0] = questions[0].model_copy(update={"options": options})
    areas = list(AREAS)
    if archived:
        areas.append(
            models.Area(
                key="mobile",
                name="Mobile",
                description="Mobile",
                sort=2,
                archived_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
    result = validate_definition(SECTIONS, questions, areas)
    assert result.errors == [
        ValidationIssue(
            loc=["questions", 0, "options", 0, "area_hints", "mobile"],
            msg="Area mobile is archived" if archived else "Area mobile does not exist",
        )
    ]
    assert result.warnings == []


@pytest.mark.parametrize(
    ("required", "contradictory"), [(True, True), (False, True), (True, False)]
)
def test_required_question_warning(required: bool, contradictory: bool) -> None:
    condition = {
        "all": [
            {"q": "languages", "op": "includes", "value": "python"},
            (
                {"q": "languages", "op": "excludes", "value": "python"}
                if contradictory
                else {"q": "years", "op": "gte", "value": 2}
            ),
        ]
    }
    questions = with_condition(5, condition)
    questions[5] = questions[5].model_copy(update={"required": required})
    result = validate_definition(SECTIONS, questions, AREAS)
    assert result.errors == []
    assert result.warnings == (
        [
            ValidationIssue(
                loc=["questions", 5, "visible_if"],
                msg="Required question can never become visible",
            )
        ]
        if required and contradictory
        else []
    )


def test_area_coverage_ignores_archived_areas() -> None:
    questions = [
        question.model_copy(
            update={
                "options": [
                    option.model_copy(
                        update={
                            "area_hints": {
                                key: value
                                for key, value in option.area_hints.items()
                                if key != "backend"
                            }
                        }
                    )
                    for option in question.options
                ]
            }
        )
        for question in QUESTIONS
    ]
    areas = [
        *AREAS,
        models.Area(
            key="mobile",
            name="Mobile",
            description="Mobile",
            sort=-1,
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    ]
    result = validate_definition(SECTIONS, questions, areas)
    assert result.errors == []
    assert result.warnings == [
        ValidationIssue(loc=["area_hints", "backend"], msg="No option has area hints for backend")
    ]


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ({"op": "answered"}, {"op": "not_answered"}),
        ({"op": "eq", "value": "a"}, {"op": "eq", "value": "b"}),
        ({"op": "eq", "value": 1}, {"op": "neq", "value": 1.0}),
        ({"op": "includes", "value": "a"}, {"op": "excludes", "value": "a"}),
        ({"op": "includes", "value": "a"}, {"op": "includes_any", "value": ["b"]}),
        ({"op": "includes_any", "value": ["a"]}, {"op": "includes_any", "value": ["b"]}),
        ({"op": "gte", "value": 3}, {"op": "lte", "value": 2}),
        ({"op": "eq", "value": 1}, {"op": "gte", "value": 2}),
        ({"op": "eq", "value": 3}, {"op": "lte", "value": 2}),
        ({"op": "excludes", "value": "a"}, {"op": "includes_any", "value": ["a", "a"]}),
    ],
)
def test_contradiction_rules_are_symmetric(
    left: dict[str, object], right: dict[str, object]
) -> None:
    a = VisibilityPredicate.model_validate({"q": "target", **left})
    b = VisibilityPredicate.model_validate({"q": "target", **right})
    assert _contradicts(a, b)
    assert _contradicts(b, a)
    assert _unsatisfiable(CONDITION.validate_python({"all": [a, {"all": [b]}]}))


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ({"op": "eq", "value": "1"}, {"op": "eq", "value": 1}),
        ({"op": "eq", "value": True}, {"op": "neq", "value": 1}),
        ({"op": "eq", "value": 1}, {"op": "eq", "value": 1.0}),
        ({"op": "gte", "value": 2}, {"op": "lte", "value": 2}),
        ({"op": "eq", "value": 2}, {"op": "gte", "value": 2}),
        ({"op": "eq", "value": 2}, {"op": "lte", "value": 2}),
        ({"op": "includes", "value": "a"}, {"op": "includes_any", "value": ["a", "b"]}),
        ({"op": "includes_any", "value": ["a"]}, {"op": "includes_any", "value": ["a", "b"]}),
        ({"op": "excludes", "value": "a"}, {"op": "includes_any", "value": ["a", "b"]}),
    ],
)
def test_compatible_or_mismatched_values_do_not_contradict(
    left: dict[str, object], right: dict[str, object]
) -> None:
    a = VisibilityPredicate.model_validate({"q": "target", **left}).model_copy(
        update={"value": left.get("value")}
    )
    b = VisibilityPredicate.model_validate({"q": "target", **right}).model_copy(
        update={"value": right.get("value")}
    )
    assert not _contradicts(a, b)
    assert not _contradicts(b, a)


@pytest.mark.parametrize(
    ("condition", "unsatisfiable"),
    [
        ({"any": []}, True),
        ({"all": []}, False),
        ({"all": [{"any": []}]}, True),
        ({"any": [{"any": []}, {"all": [{"any": []}]}]}, True),
        ({"any": [{"any": []}, {"q": "years", "op": "answered"}]}, False),
        ({"not": {"any": []}}, False),
        (
            {
                "all": [
                    {"q": "years", "op": "answered"},
                    {"any": [{"q": "years", "op": "not_answered"}]},
                ]
            },
            False,
        ),
        (
            {
                "all": [
                    {"q": "years", "op": "answered"},
                    {"not": {"q": "years", "op": "answered"}},
                ]
            },
            False,
        ),
    ],
)
def test_conservative_unsatisfiability(condition: object, unsatisfiable: bool) -> None:
    assert _unsatisfiable(CONDITION.validate_python(condition)) is unsatisfiable


@pytest.mark.parametrize("own_condition", [None, {"q": "years", "op": "not_answered"}])
def test_required_question_uses_section_condition(own_condition: object) -> None:
    sections = list(SECTIONS)
    section_condition = {"any": []} if own_condition is None else {"q": "years", "op": "answered"}
    sections[1] = sections[1].model_copy(
        update={"visible_if": CONDITION.validate_python(section_condition)}
    )
    questions = list(QUESTIONS) if own_condition is None else with_condition(5, own_condition)
    questions[5] = questions[5].model_copy(update={"required": True})
    result = validate_definition(sections, questions, AREAS)
    assert result.errors == []
    assert result.warnings == [
        ValidationIssue(
            loc=["questions", 5, "visible_if"], msg="Required question can never become visible"
        )
    ]


def test_section_barrier_and_empty_section() -> None:
    condition = CONDITION.validate_python({"q": "about", "op": "answered"})
    sections = [
        SECTIONS[0],
        SECTIONS[1].model_copy(update={"visible_if": condition}),
        QuizSection.model_validate({"key": "empty", "position": 2, "visible_if": condition}),
    ]
    result = validate_definition(sections, QUESTIONS, AREAS)
    assert result.errors == [
        ValidationIssue(
            loc=["sections", 1, "visible_if"], msg="Question about is not positioned earlier"
        )
    ]


def test_issue_order_and_first_question_reference() -> None:
    sections = [
        *SECTIONS,
        SECTIONS[0].model_copy(
            update={"visible_if": CONDITION.validate_python({"q": "nope", "op": "answered"})}
        ),
    ]
    condition: object = {"q": "nope", "op": "answered"}
    for _ in range(5):
        condition = {"all": [condition]}
    questions = with_condition(
        5,
        {
            "all": [
                condition,
                {"q": "languages", "op": "includes_any", "value": ["rust", "go"]},
                {"q": "languages", "op": "answered"},
            ]
        },
    )
    questions[5] = questions[5].model_copy(update={"key": "languages", "position": 4})
    result = validate_definition(sections, questions, AREAS)
    assert result.errors == [
        ValidationIssue(loc=["sections", 2, "key"], msg="Section key is duplicated"),
        ValidationIssue(loc=["sections", 2, "position"], msg="Section position is duplicated"),
        ValidationIssue(loc=["sections", 2, "visible_if"], msg="Question nope does not exist"),
        ValidationIssue(loc=["questions", 5, "key"], msg="Question key is duplicated"),
        ValidationIssue(loc=["questions", 5, "position"], msg="Question position is duplicated"),
        ValidationIssue(
            loc=["questions", 5, "visible_if"], msg="Visibility condition exceeds depth 5"
        ),
        ValidationIssue(loc=["questions", 5, "visible_if"], msg="Question nope does not exist"),
        ValidationIssue(loc=["questions", 5, "visible_if"], msg="Option rust does not exist"),
        ValidationIssue(loc=["questions", 5, "visible_if"], msg="Option go does not exist"),
    ]


def test_warning_order() -> None:
    questions = with_condition(2, {"any": []})
    questions[0] = questions[0].model_copy(
        update={"visible_if": CONDITION.validate_python({"any": []})}
    )
    areas = [
        *AREAS,
        models.Area(key="z", name="Z", description="Z", sort=3, archived_at=None),
        models.Area(key="b", name="B", description="B", sort=2, archived_at=None),
        models.Area(key="a", name="A", description="A", sort=2, archived_at=None),
    ]
    result = validate_definition(SECTIONS, questions, areas)
    assert result.errors == []
    assert result.warnings == [
        ValidationIssue(
            loc=["questions", 0, "visible_if"], msg="Required question can never become visible"
        ),
        ValidationIssue(
            loc=["questions", 2, "visible_if"], msg="Required question can never become visible"
        ),
        ValidationIssue(loc=["area_hints", "a"], msg="No option has area hints for a"),
        ValidationIssue(loc=["area_hints", "b"], msg="No option has area hints for b"),
        ValidationIssue(loc=["area_hints", "z"], msg="No option has area hints for z"),
    ]


def test_option_duplicate_issues_precede_area_issues() -> None:
    questions = list(QUESTIONS)
    options = list(questions[0].options)
    options[0] = options[0].model_copy(update={"area_hints": {"mobile": 0.5}})
    options.append(options[0])
    questions[0] = questions[0].model_copy(update={"options": options})
    result = validate_definition(SECTIONS, questions, AREAS)
    assert result.errors == [
        ValidationIssue(loc=["questions", 0, "options", 2, "key"], msg="Option key is duplicated"),
        ValidationIssue(
            loc=["questions", 0, "options", 0, "area_hints", "mobile"],
            msg="Area mobile does not exist",
        ),
        ValidationIssue(
            loc=["questions", 0, "options", 2, "area_hints", "mobile"],
            msg="Area mobile does not exist",
        ),
    ]
