from itertools import combinations

from hmm_contracts.algo import (
    QuestionType,
    QuizQuestion,
    QuizSection,
    VisibilityAll,
    VisibilityAny,
    VisibilityCondition,
    VisibilityNot,
    VisibilityOperator,
    VisibilityPredicate,
)
from hmm_contracts.common import ValidationIssue

from hmm_core import models
from hmm_core.onboarding.schemas import QuizValidation

_OPERATORS = {
    QuestionType.SINGLE_CHOICE: {VisibilityOperator.EQ, VisibilityOperator.NEQ},
    QuestionType.MULTI_CHOICE: {
        VisibilityOperator.INCLUDES,
        VisibilityOperator.EXCLUDES,
        VisibilityOperator.INCLUDES_ANY,
    },
    QuestionType.SCALE: {
        VisibilityOperator.EQ,
        VisibilityOperator.NEQ,
        VisibilityOperator.GTE,
        VisibilityOperator.LTE,
    },
    QuestionType.NUMBER: {
        VisibilityOperator.EQ,
        VisibilityOperator.NEQ,
        VisibilityOperator.GTE,
        VisibilityOperator.LTE,
    },
    QuestionType.TEXT: {VisibilityOperator.EQ, VisibilityOperator.NEQ},
    QuestionType.URL: {VisibilityOperator.EQ, VisibilityOperator.NEQ},
}
_VALUELESS = {VisibilityOperator.ANSWERED, VisibilityOperator.NOT_ANSWERED}


def validate_definition(
    sections: list[QuizSection],
    questions: list[QuizQuestion],
    areas: list[models.Area],
) -> QuizValidation:
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    by_key: dict[str, QuizQuestion] = {}
    for question in questions:
        by_key.setdefault(question.key, question)
    section_keys: set[str] = set()
    section_positions: set[int] = set()
    sections_by_key: dict[str, QuizSection] = {}
    for i, section in enumerate(sections):
        if section.key in section_keys:
            errors.append(
                ValidationIssue(loc=["sections", i, "key"], msg="Section key is duplicated")
            )
        if section.position in section_positions:
            errors.append(
                ValidationIssue(
                    loc=["sections", i, "position"], msg="Section position is duplicated"
                )
            )
        section_keys.add(section.key)
        section_positions.add(section.position)
        sections_by_key.setdefault(section.key, section)
        if section.visible_if is not None:
            barrier = min((q.position for q in questions if q.section == section.key), default=None)
            errors.extend(
                _condition_issues(
                    section.visible_if, by_key, barrier, ["sections", i, "visible_if"]
                )
            )
    question_keys: set[str] = set()
    question_positions: set[int] = set()
    areas_by_key = {area.key: area for area in areas}
    hinted_areas: set[str] = set()
    for i, question in enumerate(questions):
        if question.key in question_keys:
            errors.append(
                ValidationIssue(loc=["questions", i, "key"], msg="Question key is duplicated")
            )
        if question.position in question_positions:
            errors.append(
                ValidationIssue(
                    loc=["questions", i, "position"], msg="Question position is duplicated"
                )
            )
        question_keys.add(question.key)
        question_positions.add(question.position)
        if question.section not in section_keys:
            errors.append(
                ValidationIssue(loc=["questions", i, "section"], msg="Section does not exist")
            )
        option_keys: set[str] = set()
        for j, option in enumerate(question.options):
            if option.key in option_keys:
                errors.append(
                    ValidationIssue(
                        loc=["questions", i, "options", j, "key"], msg="Option key is duplicated"
                    )
                )
            option_keys.add(option.key)
        for j, option in enumerate(question.options):
            for key in option.area_hints:
                hinted_areas.add(key)
                area = areas_by_key.get(key)
                loc: list[str | int] = ["questions", i, "options", j, "area_hints", key]
                if area is None:
                    errors.append(ValidationIssue(loc=loc, msg=f"Area {key} does not exist"))
                elif area.archived_at is not None:
                    errors.append(ValidationIssue(loc=loc, msg=f"Area {key} is archived"))
        if question.visible_if is not None:
            errors.extend(
                _condition_issues(
                    question.visible_if, by_key, question.position, ["questions", i, "visible_if"]
                )
            )
    for i, question in enumerate(questions):
        section = sections_by_key.get(question.section)
        effective = question.visible_if
        if section is not None and section.visible_if is not None:
            effective = (
                section.visible_if
                if effective is None
                else VisibilityAll(all=[section.visible_if, effective])
            )
        if question.required and effective is not None and _unsatisfiable(effective):
            warnings.append(
                ValidationIssue(
                    loc=["questions", i, "visible_if"],
                    msg="Required question can never become visible",
                )
            )
    for area in sorted(areas, key=lambda area: (area.sort, area.key)):
        if area.archived_at is None and area.key not in hinted_areas:
            warnings.append(
                ValidationIssue(
                    loc=["area_hints", area.key], msg=f"No option has area hints for {area.key}"
                )
            )
    return QuizValidation(errors=errors, warnings=warnings)


def _condition_issues(
    condition: VisibilityCondition,
    by_key: dict[str, QuizQuestion],
    barrier: int | None,
    loc: list[str | int],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for pred, depth in _predicates(condition):
        if depth > 5:
            issues.append(ValidationIssue(loc=loc, msg="Visibility condition exceeds depth 5"))
        target = by_key.get(pred.q)
        if target is None:
            issues.append(ValidationIssue(loc=loc, msg=f"Question {pred.q} does not exist"))
            continue
        if barrier is not None and target.position >= barrier:
            issues.append(
                ValidationIssue(loc=loc, msg=f"Question {pred.q} is not positioned earlier")
            )
        if pred.op not in _OPERATORS[target.type] | _VALUELESS:
            issues.append(
                ValidationIssue(
                    loc=loc, msg=f"Operator {pred.op.value} is not valid for {target.type.value}"
                )
            )
            continue
        issues.extend(ValidationIssue(loc=loc, msg=msg) for msg in _value_issues(pred, target))
    return issues


def _predicates(
    condition: VisibilityCondition, depth: int = 1
) -> list[tuple[VisibilityPredicate, int]]:
    if isinstance(condition, VisibilityPredicate):
        return [(condition, depth)]
    if isinstance(condition, VisibilityNot):
        return _predicates(condition.condition, depth + 1)
    children = condition.all if isinstance(condition, VisibilityAll) else condition.any
    return [item for child in children for item in _predicates(child, depth + 1)]


def _value_issues(pred: VisibilityPredicate, target: QuizQuestion) -> list[str]:
    value = pred.value
    if pred.op in _VALUELESS:
        return ["Operator does not take a value"] if value is not None else []
    option_keys = {option.key for option in target.options}
    if pred.op == VisibilityOperator.INCLUDES_ANY:
        if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
            return ["Value must be a non-empty list of option keys"]
        return [f"Option {item} does not exist" for item in value if item not in option_keys]
    if target.type == QuestionType.SINGLE_CHOICE or pred.op in {
        VisibilityOperator.INCLUDES,
        VisibilityOperator.EXCLUDES,
    }:
        if not isinstance(value, str):
            return ["Value must be an option key"]
        return [f"Option {value} does not exist"] if value not in option_keys else []
    if target.type in {QuestionType.TEXT, QuestionType.URL}:
        return [] if isinstance(value, str) else ["Value must be a string"]
    if target.type == QuestionType.SCALE:
        return (
            []
            if isinstance(value, int) and not isinstance(value, bool)
            else ["Value must be an integer"]
        )
    return [] if _number(value) else ["Value must be a number"]


def _unsatisfiable(condition: VisibilityCondition) -> bool:
    if isinstance(condition, VisibilityPredicate | VisibilityNot):
        return False
    if isinstance(condition, VisibilityAny):
        return all(_unsatisfiable(child) for child in condition.any)
    if any(_unsatisfiable(child) for child in condition.all):
        return True
    by_question: dict[str, list[VisibilityPredicate]] = {}
    for pred in _flatten_all(condition):
        by_question.setdefault(pred.q, []).append(pred)
    return any(
        _contradicts(a, b) for group in by_question.values() for a, b in combinations(group, 2)
    )


def _flatten_all(condition: VisibilityCondition) -> list[VisibilityPredicate]:
    if isinstance(condition, VisibilityPredicate):
        return [condition]
    if isinstance(condition, VisibilityAll):
        return [pred for child in condition.all for pred in _flatten_all(child)]
    return []


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _contradicts(a: VisibilityPredicate, b: VisibilityPredicate) -> bool:
    if a.q != b.q:
        return False
    for left, right in ((a, b), (b, a)):
        x, y = left.value, right.value
        match left.op, right.op:
            case VisibilityOperator.ANSWERED, VisibilityOperator.NOT_ANSWERED:
                return True
            case VisibilityOperator.EQ, VisibilityOperator.EQ | VisibilityOperator.NEQ:
                if (isinstance(x, str) and isinstance(y, str)) or (_number(x) and _number(y)):
                    return x != y if right.op == VisibilityOperator.EQ else x == y
            case VisibilityOperator.INCLUDES, VisibilityOperator.EXCLUDES:
                if isinstance(x, str) and isinstance(y, str):
                    return x == y
            case VisibilityOperator.INCLUDES, VisibilityOperator.INCLUDES_ANY:
                if isinstance(x, str) and isinstance(y, list):
                    return all(isinstance(item, str) for item in y) and x not in y
            case VisibilityOperator.INCLUDES_ANY, VisibilityOperator.INCLUDES_ANY:
                if isinstance(x, list) and isinstance(y, list):
                    return all(isinstance(item, str) for item in [*x, *y]) and not set(x) & set(y)
            case VisibilityOperator.GTE, VisibilityOperator.LTE:
                if isinstance(x, int | float) and isinstance(y, int | float):
                    return _number(x) and _number(y) and x > y
            case VisibilityOperator.EQ, VisibilityOperator.GTE | VisibilityOperator.LTE:
                if isinstance(x, int | float) and isinstance(y, int | float):
                    return (
                        _number(x)
                        and _number(y)
                        and (x < y if right.op == VisibilityOperator.GTE else x > y)
                    )
            case VisibilityOperator.EXCLUDES, VisibilityOperator.INCLUDES_ANY:
                if isinstance(x, str) and isinstance(y, list):
                    return all(isinstance(item, str) for item in y) and set(y) == {x}
    return False
