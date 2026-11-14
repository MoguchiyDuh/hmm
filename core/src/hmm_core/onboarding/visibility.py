from collections.abc import Mapping, Sequence

from hmm_contracts.algo import (
    OnboardingAnswer,
    QuizQuestion,
    QuizSection,
    VisibilityAll,
    VisibilityAny,
    VisibilityCondition,
    VisibilityNot,
    VisibilityOperator,
    VisibilityPredicate,
)


def answered(answer: OnboardingAnswer | None) -> bool:
    if answer is None:
        return False
    value = answer.value
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    return True


def evaluate(
    condition: VisibilityCondition | None, answers: Mapping[str, OnboardingAnswer]
) -> bool:
    if condition is None:
        return True
    if isinstance(condition, VisibilityAll):
        return all(evaluate(child, answers) for child in condition.all)
    if isinstance(condition, VisibilityAny):
        return any(evaluate(child, answers) for child in condition.any)
    if isinstance(condition, VisibilityNot):
        return not evaluate(condition.condition, answers)
    return _predicate(condition, answers)


def visible_answers(
    sections: Sequence[QuizSection],
    questions: Sequence[QuizQuestion],
    answers: Mapping[str, OnboardingAnswer],
) -> tuple[list[QuizQuestion], dict[str, OnboardingAnswer]]:
    section_conditions = {section.key: section.visible_if for section in sections}
    shown_questions: list[QuizQuestion] = []
    visible: dict[str, OnboardingAnswer] = {}
    for question in sorted(questions, key=lambda question: (question.position, question.key)):
        shown = evaluate(section_conditions.get(question.section), visible) and evaluate(
            question.visible_if, visible
        )
        if not shown:
            continue
        shown_questions.append(question)
        if question.key in answers:
            visible[question.key] = answers[question.key]
    return shown_questions, visible


def _predicate(predicate: VisibilityPredicate, answers: Mapping[str, OnboardingAnswer]) -> bool:
    answer = answers.get(predicate.q)
    is_answered = answered(answer)
    if predicate.op == VisibilityOperator.ANSWERED:
        return is_answered
    if predicate.op == VisibilityOperator.NOT_ANSWERED:
        return not is_answered
    if answer is None or not is_answered:
        return False
    actual = answer.value
    expected = predicate.value
    match predicate.op:
        case VisibilityOperator.EQ:
            return _equal(actual, expected)
        case VisibilityOperator.NEQ:
            return not _equal(actual, expected)
        case VisibilityOperator.INCLUDES:
            return _includes(actual, expected)
        case VisibilityOperator.EXCLUDES:
            return not _includes(actual, expected)
        case VisibilityOperator.INCLUDES_ANY:
            return isinstance(expected, list) and any(_includes(actual, item) for item in expected)
        case VisibilityOperator.GTE:
            return _compare(actual, expected, greater_or_equal=True)
        case VisibilityOperator.LTE:
            return _compare(actual, expected, greater_or_equal=False)
    return False


def _equal(actual: object, expected: object) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    return actual == expected


def _includes(actual: object, expected: object) -> bool:
    if isinstance(actual, list):
        return expected in actual
    if isinstance(actual, str):
        return actual == expected
    return False


def _compare(actual: object, expected: object, *, greater_or_equal: bool) -> bool:
    if not isinstance(actual, int | float) or isinstance(actual, bool):
        return False
    if not isinstance(expected, int | float) or isinstance(expected, bool):
        return False
    return actual >= expected if greater_or_equal else actual <= expected
