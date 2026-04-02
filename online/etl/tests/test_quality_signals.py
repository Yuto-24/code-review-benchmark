"""Tests for per-PR quality signal computation."""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pipeline.quality import (
    is_bot_username,
    compute_and_serialize,
    compute_quality_signals,
)


# -- _is_bot_username ---------------------------------------------------------

@pytest.mark.parametrize("username,expected", [
    ("coderabbitai[bot]", True),
    ("dependabot[bot]", True),
    ("Copilot", True),  # in DEFAULT_CHATBOT_USERNAMES
    ("copilot", True),  # case insensitive
    ("renovate", True),
    ("github-actions", True),
    ("alice", False),
    ("mybot", False),
    ("snyk-bot", True),
    ("DEPENDABOT", True),  # case insensitive
    ("GitHub-Actions", True),
    ("cubic-dev-ai", True),  # chatbot without [bot] suffix (BQ events)
    ("gemini-code-assist", True),  # chatbot without [bot] suffix
    ("github-advanced-security", True),  # general bot
])
def test_is_bot_username(username: str, expected: bool) -> None:
    assert is_bot_username(username) == expected


# -- compute_quality_signals --------------------------------------------------

def _make_event(
    event_type: str,
    actor: str,
    timestamp: str,
    **data_kwargs: object,
) -> dict:
    return {
        "event_type": event_type,
        "actor": actor,
        "timestamp": timestamp,
        "data": data_kwargs,
    }


def _make_assembled(
    pr_author: str = "alice",
    events: list[dict] | None = None,
) -> dict:
    return {
        "pr_author": pr_author,
        "events": events or [],
    }


class TestHasHumanEngagement:
    """Test the has_human_engagement signal."""

    def test_no_events(self) -> None:
        result = compute_quality_signals(_make_assembled(), "coderabbitai[bot]")
        assert result["has_human_engagement"] is False

    def test_only_bot_events(self) -> None:
        events = [
            _make_event("review", "coderabbitai[bot]", "2026-01-01T00:00:00Z"),
            _make_event("review_comment", "coderabbitai[bot]", "2026-01-01T00:01:00Z"),
        ]
        result = compute_quality_signals(_make_assembled(events=events), "coderabbitai[bot]")
        assert result["has_human_engagement"] is False

    def test_human_comment_after_bot_review(self) -> None:
        events = [
            _make_event("review", "coderabbitai[bot]", "2026-01-01T00:00:00Z"),
            _make_event("issue_comment", "alice", "2026-01-01T00:05:00Z"),
        ]
        result = compute_quality_signals(_make_assembled(events=events), "coderabbitai[bot]")
        assert result["has_human_engagement"] is True

    def test_human_commit_after_bot_review(self) -> None:
        events = [
            _make_event("review", "coderabbitai[bot]", "2026-01-01T00:00:00Z"),
            _make_event("commit", "alice", "2026-01-01T01:00:00Z"),
        ]
        result = compute_quality_signals(_make_assembled(events=events), "coderabbitai[bot]")
        assert result["has_human_engagement"] is True

    def test_human_comment_before_bot_review_only(self) -> None:
        """Human activity only before the bot reviewed — doesn't count."""
        events = [
            _make_event("issue_comment", "alice", "2026-01-01T00:00:00Z"),
            _make_event("review", "coderabbitai[bot]", "2026-01-01T00:10:00Z"),
        ]
        result = compute_quality_signals(_make_assembled(events=events), "coderabbitai[bot]")
        assert result["has_human_engagement"] is False

    def test_another_bot_comment_doesnt_count(self) -> None:
        """Another bot commenting after review isn't human engagement."""
        events = [
            _make_event("review", "coderabbitai[bot]", "2026-01-01T00:00:00Z"),
            _make_event("issue_comment", "dependabot[bot]", "2026-01-01T00:05:00Z"),
        ]
        result = compute_quality_signals(_make_assembled(events=events), "coderabbitai[bot]")
        assert result["has_human_engagement"] is False

    def test_pr_event_doesnt_count_as_engagement(self) -> None:
        """Non-comment/commit events (like pr_merged) from humans don't count."""
        events = [
            _make_event("review", "coderabbitai[bot]", "2026-01-01T00:00:00Z"),
            _make_event("pr_merged", "alice", "2026-01-01T01:00:00Z"),
        ]
        result = compute_quality_signals(_make_assembled(events=events), "coderabbitai[bot]")
        assert result["has_human_engagement"] is False

    def test_bot_no_review_events(self) -> None:
        """If the bot only has non-review events, no engagement is tracked."""
        events = [
            _make_event("commit", "coderabbitai[bot]", "2026-01-01T00:00:00Z"),
            _make_event("issue_comment", "alice", "2026-01-01T00:05:00Z"),
        ]
        result = compute_quality_signals(_make_assembled(events=events), "coderabbitai[bot]")
        assert result["has_human_engagement"] is False

    def test_review_comment_counts_as_bot_review(self) -> None:
        events = [
            _make_event("review_comment", "coderabbitai[bot]", "2026-01-01T00:00:00Z"),
            _make_event("issue_comment", "alice", "2026-01-01T00:05:00Z"),
        ]
        result = compute_quality_signals(_make_assembled(events=events), "coderabbitai[bot]")
        assert result["has_human_engagement"] is True

    def test_issue_comment_counts_as_bot_review(self) -> None:
        events = [
            _make_event("issue_comment", "coderabbitai[bot]", "2026-01-01T00:00:00Z"),
            _make_event("review_comment", "alice", "2026-01-01T00:05:00Z"),
        ]
        result = compute_quality_signals(_make_assembled(events=events), "coderabbitai[bot]")
        assert result["has_human_engagement"] is True


class TestSerialize:
    def test_round_trip(self) -> None:
        assembled = _make_assembled(
            pr_author="alice",
            events=[
                _make_event("review", "coderabbitai[bot]", "2026-01-01T00:00:00Z"),
                _make_event("issue_comment", "alice", "2026-01-01T00:05:00Z"),
            ],
        )
        serialized = compute_and_serialize(assembled, "coderabbitai[bot]")
        parsed = json.loads(serialized)
        assert parsed == {"has_human_engagement": True}


# -- Property-based tests ---------------------------------------------------

_actor_strat = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_[]"),
    min_size=1,
    max_size=30,
)

_timestamp_strat = st.datetimes().map(lambda dt: dt.isoformat() + "Z")

_event_type_strat = st.sampled_from(["review", "review_comment", "issue_comment", "commit", "pr_merged", "pr_closed"])


@given(
    pr_author=_actor_strat,
    chatbot=_actor_strat,
    events=st.lists(
        st.tuples(_event_type_strat, _actor_strat, _timestamp_strat),
        max_size=20,
    ),
)
@settings(max_examples=200)
def test_signals_always_valid_structure(
    pr_author: str,
    chatbot: str,
    events: list[tuple[str, str, str]],
) -> None:
    """Quality signals always return the expected keys with boolean values."""
    assembled = _make_assembled(
        pr_author=pr_author,
        events=[_make_event(et, actor, ts) for et, actor, ts in events],
    )
    result = compute_quality_signals(assembled, chatbot)
    assert set(result.keys()) == {"has_human_engagement"}
    assert isinstance(result["has_human_engagement"], bool)


@given(
    chatbot=_actor_strat,
    n_bot_only=st.integers(min_value=0, max_value=10),
)
@settings(max_examples=100)
def test_bot_only_never_has_engagement(chatbot: str, n_bot_only: int) -> None:
    """If all events are from the chatbot, there can be no human engagement."""
    events = [
        _make_event("review_comment", chatbot, f"2026-01-01T00:{i:02d}:00Z")
        for i in range(n_bot_only)
    ]
    assembled = _make_assembled(pr_author="alice", events=events)
    result = compute_quality_signals(assembled, chatbot)
    assert result["has_human_engagement"] is False
