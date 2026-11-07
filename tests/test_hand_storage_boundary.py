"""A pure legal result can share a SQLite owner/score transaction.

This is not a publication, persisted-offer, round-resolution or award service.
"""

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from random import Random

import pytest

from lets_play.new_rules.contracts import (
    Deadline,
    DeadlineKind,
    EventEnvelope,
    EventKind,
    Lifecycle,
    MessageEvent,
    RuleInstance,
    SessionPosition,
    SessionRoute,
)
from lets_play.new_rules.evaluation import EvaluationContext, evaluate_message
from lets_play.new_rules.hands import ChoiceStatus, build_hand, revalidate_choice
from lets_play.storage.game_codec import IncompatibleState
from lets_play.storage.game_store import ApplyStatus, open_game_store

NOW = datetime(2026, 11, 7, tzinfo=UTC)
ROUTE = SessionRoute(101, 202, "session")
POSITION = SessionPosition(ROUTE, Lifecycle.PLAYING, round_id="round-one", ruleset_version=1)


def decision(ctx, source, text):
    envelope = EventEnvelope(ROUTE, EventKind.MESSAGE_CREATE, source, 303, NOW, NOW, 1)
    return evaluate_message(MessageEvent(envelope, text), ctx)


@pytest.mark.asyncio
async def test_legal_replacement_and_new_owner_score_compose_atomically(tmp_path):
    ctx = EvaluationContext(
        POSITION, (RuleInstance("brief", "NR017"), RuleInstance("over", "NR034"))
    )
    hand = build_hand(
        ctx,
        offer_id="offer",
        picker_id=303,
        deadline=Deadline("choice", DeadlineKind.CHOICE, NOW + timedelta(seconds=60)),
        rng=Random(0),
        available_templates=frozenset({"NR004", "NR006", "NR019"}),
    ).hand
    option = next(item for item in hand.options if item.incoming.card_id == "NR019")
    prepared = revalidate_choice(hand, ctx, option.incoming.instance_id, now=NOW, actor_id=303)
    assert prepared.status is ChoiceStatus.READY
    incoming = EvaluationContext(
        replace(POSITION, round_id="round-two", ruleset_version=2, revision=1),
        prepared.completion.instances,
    )
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        async with store.transaction() as unit:
            await unit.insert_session(ctx)
        assert await store.apply(decision(ctx, "old-source", "hello over")) is ApplyStatus.APPLIED
        async with store.transaction() as unit:
            assert await unit.set_owner(POSITION, incoming)
            assert (
                await unit.apply(decision(incoming, "new-source", "two over"))
                is ApplyStatus.APPLIED
            )
        assert (
            await store.apply(decision(ctx, "stale-source", "hello over"))
            is ApplyStatus.STALE_OWNER
        )
        async with store.transaction(write=False) as unit:
            total = await unit.get_score("session", 303)
            assert total.ordinary_quality == Fraction(3, 2) and total.actual_percent == 75
            assert (
                await unit.get_score("session", 303, round_id="round-one")
            ).actual_percent == 100
            assert (await unit.get_score("session", 303, round_id="round-two")).actual_percent == 50
            assert await unit.get_context("session") == incoming


@pytest.mark.asyncio
async def test_old_subset_version_requires_explicit_recovery_not_reinterpretation(tmp_path):
    path = tmp_path / "game.sqlite3"
    ctx = EvaluationContext(POSITION, (RuleInstance("over", "NR034"),))
    async with open_game_store(path) as store:
        async with store.transaction() as unit:
            await unit.insert_session(ctx)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("UPDATE nr_sessions SET evaluator_version='representatives-1'")
        with pytest.raises(IncompatibleState, match="explicit recovery"):
            await store.apply(decision(ctx, "source", "over"))
