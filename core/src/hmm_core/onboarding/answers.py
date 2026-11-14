from collections.abc import Mapping, Sequence

from hmm_contracts.algo import (
    MultiChoiceQuestion,
    NumberQuestion,
    OnboardingAnswer,
    QuizQuestion,
    QuizSection,
    ScaleQuestion,
    SingleChoiceQuestion,
    TextQuestion,
    UrlQuestion,
)
from hmm_contracts.common import ValidationIssue
from pydantic import HttpUrl, TypeAdapter, ValidationError

from hmm_core.onboarding.visibility import visible_answers

_URL = TypeAdapter(HttpUrl)


def known_answers(
    questions: Sequence[QuizQuestion], answers: Mapping[str, OnboardingAnswer]
) -> dict[str, OnboardingAnswer]:
    keys = {question.key for question in questions}
    return {key: answer for key, answer in answers.items() if key in keys}


def validate_values(
    questions: Sequence[QuizQuestion], answers: Mapping[str, OnboardingAnswer]
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for question in sorted(questions, key=lambda question: (question.position, question.key)):
        answer = answers.get(question.key)
        if answer is None:
            continue
        issues.extend(_value_issues(question, answer))
        if answer.other is not None and not _allows_other(question):
            issues.append(
                ValidationIssue(
                    loc=["answers", question.key, "other"], msg="Question does not allow other"
                )
            )
    return issues


def submission_answers(
    sections: Sequence[QuizSection],
    questions: Sequence[QuizQuestion],
    answers: Mapping[str, OnboardingAnswer],
) -> tuple[dict[str, OnboardingAnswer], list[ValidationIssue]]:
    visible_questions, kept = visible_answers(sections, questions, answers)
    issues = validate_values(visible_questions, kept)
    issues.extend(
        ValidationIssue(loc=["answers", question.key], msg="Required question is not answered")
        for question in visible_questions
        if question.required and question.key not in kept
    )
    return kept, issues


def _allows_other(question: QuizQuestion) -> bool:
    if isinstance(question, SingleChoiceQuestion | MultiChoiceQuestion):
        return question.config.allow_other
    return False


def _has_other(answer: OnboardingAnswer) -> bool:
    return answer.other is not None and bool(answer.other.strip())


def _value_issues(question: QuizQuestion, answer: OnboardingAnswer) -> list[ValidationIssue]:
    loc: list[str | int] = ["answers", question.key, "value"]
    value = answer.value
    option_keys = {option.key for option in question.options}
    match question:
        case SingleChoiceQuestion():
            if not isinstance(value, str):
                return [ValidationIssue(loc=loc, msg="Value must be an option key")]
            if value in option_keys:
                return []
            if question.config.allow_other:
                if value == "other" and _has_other(answer):
                    return []
                return [ValidationIssue(loc=loc, msg="Value must be an option key")]
            return [ValidationIssue(loc=loc, msg=f"Option {value} does not exist")]
        case MultiChoiceQuestion():
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                return [ValidationIssue(loc=loc, msg="Value must be a list of option keys")]
            issues: list[ValidationIssue] = []
            for item in value:
                if item in option_keys:
                    continue
                if item == "other" and question.config.allow_other:
                    if not _has_other(answer):
                        issues.append(
                            ValidationIssue(
                                loc=["answers", question.key, "other"],
                                msg="Other value is missing",
                            )
                        )
                    continue
                issues.append(ValidationIssue(loc=loc, msg=f"Option {item} does not exist"))
            if len(value) < question.config.min:
                issues.append(ValidationIssue(loc=loc, msg="Too few options selected"))
            if question.config.max > 0 and len(value) > question.config.max:
                issues.append(ValidationIssue(loc=loc, msg="Too many options selected"))
            return issues
        case ScaleQuestion():
            if not isinstance(value, int) or isinstance(value, bool):
                return [ValidationIssue(loc=loc, msg="Value must be an integer")]
            if value < question.config.min:
                return [ValidationIssue(loc=loc, msg="Value is below the minimum")]
            if value > question.config.max:
                return [ValidationIssue(loc=loc, msg="Value is above the maximum")]
            return []
        case NumberQuestion():
            if not isinstance(value, int | float) or isinstance(value, bool):
                return [ValidationIssue(loc=loc, msg="Value must be a number")]
            if question.config.min is not None and value < question.config.min:
                return [ValidationIssue(loc=loc, msg="Value is below the minimum")]
            if question.config.max is not None and value > question.config.max:
                return [ValidationIssue(loc=loc, msg="Value is above the maximum")]
            return []
        case TextQuestion():
            if not isinstance(value, str):
                return [ValidationIssue(loc=loc, msg="Value must be a string")]
            if len(value) > question.config.max_length:
                return [ValidationIssue(loc=loc, msg="Value is too long")]
            return []
        case UrlQuestion():
            if not isinstance(value, str):
                return [ValidationIssue(loc=loc, msg="Value must be a string")]
            try:
                _URL.validate_python(value)
            except ValidationError:
                return [ValidationIssue(loc=loc, msg="Value must be a URL")]
            return []
    return []
