from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.rp.content import (
    SCENARIO_SNAPSHOT_SCHEMA_VERSION,
    WORLD_SNAPSHOT_SCHEMA_VERSION,
    ScenarioSnapshot,
    WorldSnapshot,
)
from app.rp.memory import (
    RP_MEMORY_SCHEMA_VERSION,
    RPAssetsAndRulesMemory,
    RPCharactersMemory,
    RPChronologyAndHooksMemory,
    RPMemoryFact,
    RPSituationMemory,
    RPStoryMemoryRecord,
    RPStoryMemorySnapshot,
    RPThreadsMemory,
)
from app.rp.narrator import (
    RPNarratorPrompt,
    RPNarratorPromptBuilder,
    RPNarratorService,
    RPNarratorUnavailable,
    RPPromptBudgetExceeded,
    RPPromptLimits,
    select_raw_turns,
)
from app.rp.turn_engine import (
    RPMemoryIdempotencyConflict,
    RPMemoryVersionConflict,
    RPPartyVersionConflict,
    RPTurn,
    RPTurnEngine,
)


class RecordingNarrator:
    def __init__(self, *results: str | Exception):
        self.results = list(results)
        self.prompts: list[RPNarratorPrompt] = []

    async def complete(self, prompt: RPNarratorPrompt) -> str:
        self.prompts.append(prompt)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class RacingNarrator:
    def __init__(self):
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, prompt: RPNarratorPrompt) -> str:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return "Единственный конкурентный ответ."


def _party_source() -> dict[str, WorldSnapshot | ScenarioSnapshot]:
    world = WorldSnapshot(
        schema_version=WORLD_SNAPSHOT_SCHEMA_VERSION,
        world_id="day-watch-moscow-v2",
        title="WORLD_TITLE_TOKEN",
        language="ru",
        premise="WORLD_PREMISE_TOKEN",
        canon=("WORLD_CANON_TOKEN",),
        setting_rules="WORLD_RULE_TOKEN",
        characters="WORLD_CHARACTER_TOKEN",
        relationship_ontology={"axes": ["trust"]},
        seed_lore_cards=({"cards": [{"id": "WORLD_LORE_TOKEN"}]},),
    )
    scenario = ScenarioSnapshot(
        schema_version=SCENARIO_SNAPSHOT_SCHEMA_VERSION,
        scenario_id="test-scenario",
        title="Scenario",
        world_id=world.world_id,
        source="preset",
        player_role="SCENARIO_PLAYER_TOKEN",
        style="SCENARIO_STYLE_TOKEN",
        format="plain_scene_text",
        difficulty=None,
        detail_level="default",
        narrator_system="SCENARIO_SYSTEM_TOKEN",
        narrator_note="SCENARIO_NOTE_TOKEN",
        opening="SCENARIO_OPENING_TOKEN",
        initial_state={
            "player": {"private": "SCENARIO_STATE_NOT_PROMPTED"},
            "characters": {"npc-one": {}},
            "factions": {},
            "locations": {},
            "relationships": {},
        },
        active_character_ids=("npc-one",),
        starting_relationships={},
    )
    return {"world_snapshot": world, "scenario_snapshot": scenario}


def _create_engine(tmp_path: Path, party_id: str = "party-one") -> RPTurnEngine:
    engine = RPTurnEngine(tmp_path / "rp-clean.db")
    engine.create_party(owner_user_id="owner-one", party_id=party_id, **_party_source())
    return engine


def _snapshot(
    observed: int,
    coverage: int | dict[str, int],
    *,
    memory_text: str | None = None,
) -> RPStoryMemorySnapshot:
    values = (
        {key: coverage for key in (
            "situation",
            "threads",
            "characters",
            "assets_and_rules",
            "chronology_and_hooks",
        )}
        if isinstance(coverage, int)
        else coverage
    )
    fact = (
        RPMemoryFact(
            fact_id="party.memory.fact",
            text=memory_text,
            authority="inference",
            source_turn_versions=(1,),
        )
        if memory_text is not None
        else None
    )
    return RPStoryMemorySnapshot(
        schema_version=RP_MEMORY_SCHEMA_VERSION,
        observed_through_version=observed,
        situation=RPSituationMemory(
            coverage=values["situation"],
            status="fresh",
            current_situation=fact,
        ),
        threads=RPThreadsMemory(coverage=values["threads"], status="fresh"),
        characters=RPCharactersMemory(
            coverage=values["characters"], status="fresh"
        ),
        assets_and_rules=RPAssetsAndRulesMemory(
            coverage=values["assets_and_rules"], status="fresh"
        ),
        chronology_and_hooks=RPChronologyAndHooksMemory(
            coverage=values["chronology_and_hooks"], status="fresh"
        ),
    )


def _turns(count: int) -> tuple[RPTurn, ...]:
    return tuple(
        RPTurn(
            id=version,
            party_id="party-one",
            turn_kind="narrative",
            request_id=f"request-{version}",
            idempotency_key=f"key-{version}",
            expected_version=version - 1,
            committed_version=version,
            player_text=f"PARTY_PLAYER_{version}",
            narrator_text=f"PARTY_NARRATOR_{version}",
            created_at=version,
        )
        for version in range(1, count + 1)
    )


def test_memory_fact_keeps_bounded_provenance_for_a_long_party() -> None:
    fact = RPMemoryFact(
        fact_id="long.party.provenance",
        text="Факт сохраняет проверяемые источники длинной партии.",
        authority="inference",
        source_turn_versions=tuple(range(1, 129)),
    )

    assert len(fact.source_turn_versions) == 128
    assert len(
        RPMemoryFact(
            fact_id="compact.memory.fact",
            text="x" * 1_024,
            authority="inference",
            source_turn_versions=(1,),
        ).text
    ) == 1_024
    with pytest.raises(ValueError):
        RPMemoryFact(
            fact_id="oversized.memory.fact",
            text="x" * 1_025,
            authority="inference",
            source_turn_versions=(1,),
        )
    with pytest.raises(ValueError):
        RPMemoryFact(
            fact_id="too.long.party.provenance",
            text="Превышенный внутренний bound отклоняется.",
            authority="inference",
            source_turn_versions=tuple(range(1, 130)),
        )


def _record(snapshot: RPStoryMemorySnapshot, record_id: int) -> RPStoryMemoryRecord:
    return RPStoryMemoryRecord(
        id=record_id,
        party_id="party-one",
        revision=record_id,
        base_snapshot_id=record_id - 1 or None,
        update_id=f"memory-{record_id}",
        snapshot=snapshot,
        created_at=record_id,
    )


def test_provider_failure_and_blank_output_leave_no_raw_and_retry_once(
    tmp_path: Path,
) -> None:
    engine = _create_engine(tmp_path)
    narrator = RecordingNarrator(RuntimeError("transport failed"), "  Мир отвечает.  ", " ")
    service = RPNarratorService(engine, narrator)
    arguments = {
        "owner_user_id": "owner-one",
        "party_id": "party-one",
        "request_id": "request-one",
        "idempotency_key": "key-one",
        "expected_version": 0,
        "player_text": "  Я остаюсь.  ",
    }

    with pytest.raises(RPNarratorUnavailable) as failed:
        asyncio.run(service.narrate_turn(**arguments))
    assert failed.value.retryable is True
    assert failed.value.player_text == "  Я остаюсь.  "
    assert engine.list_turns(owner_user_id="owner-one", party_id="party-one") == ()
    assert engine.get_party(owner_user_id="owner-one", party_id="party-one").current_version == 0

    committed = asyncio.run(service.narrate_turn(**arguments))
    replayed = asyncio.run(service.narrate_turn(**arguments))
    assert replayed == committed
    assert committed.player_text == "  Я остаюсь.  "
    assert committed.narrator_text == "  Мир отвечает.  "
    assert len(narrator.prompts) == 2

    with pytest.raises(RPNarratorUnavailable):
        asyncio.run(
            service.narrate_turn(
                **{
                    **arguments,
                    "request_id": "request-two",
                    "idempotency_key": "key-two",
                    "expected_version": 1,
                    "player_text": "Следующее действие.",
                }
            )
        )
    assert engine.get_party(owner_user_id="owner-one", party_id="party-one").current_version == 1


def test_concurrent_exact_retry_returns_the_single_committed_turn(tmp_path: Path) -> None:
    engine = _create_engine(tmp_path)
    narrator = RacingNarrator()
    first_service = RPNarratorService(engine, narrator)
    second_service = RPNarratorService(RPTurnEngine(engine.sqlite_path), narrator)
    arguments = {
        "owner_user_id": "owner-one",
        "party_id": "party-one",
        "request_id": "request-one",
        "idempotency_key": "key-one",
        "expected_version": 0,
        "player_text": "Я действую один раз.",
    }

    async def race() -> tuple[RPTurn, RPTurn]:
        first_task = asyncio.create_task(first_service.narrate_turn(**arguments))
        await narrator.started.wait()
        second_task = asyncio.create_task(second_service.narrate_turn(**arguments))
        await asyncio.sleep(0.02)
        narrator.release.set()
        first, second = await asyncio.gather(first_task, second_task)
        return first, second

    first, second = asyncio.run(race())

    assert narrator.calls == 1
    assert first == second
    assert engine.list_turns(owner_user_id="owner-one", party_id="party-one") == (
        first,
    )


def test_concurrent_different_request_fails_before_second_provider_call(
    tmp_path: Path,
) -> None:
    engine = _create_engine(tmp_path)
    narrator = RacingNarrator()
    first_service = RPNarratorService(engine, narrator)
    second_service = RPNarratorService(RPTurnEngine(engine.sqlite_path), narrator)

    async def race() -> RPTurn:
        first_task = asyncio.create_task(
            first_service.narrate_turn(
                owner_user_id="owner-one",
                party_id="party-one",
                request_id="request-one",
                idempotency_key="key-one",
                expected_version=0,
                player_text="Первое действие.",
            )
        )
        await narrator.started.wait()
        with pytest.raises(RPPartyVersionConflict, match="in-flight narration"):
            await second_service.narrate_turn(
                owner_user_id="owner-one",
                party_id="party-one",
                request_id="request-two",
                idempotency_key="key-two",
                expected_version=0,
                player_text="Конкурирующее действие.",
            )
        narrator.release.set()
        return await first_task

    committed = asyncio.run(race())
    assert narrator.calls == 1
    assert engine.list_turns(owner_user_id="owner-one", party_id="party-one") == (
        committed,
    )


def test_opening_is_one_assistant_only_raw_unit(tmp_path: Path) -> None:
    engine = _create_engine(tmp_path)
    narrator = RecordingNarrator("Открывающая сцена.", "Продолжение сцены.")
    service = RPNarratorService(engine, narrator)

    opening = asyncio.run(
        service.narrate_opening(
            owner_user_id="owner-one",
            party_id="party-one",
            request_id="opening-request",
            idempotency_key="opening-key",
        )
    )
    turn = asyncio.run(
        service.narrate_turn(
            owner_user_id="owner-one",
            party_id="party-one",
            request_id="turn-request",
            idempotency_key="turn-key",
            expected_version=1,
            player_text="Я подхожу ближе.",
        )
    )

    assert opening.turn_kind == "opening_scene"
    assert opening.player_text == ""
    assert turn.turn_kind == "narrative"
    assert narrator.prompts[1].raw_turn_versions == (1,)
    opening_raw = [
        message
        for message in narrator.prompts[1].messages
        if message.block_id == "raw:1"
    ]
    assert [(message.role, message.content) for message in opening_raw] == [
        ("assistant", "Открывающая сцена.")
    ]


def test_story_memory_is_append_only_monotonic_and_party_scoped(tmp_path: Path) -> None:
    engine = _create_engine(tmp_path)
    for version in range(3):
        engine.commit_turn(
            owner_user_id="owner-one",
            party_id="party-one",
            request_id=f"request-{version}",
            idempotency_key=f"key-{version}",
            expected_version=version,
            player_text=f"Игрок {version}",
            narrator_text=f"Нарратор {version}",
        )
    first_snapshot = _snapshot(
        3,
        {
            "situation": 2,
            "threads": 2,
            "characters": 2,
            "assets_and_rules": 2,
            "chronology_and_hooks": 1,
        },
    )
    first = engine.append_story_memory(
        owner_user_id="owner-one",
        party_id="party-one",
        expected_base_snapshot_id=None,
        update_id="memory-one",
        snapshot=first_snapshot,
    )
    second_snapshot = _snapshot(3, 2)
    second = engine.append_story_memory(
        owner_user_id="owner-one",
        party_id="party-one",
        expected_base_snapshot_id=first.id,
        update_id="memory-two",
        snapshot=second_snapshot,
    )

    assert first.snapshot.safe_coverage == 1
    assert second.snapshot.safe_coverage == 2
    assert engine.latest_story_memory(
        owner_user_id="owner-one", party_id="party-one"
    ) == second
    engine.create_party(
        owner_user_id="owner-one", party_id="party-two", **_party_source()
    )
    assert engine.latest_story_memory(
        owner_user_id="owner-one", party_id="party-two"
    ) is None
    assert engine.append_story_memory(
        owner_user_id="owner-one",
        party_id="party-one",
        expected_base_snapshot_id=first.id,
        update_id="memory-two",
        snapshot=second_snapshot,
    ) == second
    with pytest.raises(RPMemoryVersionConflict):
        engine.append_story_memory(
            owner_user_id="owner-one",
            party_id="party-one",
            expected_base_snapshot_id=first.id,
            update_id="stale-base",
            snapshot=second_snapshot,
        )
    with pytest.raises(ValueError, match="cannot regress"):
        engine.append_story_memory(
            owner_user_id="owner-one",
            party_id="party-one",
            expected_base_snapshot_id=second.id,
            update_id="regression",
            snapshot=first_snapshot,
        )
    with pytest.raises(RPMemoryIdempotencyConflict):
        engine.append_story_memory(
            owner_user_id="owner-one",
            party_id="party-one",
            expected_base_snapshot_id=first.id,
            update_id="memory-two",
            snapshot=first_snapshot,
        )
    stale_advanced = second_snapshot.model_copy(
        update={"threads": RPThreadsMemory(coverage=3, status="stale")}
    )
    with pytest.raises(ValueError, match="stale memory section"):
        engine.append_story_memory(
            owner_user_id="owner-one",
            party_id="party-one",
            expected_base_snapshot_id=second.id,
            update_id="stale-advanced",
            snapshot=stale_advanced,
        )
    stale_rewritten = second_snapshot.model_copy(
        update={
            "situation": RPSituationMemory(
                coverage=2,
                status="stale",
                current_situation=RPMemoryFact(
                    fact_id="rewritten.stale.fact",
                    text="Неподтверждённая замена.",
                    authority="inference",
                    source_turn_versions=(1,),
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="must preserve its base content"):
        engine.append_story_memory(
            owner_user_id="owner-one",
            party_id="party-one",
            expected_base_snapshot_id=second.id,
            update_id="stale-rewritten",
            snapshot=stale_rewritten,
        )

    with sqlite3.connect(engine.sqlite_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE rp_story_memory_snapshots SET update_id = 'changed' WHERE id = ?",
                (second.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM rp_story_memory_snapshots WHERE id = ?", (second.id,)
            )


def test_failed_or_oversized_memory_cannot_hide_required_raw(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot claim safe coverage"):
        RPThreadsMemory(coverage=1, status="failed")

    engine = _create_engine(tmp_path)
    engine.commit_turn(
        owner_user_id="owner-one",
        party_id="party-one",
        request_id="seed-request",
        idempotency_key="seed-key",
        expected_version=0,
        player_text="Я запоминаю.",
        narrator_text="Память закрепляется.",
    )
    oversized_facts = tuple(
        RPMemoryFact(
            fact_id=f"oversized.fact.{index}",
            text="x" * 1_024,
            authority="inference",
            source_turn_versions=(1,),
        )
        for index in range(25)
    )
    oversized = RPStoryMemorySnapshot(
        schema_version=RP_MEMORY_SCHEMA_VERSION,
        observed_through_version=1,
        situation=RPSituationMemory(
            coverage=1, status="fresh", canon=oversized_facts
        ),
        threads=RPThreadsMemory(coverage=1, status="fresh"),
        characters=RPCharactersMemory(coverage=1, status="fresh"),
        assets_and_rules=RPAssetsAndRulesMemory(coverage=1, status="fresh"),
        chronology_and_hooks=RPChronologyAndHooksMemory(
            coverage=1, status="fresh"
        ),
    )

    with pytest.raises(ValueError, match="exceeds its prompt budget"):
        engine.append_story_memory(
            owner_user_id="owner-one",
            party_id="party-one",
            expected_base_snapshot_id=None,
            update_id="oversized-memory",
            snapshot=oversized,
        )
    assert engine.latest_story_memory(
        owner_user_id="owner-one", party_id="party-one"
    ) is None


def test_raw_anchor_safe_coverage_stable_prefix_and_source_ownership(
    tmp_path: Path,
) -> None:
    engine = _create_engine(tmp_path)
    party = engine.get_party(owner_user_id="owner-one", party_id="party-one")
    turns_66 = _turns(66)

    assert tuple(
        turn.committed_version
        for turn in select_raw_turns(
            turns_66[:57], safe_coverage=57, window_turns=50, anchor_turns=8
        )
    ) == tuple(range(1, 58))
    assert tuple(
        turn.committed_version
        for turn in select_raw_turns(
            turns_66[:58], safe_coverage=58, window_turns=50, anchor_turns=8
        )
    ) == tuple(range(9, 59))
    assert tuple(
        turn.committed_version
        for turn in select_raw_turns(
            turns_66, safe_coverage=66, window_turns=50, anchor_turns=8
        )
    ) == tuple(range(17, 67))
    lagging = select_raw_turns(
        turns_66, safe_coverage=10, window_turns=50, anchor_turns=8
    )
    assert tuple(turn.committed_version for turn in lagging) == tuple(range(11, 67))

    builder = RPNarratorPromptBuilder(
        RPPromptLimits(raw_window_turns=2, raw_anchor_turns=2)
    )
    at_two = builder.build_turn(
        party=party,
        turns=turns_66[:2],
        memory=_record(_snapshot(2, 2, memory_text="PARTY_MEMORY_TOKEN"), 1),
        player_text="CURRENT_ACTION_TWO",
    )
    at_three = builder.build_turn(
        party=party,
        turns=turns_66[:3],
        memory=_record(_snapshot(3, 3, memory_text="CHANGED_MEMORY_TOKEN"), 2),
        player_text="CURRENT_ACTION_THREE",
    )
    at_four = builder.build_turn(
        party=party,
        turns=turns_66[:4],
        memory=_record(_snapshot(4, 4, memory_text="CHANGED_AGAIN_TOKEN"), 3),
        player_text="CURRENT_ACTION_FOUR",
    )

    assert at_two.stable_prefix_hash == at_three.stable_prefix_hash
    assert at_three.stable_prefix_hash != at_four.stable_prefix_hash
    current_actions = [
        message
        for message in at_three.messages
        if message.block_id == "current_player_action"
    ]
    assert [(message.role, message.content) for message in current_actions] == [
        ("user", "CURRENT_ACTION_THREE")
    ]
    contents = {
        block_id: "\n".join(
            message.content for message in at_two.messages if message.block_id == block_id
        )
        for block_id in {
            "world",
            "scenario_experience",
            "story_memory",
            "current_player_action",
        }
    }
    assert "WORLD_CANON_TOKEN" in contents["world"]
    assert "SCENARIO_SYSTEM_TOKEN" not in contents["world"]
    assert "SCENARIO_SYSTEM_TOKEN" in contents["scenario_experience"]
    assert "WORLD_CANON_TOKEN" not in contents["scenario_experience"]
    assert "PARTY_MEMORY_TOKEN" in contents["story_memory"]
    assert "SCENARIO_STATE_NOT_PROMPTED" not in "\n".join(
        message.content for message in at_two.messages
    )


@pytest.mark.parametrize(
    "layer",
    (
        "gateway_rules",
        "world",
        "scenario",
        "player",
        "world_rules",
        "memory",
        "lore",
        "relationships",
        "narrator_note",
        "opening",
    ),
)
def test_each_protected_prompt_layer_fails_closed_before_provider_or_commit(
    tmp_path: Path, layer: str
) -> None:
    engine = _create_engine(tmp_path)
    if layer == "memory":
        engine.append_story_memory(
            owner_user_id="owner-one",
            party_id="party-one",
            expected_base_snapshot_id=None,
            update_id="memory-zero",
            snapshot=_snapshot(0, 0),
        )
    narrator = RecordingNarrator("Этот ответ не должен быть вызван.")
    limit_field = "relationship" if layer == "relationships" else layer
    service = RPNarratorService(
        engine,
        narrator,
        RPNarratorPromptBuilder(RPPromptLimits(**{f"{limit_field}_chars": 1})),
    )

    with pytest.raises(RPPromptBudgetExceeded) as overflow:
        if layer == "opening":
            asyncio.run(
                service.narrate_opening(
                    owner_user_id="owner-one",
                    party_id="party-one",
                    request_id="opening-request",
                    idempotency_key="opening-key",
                )
            )
        else:
            asyncio.run(
                service.narrate_turn(
                    owner_user_id="owner-one",
                    party_id="party-one",
                    request_id="request-one",
                    idempotency_key="key-one",
                    expected_version=0,
                    player_text="Я действую.",
                )
            )

    assert overflow.value.layer == layer
    assert narrator.prompts == []
    assert engine.list_turns(owner_user_id="owner-one", party_id="party-one") == ()


def test_persisted_raw_and_latest_memory_feed_the_narrator_boundary(
    tmp_path: Path,
) -> None:
    engine = _create_engine(tmp_path)
    engine.commit_turn(
        owner_user_id="owner-one",
        party_id="party-one",
        request_id="seed-request",
        idempotency_key="seed-key",
        expected_version=0,
        player_text="Я запоминаю знак.",
        narrator_text="Знак остаётся на двери.",
    )
    first = engine.append_story_memory(
        owner_user_id="owner-one",
        party_id="party-one",
        expected_base_snapshot_id=None,
        update_id="memory-one",
        snapshot=_snapshot(1, 1, memory_text="OLD_MEMORY_TOKEN"),
    )
    engine.append_story_memory(
        owner_user_id="owner-one",
        party_id="party-one",
        expected_base_snapshot_id=first.id,
        update_id="memory-two",
        snapshot=_snapshot(1, 1, memory_text="LATEST_MEMORY_TOKEN"),
    )
    narrator = RecordingNarrator("Сцена продолжается.")
    service = RPNarratorService(engine, narrator)

    committed = asyncio.run(
        service.narrate_turn(
            owner_user_id="owner-one",
            party_id="party-one",
            request_id="live-request",
            idempotency_key="live-key",
            expected_version=1,
            player_text="Я проверяю дверь.",
        )
    )

    assert committed.committed_version == 2
    assert len(narrator.prompts) == 1
    prompt = narrator.prompts[0]
    assert prompt.raw_turn_versions == (1,)
    assert prompt.safe_memory_coverage == 1
    memory_blocks = [
        message.content
        for message in prompt.messages
        if message.block_id == "story_memory"
    ]
    assert len(memory_blocks) == 1
    assert "LATEST_MEMORY_TOKEN" in memory_blocks[0]
    assert "OLD_MEMORY_TOKEN" not in memory_blocks[0]


def test_narrator_waits_for_previous_raw_jobs_then_rereads_prompt_inputs(
    tmp_path: Path,
) -> None:
    engine = _create_engine(tmp_path)
    engine.commit_turn(
        owner_user_id="owner-one",
        party_id="party-one",
        request_id="seed-request",
        idempotency_key="seed-key",
        expected_version=0,
        player_text="Я замечаю знак.",
        narrator_text="Знак остаётся на стене.",
    )
    claimed_jobs = []
    while True:
        job = engine.claim_service_job()
        if job is None:
            break
        claimed_jobs.append(job)
    assert len(claimed_jobs) == 3

    terminal = False
    history_reads = 0
    original_list_turns = engine.list_turns
    original_get_party = engine.get_party
    original_latest_memory = engine.latest_story_memory
    original_derived_context = engine.derived_context

    def checked_list_turns(**kwargs: object) -> tuple[RPTurn, ...]:
        nonlocal history_reads
        history_reads += 1
        if history_reads > 1:
            assert terminal is True
        return original_list_turns(**kwargs)  # type: ignore[arg-type]

    def checked_get_party(**kwargs: object) -> object:
        assert terminal is True
        return original_get_party(**kwargs)  # type: ignore[arg-type]

    def checked_latest_memory(**kwargs: object) -> RPStoryMemoryRecord | None:
        assert terminal is True
        return original_latest_memory(**kwargs)  # type: ignore[arg-type]

    def checked_derived_context(**kwargs: object) -> object:
        assert terminal is True
        return original_derived_context(**kwargs)  # type: ignore[arg-type]

    engine.list_turns = checked_list_turns  # type: ignore[method-assign]
    engine.get_party = checked_get_party  # type: ignore[method-assign]
    engine.latest_story_memory = checked_latest_memory  # type: ignore[method-assign]
    engine.derived_context = checked_derived_context  # type: ignore[method-assign]
    narrator = RecordingNarrator("Следующая сцена учитывает обновлённую память.")
    service = RPNarratorService(
        engine,
        narrator,
        atomic_service_enabled=True,
        derived_wait_seconds=0.5,
        derived_poll_interval=0.001,
    )

    async def exercise() -> RPTurn:
        nonlocal terminal

        async def finish_jobs() -> None:
            nonlocal terminal
            await asyncio.sleep(0.01)
            engine.append_story_memory(
                owner_user_id="owner-one",
                party_id="party-one",
                expected_base_snapshot_id=None,
                update_id="waited-memory",
                snapshot=_snapshot(1, 1, memory_text="WAITED_MEMORY_TOKEN"),
            )
            for job in claimed_jobs:
                assert job.claim_token is not None
                engine.complete_service_job(
                    job_id=job.id,
                    claim_token=job.claim_token,
                    result={"kind": job.job_type, "result": "completed"},
                )
            terminal = True

        turn_task = asyncio.create_task(
            service.narrate_turn(
                owner_user_id="owner-one",
                party_id="party-one",
                request_id="live-request",
                idempotency_key="live-key",
                expected_version=1,
                player_text="Я изучаю знак.",
            )
        )
        await asyncio.gather(turn_task, finish_jobs())
        return turn_task.result()

    committed = asyncio.run(exercise())

    assert committed.committed_version == 2
    assert history_reads >= 2
    assert narrator.prompts[0].raw_turn_versions == (1,)
    memory_block = next(
        message.content
        for message in narrator.prompts[0].messages
        if message.block_id == "story_memory"
    )
    assert "WAITED_MEMORY_TOKEN" in memory_block


def test_narrator_derived_wait_is_bounded_and_fail_open(tmp_path: Path) -> None:
    engine = _create_engine(tmp_path)
    engine.commit_turn(
        owner_user_id="owner-one",
        party_id="party-one",
        request_id="seed-request",
        idempotency_key="seed-key",
        expected_version=0,
        player_text="Я действую.",
        narrator_text="Сцена отвечает.",
    )
    service = RPNarratorService(
        engine,
        RecordingNarrator("Ход продолжается после bounded wait."),
        atomic_service_enabled=True,
        derived_wait_seconds=0.01,
        derived_poll_interval=0.001,
    )

    async def exercise() -> RPTurn:
        return await asyncio.wait_for(
            service.narrate_turn(
                owner_user_id="owner-one",
                party_id="party-one",
                request_id="live-request",
                idempotency_key="live-key",
                expected_version=1,
                player_text="Я продолжаю.",
            ),
            timeout=0.5,
        )

    committed = asyncio.run(exercise())

    assert committed.committed_version == 2
    previous_jobs = engine.service_jobs_for_source_version(
        owner_user_id="owner-one", party_id="party-one", source_version=1
    )
    assert all((job.status, job.attempts) == ("pending", 0) for job in previous_jobs)


def test_narrator_skips_derived_wait_when_atomic_service_is_disabled(
    tmp_path: Path,
) -> None:
    engine = _create_engine(tmp_path)
    engine.commit_turn(
        owner_user_id="owner-one",
        party_id="party-one",
        request_id="seed-request",
        idempotency_key="seed-key",
        expected_version=0,
        player_text="Я действую.",
        narrator_text="Сцена отвечает.",
    )

    def unexpected_wait(**_: object) -> object:
        raise AssertionError("disabled atomic service must not be polled")

    engine.service_jobs_for_source_version = unexpected_wait  # type: ignore[method-assign]
    service = RPNarratorService(
        engine,
        RecordingNarrator("Ход продолжается без ожидания service jobs."),
        atomic_service_enabled=False,
        derived_wait_seconds=60,
    )

    committed = asyncio.run(
        service.narrate_turn(
            owner_user_id="owner-one",
            party_id="party-one",
            request_id="live-request",
            idempotency_key="live-key",
            expected_version=1,
            player_text="Я продолжаю.",
        )
    )

    assert committed.committed_version == 2


def test_hard_prompt_overflow_stops_before_narrator_and_commit(tmp_path: Path) -> None:
    engine = _create_engine(tmp_path)
    narrator = RecordingNarrator("Этот ответ не должен быть вызван.")
    service = RPNarratorService(
        engine,
        narrator,
        RPNarratorPromptBuilder(RPPromptLimits(hard_input_chars=10)),
    )

    with pytest.raises(RPPromptBudgetExceeded) as overflow:
        asyncio.run(
            service.narrate_turn(
                owner_user_id="owner-one",
                party_id="party-one",
                request_id="request-one",
                idempotency_key="key-one",
                expected_version=0,
                player_text="Я действую.",
            )
        )

    assert overflow.value.layer == "hard_input"
    assert narrator.prompts == []
    assert engine.list_turns(owner_user_id="owner-one", party_id="party-one") == ()
