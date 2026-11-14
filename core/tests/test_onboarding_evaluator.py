import random
from typing import Any

import pytest
from hmm_contracts.algo import (
    OnboardingAnswer,
    QuizQuestion,
    QuizSection,
    VisibilityCondition,
)
from hmm_contracts.common import ValidationIssue
from hmm_core.onboarding.answers import known_answers, submission_answers, validate_values
from hmm_core.onboarding.visibility import answered, evaluate, visible_answers
from pydantic import TypeAdapter

CONDITION = TypeAdapter(VisibilityCondition)
QUESTIONS = TypeAdapter(list[QuizQuestion])


def _option(n: int, key: str) -> dict[str, Any]:
    return {
        "id": f"019b0000-0000-7000-8000-{n:012d}",
        "key": key,
        "label": key.title(),
        "position": n % 10,
        "area_hints": {},
    }


def _question(
    n: int,
    key: str,
    type_: str,
    config: dict[str, Any],
    *,
    section: str = "general",
    required: bool = False,
    visible_if: dict[str, Any] | None = None,
    options: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"019b0000-0000-7000-8000-{n:012d}",
        "section": section,
        "key": key,
        "type": type_,
        "text": key.title(),
        "help": None,
        "required": required,
        "position": n,
        "visible_if": visible_if,
        "config": config,
        "options": [_option(n * 10 + i, option) for i, option in enumerate(options or [])],
    }


SECTIONS = [
    QuizSection.model_validate({"key": "general", "position": 0, "visible_if": None}),
    QuizSection.model_validate(
        {
            "key": "advanced",
            "position": 1,
            "visible_if": {
                "any": [
                    {"q": "languages", "op": "includes", "value": "python"},
                    {"q": "years", "op": "gte", "value": 2},
                ]
            },
        }
    ),
]

RAW_QUESTIONS = [
    _question(
        1,
        "languages",
        "multi_choice",
        {"min": 1, "max": 2, "allow_other": True},
        required=True,
        options=["python", "react"],
    ),
    _question(2, "years", "number", {"min": 0, "max": 50, "unit": "years"}),
    _question(
        3,
        "direction",
        "single_choice",
        {"allow_other": False},
        required=True,
        visible_if={"q": "languages", "op": "includes", "value": "python"},
        options=["backend", "frontend"],
    ),
    _question(
        4,
        "about",
        "text",
        {"max_length": 20, "multiline": False},
        section="advanced",
        visible_if={"not": {"q": "years", "op": "gte", "value": 2}},
    ),
    _question(
        5, "rating", "scale", {"min": 1, "max": 10, "min_label": "new", "max_label": "expert"}
    ),
    _question(6, "link", "url", {}),
]

QUIZ = QUESTIONS.validate_python(RAW_QUESTIONS)


def _answers(raw: dict[str, Any]) -> dict[str, OnboardingAnswer]:
    return {
        key: value if isinstance(value, OnboardingAnswer) else OnboardingAnswer(value=value)
        for key, value in raw.items()
    }


def _check(condition: dict[str, Any], raw: dict[str, Any]) -> bool:
    return evaluate(CONDITION.validate_python(condition), _answers(raw))


def _keys(questions: list[QuizQuestion]) -> list[str]:
    return [question.key for question in questions]


@pytest.mark.parametrize(
    ("condition", "raw", "expected"),
    [
        ({"q": "d", "op": "eq", "value": "backend"}, {"d": "backend"}, True),
        ({"q": "d", "op": "eq", "value": "backend"}, {"d": "frontend"}, False),
        ({"q": "n", "op": "eq", "value": 1.0}, {"n": 1}, True),
        ({"q": "n", "op": "eq", "value": 1}, {"n": 1.0}, True),
        ({"q": "d", "op": "neq", "value": "backend"}, {"d": "frontend"}, True),
        ({"q": "d", "op": "neq", "value": "backend"}, {"d": "backend"}, False),
        ({"q": "n", "op": "neq", "value": 1}, {"n": 1.0}, False),
        ({"q": "l", "op": "includes", "value": "python"}, {"l": ["python", "react"]}, True),
        ({"q": "l", "op": "includes", "value": "go"}, {"l": ["python"]}, False),
        ({"q": "l", "op": "includes", "value": "python"}, {"l": "python"}, True),
        ({"q": "l", "op": "includes", "value": "python"}, {"l": "react"}, False),
        ({"q": "l", "op": "excludes", "value": "go"}, {"l": ["python"]}, True),
        ({"q": "l", "op": "excludes", "value": "python"}, {"l": ["python"]}, False),
        ({"q": "l", "op": "includes_any", "value": ["go", "python"]}, {"l": ["python"]}, True),
        ({"q": "l", "op": "includes_any", "value": ["go", "rust"]}, {"l": ["python"]}, False),
        ({"q": "l", "op": "includes_any", "value": "python"}, {"l": ["python"]}, False),
        ({"q": "n", "op": "gte", "value": 2}, {"n": 2}, True),
        ({"q": "n", "op": "gte", "value": 2}, {"n": 1.5}, False),
        ({"q": "n", "op": "lte", "value": 2}, {"n": 2.0}, True),
        ({"q": "n", "op": "lte", "value": 2}, {"n": 3}, False),
        ({"q": "n", "op": "gte", "value": 2}, {"n": "3"}, False),
        ({"q": "n", "op": "answered"}, {"n": 0}, True),
        ({"q": "n", "op": "answered"}, {"n": ""}, False),
        ({"q": "n", "op": "not_answered"}, {"n": []}, True),
        ({"q": "n", "op": "not_answered"}, {"n": "x"}, False),
        ({"q": "missing", "op": "not_answered"}, {}, True),
        ({"q": "missing", "op": "answered"}, {}, False),
        ({"q": "missing", "op": "eq", "value": "x"}, {}, False),
        ({"q": "missing", "op": "neq", "value": "x"}, {}, False),
        ({"q": "missing", "op": "includes", "value": "x"}, {}, False),
        ({"q": "missing", "op": "excludes", "value": "x"}, {}, False),
        ({"q": "missing", "op": "includes_any", "value": ["x"]}, {}, False),
        ({"q": "missing", "op": "gte", "value": 0}, {}, False),
        ({"q": "missing", "op": "lte", "value": 0}, {}, False),
        ({"q": "blank", "op": "neq", "value": "x"}, {"blank": "  "}, False),
        ({"q": "blank", "op": "excludes", "value": "x"}, {"blank": []}, False),
    ],
)
def test_operators(condition: dict[str, Any], raw: dict[str, Any], expected: bool) -> None:
    assert _check(condition, raw) is expected


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (None, False),
        (OnboardingAnswer(value=""), False),
        (OnboardingAnswer(value="  "), False),
        (OnboardingAnswer(value=[]), False),
        (OnboardingAnswer(value=0), True),
        (OnboardingAnswer(value=0.0), True),
        (OnboardingAnswer.model_construct(value=[0]), True),
        (OnboardingAnswer(value="a"), True),
    ],
)
def test_answered(answer: OnboardingAnswer | None, expected: bool) -> None:
    assert answered(answer) is expected


def test_combinators() -> None:
    raw = {"a": "x", "n": 3}
    assert evaluate(None, {}) is True
    assert _check({"all": []}, raw) is True
    assert _check({"any": []}, raw) is False
    assert _check({"all": [{"q": "a", "op": "answered"}, {"q": "n", "op": "gte", "value": 3}]}, raw)
    assert not _check(
        {"all": [{"q": "a", "op": "answered"}, {"q": "n", "op": "gte", "value": 4}]}, raw
    )
    assert _check({"any": [{"q": "b", "op": "answered"}, {"q": "n", "op": "lte", "value": 3}]}, raw)
    assert not _check({"any": [{"q": "b", "op": "answered"}, {"q": "c", "op": "answered"}]}, raw)
    assert _check({"not": {"q": "b", "op": "answered"}}, raw)
    assert not _check({"not": {"q": "a", "op": "answered"}}, raw)
    nested = {
        "all": [
            {"any": [{"q": "b", "op": "answered"}, {"q": "a", "op": "eq", "value": "x"}]},
            {"not": {"q": "n", "op": "gte", "value": 5}},
        ]
    }
    assert _check(nested, raw)
    assert not _check(nested, {"a": "x", "n": 5})
    assert not _check(nested, {"a": "y", "n": 1})


def test_branch_python() -> None:
    questions, visible = visible_answers(
        SECTIONS, QUIZ, _answers({"languages": ["python"], "years": 1, "rating": 5})
    )
    assert _keys(questions) == ["languages", "years", "direction", "about", "rating", "link"]
    assert list(visible) == ["languages", "years", "rating"]
    assert visible["rating"] == OnboardingAnswer(value=5)


def test_branch_react() -> None:
    questions, visible = visible_answers(
        SECTIONS,
        QUIZ,
        _answers({"languages": ["react"], "years": 3, "direction": "backend", "about": "x"}),
    )
    assert _keys(questions) == ["languages", "years", "rating", "link"]
    assert list(visible) == ["languages", "years"]


def test_hidden_answer_does_not_unlock() -> None:
    raw = [dict(question) for question in RAW_QUESTIONS]
    raw[4] = {**raw[4], "visible_if": {"q": "direction", "op": "eq", "value": "backend"}}
    quiz = QUESTIONS.validate_python(raw)
    questions, visible = visible_answers(
        SECTIONS, quiz, _answers({"languages": ["react"], "direction": "backend", "rating": 4})
    )
    assert _keys(questions) == ["languages", "years", "link"]
    assert list(visible) == ["languages"]
    questions, _ = visible_answers(
        SECTIONS, quiz, _answers({"languages": ["python"], "direction": "backend"})
    )
    assert "rating" in _keys(questions)


def test_section_and_question_conditions() -> None:
    questions, _ = visible_answers(SECTIONS, QUIZ, _answers({"languages": ["python"], "years": 2}))
    assert "about" not in _keys(questions)
    raw = [dict(question) for question in RAW_QUESTIONS]
    raw[3] = {**raw[3], "visible_if": None}
    quiz = QUESTIONS.validate_python(raw)
    questions, _ = visible_answers(SECTIONS, quiz, _answers({"languages": ["react"], "years": 1}))
    assert "about" not in _keys(questions)
    questions, _ = visible_answers(SECTIONS, quiz, _answers({"languages": ["react"], "years": 2}))
    assert "about" in _keys(questions)


def test_input_order_independence() -> None:
    answers = _answers({"languages": ["python"], "years": 1, "rating": 5, "about": "hi"})
    expected = visible_answers(SECTIONS, QUIZ, answers)
    shuffled = list(QUIZ)
    random.Random(7).shuffle(shuffled)
    assert _keys(shuffled) != _keys(QUIZ)
    questions, visible = visible_answers(list(reversed(SECTIONS)), shuffled, answers)
    assert _keys(questions) == _keys(expected[0])
    assert visible == expected[1]


def test_known_answers() -> None:
    answers = _answers({"rating": 3, "ghost": "x", "languages": ["python"], "other": 1})
    assert list(known_answers(QUIZ, answers)) == ["rating", "languages"]


VALID = {
    "languages": OnboardingAnswer(value=["python", "other"], other="Rust"),
    "years": 3.5,
    "direction": "backend",
    "about": "hello",
    "rating": 10,
    "link": "https://example.com/me",
    "ghost": 1,
}


def test_validate_values_valid() -> None:
    assert validate_values(QUIZ, _answers(VALID)) == []


def _issue(key: str, msg: str, field: str = "value") -> ValidationIssue:
    return ValidationIssue(loc=["answers", key, field], msg=msg)


@pytest.mark.parametrize(
    ("key", "answer", "issues"),
    [
        ("direction", 1, [_issue("direction", "Value must be an option key")]),
        ("direction", "qa", [_issue("direction", "Option qa does not exist")]),
        ("direction", ["backend"], [_issue("direction", "Value must be an option key")]),
        (
            "languages",
            OnboardingAnswer(value=["other"]),
            [_issue("languages", "Other value is missing", "other")],
        ),
        (
            "languages",
            OnboardingAnswer(value=["python", "other"], other="  "),
            [_issue("languages", "Other value is missing", "other")],
        ),
        ("languages", "python", [_issue("languages", "Value must be a list of option keys")]),
        (
            "languages",
            OnboardingAnswer.model_construct(value=[1]),
            [_issue("languages", "Value must be a list of option keys")],
        ),
        ("languages", ["go"], [_issue("languages", "Option go does not exist")]),
        ("languages", [], [_issue("languages", "Too few options selected")]),
        (
            "languages",
            OnboardingAnswer(value=["python", "react", "other"], other="Go"),
            [_issue("languages", "Too many options selected")],
        ),
        ("rating", 2.5, [_issue("rating", "Value must be an integer")]),
        ("rating", "5", [_issue("rating", "Value must be an integer")]),
        ("rating", 0, [_issue("rating", "Value is below the minimum")]),
        ("rating", 11, [_issue("rating", "Value is above the maximum")]),
        ("years", "3", [_issue("years", "Value must be a number")]),
        ("years", -1, [_issue("years", "Value is below the minimum")]),
        ("years", 50.5, [_issue("years", "Value is above the maximum")]),
        ("about", 5, [_issue("about", "Value must be a string")]),
        ("about", "x" * 21, [_issue("about", "Value is too long")]),
        ("link", 5, [_issue("link", "Value must be a string")]),
        ("link", "ftp://example.com", [_issue("link", "Value must be a URL")]),
        ("link", "not a url", [_issue("link", "Value must be a URL")]),
        (
            "about",
            OnboardingAnswer(value="hi", other="x"),
            [_issue("about", "Question does not allow other", "other")],
        ),
        (
            "direction",
            OnboardingAnswer(value="backend", other="x"),
            [_issue("direction", "Question does not allow other", "other")],
        ),
    ],
)
def test_validate_values_invalid(key: str, answer: Any, issues: list[ValidationIssue]) -> None:
    assert validate_values(QUIZ, _answers({**VALID, key: answer})) == issues


def test_single_choice_other() -> None:
    raw = [dict(question) for question in RAW_QUESTIONS]
    raw[2] = {**raw[2], "config": {"allow_other": True}}
    quiz = QUESTIONS.validate_python(raw)
    ok = _answers({"direction": OnboardingAnswer(value="other", other="QA")})
    assert validate_values(quiz, ok) == []
    for answer in (
        OnboardingAnswer(value="other"),
        OnboardingAnswer(value="other", other=" "),
        OnboardingAnswer(value="qa", other="QA"),
    ):
        assert validate_values(quiz, {"direction": answer}) == [
            _issue("direction", "Value must be an option key")
        ]


def test_submission_hidden_required() -> None:
    kept, issues = submission_answers(
        SECTIONS, QUIZ, _answers({"languages": ["react"], "years": 3, "ghost": 1})
    )
    assert issues == []
    assert list(kept) == ["languages", "years"]


def test_submission_visible_required_missing() -> None:
    kept, issues = submission_answers(SECTIONS, QUIZ, _answers({"languages": ["python"]}))
    assert issues == [
        ValidationIssue(loc=["answers", "direction"], msg="Required question is not answered")
    ]
    assert list(kept) == ["languages"]


def test_submission_hidden_invalid_dropped() -> None:
    kept, issues = submission_answers(
        SECTIONS,
        QUIZ,
        _answers({"languages": ["react"], "years": 3, "direction": "qa", "about": 5}),
    )
    assert issues == []
    assert list(kept) == ["languages", "years"]


def test_submission_issue_order() -> None:
    kept, issues = submission_answers(
        SECTIONS,
        QUIZ,
        _answers({"link": "bad", "years": "x", "rating": 0, "about": "ok", "ghost": "y"}),
    )
    assert issues == [
        _issue("years", "Value must be a number"),
        _issue("rating", "Value is below the minimum"),
        _issue("link", "Value must be a URL"),
        ValidationIssue(loc=["answers", "languages"], msg="Required question is not answered"),
    ]
    assert "ghost" not in kept
    assert list(kept) == ["years", "rating", "link"]
