"""Compute per-PR quality signals from assembled timeline data.

Currently computes:
  - has_human_engagement: whether a non-bot actor posted comments or pushed
    commits after the bot's first review activity

Signals derivable from existing columns (computed at load time, not stored):
  - pr_author_is_bot: pr_author ends in [bot] or is a known bot
  - repo_unique_contributors: COUNT(DISTINCT pr_author) per repo
  - author_repo_pr_count: count per (repo, author, bot) triple
"""

from __future__ import annotations

import json
import logging
from typing import Any

from config import DEFAULT_CHATBOT_USERNAMES

logger = logging.getLogger(__name__)

# BQ events often store bot actors without the [bot] suffix, so we build
# a comprehensive set from DEFAULT_CHATBOT_USERNAMES plus other known bots.
_GENERAL_BOT_USERNAMES = frozenset({
    "dependabot", "renovate", "github-actions", "codecov",
    "mergify", "snyk-bot", "greenkeeper", "imgbot",
    "stale", "allcontributors", "semantic-release-bot",
    "github-advanced-security",
})

_KNOWN_BOT_USERNAMES: frozenset[str] = frozenset(
    {name.lower() for name in _GENERAL_BOT_USERNAMES}
    | {name.lower() for name in DEFAULT_CHATBOT_USERNAMES}
    | {name.lower().removesuffix("[bot]") for name in DEFAULT_CHATBOT_USERNAMES}
)

_COMMENT_EVENT_TYPES = frozenset({"review", "review_comment", "issue_comment"})
_REVIEW_EVENT_TYPES = frozenset({"review", "review_comment", "issue_comment"})


def is_bot_username(username: str) -> bool:
    """Heuristic: username is a bot if it ends with [bot] or matches a known bot name.

    Handles BQ event actors that may lack the [bot] suffix (e.g. 'cubic-dev-ai'
    instead of 'cubic-dev-ai[bot]').
    """
    lower = username.lower()
    return lower.endswith("[bot]") or lower in _KNOWN_BOT_USERNAMES


def compute_quality_signals(
    assembled: dict[str, Any],
    chatbot_username: str,
) -> dict[str, Any]:
    """Compute quality signals from an assembled PR record.

    Returns a dict suitable for JSON serialization into the quality_signals column.
    Only includes signals that require timeline parsing — signals derivable from
    existing columns (like pr_author_is_bot) are computed at load time instead.
    """
    events = assembled.get("events", [])

    # Find the bot's first review/comment event timestamp
    bot_lower = chatbot_username.lower()
    bot_first_activity_ts: str | None = None
    for e in events:
        actor = (e.get("actor") or "").lower()
        if actor != bot_lower:
            continue
        if e.get("event_type") in _REVIEW_EVENT_TYPES:
            bot_first_activity_ts = e.get("timestamp")
            break

    has_human_engagement = False
    if bot_first_activity_ts is not None:
        for e in events:
            ts = e.get("timestamp", "")
            if ts <= bot_first_activity_ts:
                continue
            actor = e.get("actor") or ""
            if not actor or actor.lower() == bot_lower:
                continue
            if is_bot_username(actor):
                continue
            etype = e.get("event_type", "")
            if etype in _COMMENT_EVENT_TYPES or etype == "commit":
                has_human_engagement = True
                break

    return {
        "has_human_engagement": has_human_engagement,
    }


def compute_and_serialize(
    assembled: dict[str, Any],
    chatbot_username: str,
) -> str:
    """Compute quality signals and return as a JSON string."""
    return json.dumps(compute_quality_signals(assembled, chatbot_username))
