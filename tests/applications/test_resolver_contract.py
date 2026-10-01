"""Unknown factual questions never acquire fabricated deterministic answers."""

import pytest
from app.applications.resolver import AnswerResolver, normalize_question
from app.candidate.models import CandidateProfile


@pytest.mark.parametrize(
    "question",
    [
        "What is your expected compensation?",
        "Do you have an active security clearance?",
        "Are you willing to relocate to Antarctica?",
        "Please explain why you left Acme Corp.",
        "What is your GPA?",
        "What is your citizenship?",
        "What is your graduation date?",
    ],
)
def test_unknown_factual_question_requires_review(question: str) -> None:
    result = AnswerResolver().resolve(question)
    assert result.answer is None and not result.matched
    assert result.requires_review and not result.generation_allowed
    assert result.source is None and result.relevant_stories == []


@pytest.mark.parametrize(
    "question, expected",
    [
        ("Legal first name?", "Jane"),
        ("Family name", "Doe"),
        ("E-mail address", "jane@example.com"),
        ("University", "Fixture University"),
        ("Are you legally authorized to work?", "Yes"),
        ("Will you need sponsorship?", "No"),
    ],
)
def test_candidate_fact_aliases_are_deterministic(
    candidate_profile: CandidateProfile,
    question: str,
    expected: str,
) -> None:
    result = AnswerResolver(candidate_profile).resolve(question)
    assert result.answer == expected and result.source == "candidate_profile"
    assert result.confidence == 1.0 and not result.generation_allowed


def test_story_selection_is_bounded_sorted_and_explicit() -> None:
    resolver = AnswerResolver(stories={"stories": {"z": {"summary": "Z"}, "a": {"summary": "A"}}})
    assert resolver.get_stories() == {"story_ids": [], "stories": {}}
    assert resolver.get_stories(story_ids=["z", "z", "missing"]) == {
        "story_ids": ["z"],
        "stories": {"z": {"summary": "Z"}},
    }
    assert resolver.get_stories(question="What is your citizenship?")["stories"] == {}
    with pytest.raises(ValueError, match="at most 20"):
        resolver.get_stories(story_ids=["z"] * 21)
    with pytest.raises(ValueError, match="not both"):
        resolver.get_stories(story_ids=["z"], question="why role")


def test_normalization_and_generation_policy_do_not_produce_answers() -> None:
    assert normalize_question(" WHY this role?!  ") == "why this role"
    resolver = AnswerResolver(stories={"stories": {"project": {"summary": "Reviewed"}}})
    allowed = resolver.resolve("why role")
    assert allowed.generation_allowed and allowed.answer is None
    assert allowed.relevant_stories == ["project"]
    denied = AnswerResolver(rules={"generation_allowed": []}).resolve("why role")
    assert denied.requires_review and not denied.generation_allowed
