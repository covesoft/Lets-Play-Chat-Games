"""Harmless stand-ins through the private list, evaluator, and live adapter."""

from dataclasses import replace
from datetime import timedelta
from random import Random

import pytest
from test_discord_ordinary_loop import next_offer, posts, settle
from test_discord_runtime import activated, event, opened, service
from test_evaluation import NOW, POSITION, message
from test_reboot_recovery import RecoveryTransport, fire, reading

from lets_play.cli import main
from lets_play.discord_adapter.runtime import OPENING_CARDS, ORDINARY_CARDS
from lets_play.new_rules.compatibility import Rejection, assess_state, replacement_targets
from lets_play.new_rules.contracts import Deadline, DeadlineKind, Outcome, RuleInstance
from lets_play.new_rules.evaluation import EvaluationContext, evaluate_message
from lets_play.new_rules.hands import build_hand
from lets_play.new_rules.profanity import MAX_LIST_BYTES, ProfanityList, load_profanity_list
from lets_play.new_rules.text_rules import RuleConfigurationError
from lets_play.storage.offer_codec import encode_hand


@pytest.mark.parametrize(
    ("text", "outcome"),
    [
        ("A TURNIP and a turnip!", Outcome.FAIL),
        ("a purple—carrot", Outcome.FAIL),
        ("a purple 🥕 carrot", Outcome.PASS),
        ("a purple <@123> carrot", Outcome.PASS),
        ("turnips and rutabaga", Outcome.PASS),
        ("**turnip**", Outcome.FAIL),
        ("`turnip`", Outcome.FAIL),
        ("-# turnip", None),
        (None, Outcome.NOT_APPLICABLE),
    ],
)
def test_gameplay_uses_normalized_words_and_counts_once(text, outcome):
    profanity = ProfanityList("turnip\nPurple Carrot\n")
    active = EvaluationContext(POSITION, (profanity.instantiate("rule"),), profanity=profanity)
    decision = evaluate_message(message(text), active)
    assert [item.outcome for item in decision.evaluations] == ([outcome] if outcome else [])
    assert decision.score.raw_faults == int(outcome is Outcome.FAIL)
    assert "turnip" not in repr((profanity, active, decision)).lower()
    if outcome is Outcome.FAIL:
        assert decision.evaluations[0].reason_code == "profanity_forbidden"
    assert not evaluate_message(message(text), EvaluationContext(POSITION, ())).score.raw_faults


@pytest.mark.parametrize(
    "data", [b"", b"\xff", b"<@123>", b"**turnip**", b"x" * (MAX_LIST_BYTES + 1)]
)
def test_unusable_private_files_disable_the_card(tmp_path, data):
    path = tmp_path / "gameplay.txt"
    assert load_profanity_list(path) is None
    path.write_bytes(data)
    assert load_profanity_list(path) is None


def test_config_check_reports_readiness_without_exposing_path_or_list(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "PRIVATE_INPUT_LOCATION.txt"
    monkeypatch.setenv("CHATGAMES_PROFANITY_LIST_PATH", str(path))
    assert main(["--check-config"], base_dir=tmp_path) == 0
    assert "unavailable" in capsys.readouterr().out
    path.write_bytes(b"\xef\xbb\xbfPrivate-Canary\nPurple Carrot\n\nPURPLE-CARROT\n")
    assert main(["--check-config"], base_dir=tmp_path) == 0
    output = capsys.readouterr().out
    assert "private gameplay list loaded (unique entries: 2)." in output
    assert "PRIVATE_INPUT_LOCATION" not in output and "Private-Canary" not in output
    assert (
        load_profanity_list(path).revision
        == ProfanityList("PURPLE-CARROT\nprivate canary\nprivate canary").revision
    )
    assert not list(tmp_path.glob("*.sqlite3"))


def test_dependency_is_pinned_and_only_available_in_later_hands():
    profanity = ProfanityList("turnip")
    context = EvaluationContext(POSITION, (), profanity=profanity)
    kwargs = dict(
        offer_id="offer",
        picker_id=303,
        deadline=Deadline("token", DeadlineKind.CHOICE, NOW),
        rng=Random(1),
        available_templates=frozenset({"NR032", "NR004", "NR006"}),
    )
    assert "NR032" in ORDINARY_CARDS and "NR032" not in OPENING_CARDS
    assert build_hand(replace(context, profanity=None), **kwargs).hand is None
    hand = build_hand(context, **kwargs).hand
    assert "turnip" not in encode_hand(hand)
    incoming = next(o.incoming for o in hand.options if o.incoming.card_id == "NR032")
    assert dict(incoming.parameters) == {"list_revision": profanity.revision}
    for other in (None, ProfanityList("rutabaga")):
        active = replace(context, active_instances=(incoming,), profanity=other)
        with pytest.raises(RuleConfigurationError, match="unavailable"):
            evaluate_message(message("harmless"), active)


def test_themes_and_vocabulary_cap_use_the_real_predicate():
    profanity = ProfanityList("hello\nbright\ngreen\nwe\nour\nturnip")
    rule = profanity.instantiate("language")
    royal_court = (RuleInstance("decree", "NR037"), RuleInstance("royal", "NR035"), rule)
    complaint_department = (
        RuleInstance("please", "NR033"),
        RuleInstance("question", "NR010"),
        rule,
    )
    for instances in (royal_court, complaint_department, (rule, RuleInstance("long", "NR018"))):
        assert assess_state(instances, profanity=profanity).legal
        assert not assess_state(instances).legal
    assert "us" in assess_state(royal_court, profanity=profanity).witness.text
    blocked = ProfanityList("we\nour\nus\nplease")
    for instances in (royal_court, complaint_department):
        assert (
            assess_state(
                (*instances[:-1], blocked.instantiate("language")), profanity=blocked
            ).rejection
            is Rejection.NO_WITNESS
        )
    active = (RuleInstance("echo", "NR022"), RuleInstance("sorry", "NR025"))
    assert replacement_targets(active, rule) == ("echo", "sorry")


async def offer_language(runtime):
    route = await opened(runtime)
    opening = await activated(runtime, route)
    assert all(i.card_id != "NR032" for i in opening.context.active_instances)
    templates = frozenset({"NR004", "NR007", "NR032"})
    await runtime.submit(route, "configure", templates=templates)
    await posts(runtime, route)
    offer = await next_offer(runtime, route)
    choices = [o.incoming.card_id for o in offer.offer.hand.options]
    assert frozenset(choices) == templates
    return route, offer, choices.index("NR032")


async def select_language(runtime):
    route, offer, index = await offer_language(runtime)
    assert (
        await runtime.submit(
            route,
            "choose",
            actor=offer.offer.hand.picker_id,
            message_id=offer.panel_id,
            index=index,
        )
        == "selected"
    )
    await settle(runtime)
    return route, await runtime.state(route.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("cut_over", [False, True], ids=["announced", "active"])
async def test_reopen_pauses_affected_game_and_resumes_with_matching_revision(
    tmp_path, monkeypatch, cut_over
):
    profanity = ProfanityList("turnip")
    async with service(tmp_path, monkeypatch, profanity=profanity) as runtime:
        route, selected = await select_language(runtime)
        runtime.clock.at = selected.offer.effective_at
        if cut_over:
            await runtime.submit(route, "event", event=event(runtime, route, 9000, "Turnip over"))
            await settle(runtime)
            assert runtime.transport.markers == [9000]
        original = await runtime.state(route.session_id)
    for other in (None, ProfanityList("rutabaga")):
        transport = RecoveryTransport()
        async with service(tmp_path, monkeypatch, profanity=other, transport=transport) as runtime:
            await settle(runtime)
            paused = await runtime.state(route.session_id)
            assert paused.context.position.lifecycle == "paused"
            assert route in transport.pauses
            assert not transport.recoveries
            await runtime.submit(route, "event", event=event(runtime, route, 9001, "Turnip over"))
            assert (await runtime.state(route.session_id)).scores == original.scores
            if not cut_over:
                choice = next(o for o in paused.offer.hand.options if o.incoming.card_id == "NR032")
                assert choice.completions[0].witness is None
    async with service(tmp_path, monkeypatch, profanity=ProfanityList("TURNIP")) as runtime:
        saved = await reading(runtime, route)
        await fire(runtime, route, saved.recovery_resume_at + timedelta(seconds=30))
        resumed = await runtime.state(route.session_id)
        assert resumed.recovery_id is None
        assert resumed.context.position.lifecycle == "playing"
        assert {i.card_id for i in resumed.context.active_instances} == {"NR034", "NR032"}
        await runtime.submit(
            route, "event", event=event(runtime, route, 9002, "turnip turnip over")
        )
        await settle(runtime)
        assert runtime.transport.markers == [9002]
    assert b"turnip" not in (tmp_path / "game.sqlite3").read_bytes().lower()


@pytest.mark.asyncio
async def test_missing_list_retires_an_unchosen_offer_and_old_rules_resume(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch, profanity=ProfanityList("turnip")) as runtime:
        route, original, _ = await offer_language(runtime)
    async with service(tmp_path, monkeypatch) as runtime:
        saved = await reading(runtime, route)
        await fire(runtime, route, saved.recovery_resume_at)
        resumed = await runtime.state(route.session_id)
        assert resumed.recovery_id is None
        assert resumed.context.position.lifecycle == "playing"
        assert resumed.context.position.round_id == original.context.position.round_id
        assert resumed.context.active_instances == original.context.active_instances
        assert resumed.offer is None or all(
            o.incoming.card_id != "NR032" for o in resumed.offer.hand.options
        )
        await runtime.submit(route, "event", event=event(runtime, route, 9003, "Turnip over"))
        await settle(runtime)
        async with runtime.store.transaction(write=False) as unit:
            assert (await unit.get_score(route.session_id, 606)).ordinary_quality == 1
        assert not runtime.transport.markers
