from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.rp.content import (
    SCENARIO_SNAPSHOT_SCHEMA_VERSION,
    WORLD_SNAPSHOT_SCHEMA_VERSION,
    ScenarioSnapshot,
    WorldSnapshot,
)
from app.rp.mechanics import (
    RPAdministratorHandler,
    RPAdministratorResult,
    RPAtomicServiceHandler,
    RPRelationshipCandidate,
    RPRelationshipResult,
    RPRuntimeLoreResult,
)
from app.rp.narrator import RPNarratorPromptBuilder
from app.rp.turn_engine import (
    RPAdministratorProposal,
    RPModelOutputRejected,
    RPPartyNotFound,
    RPServiceJob,
    RPTurnEngine,
)


def _party_source() -> dict[str, WorldSnapshot | ScenarioSnapshot]:
    world = WorldSnapshot(
        schema_version=WORLD_SNAPSHOT_SCHEMA_VERSION,
        world_id="day-watch-moscow-v2",
        title="Дневной Дозор",
        language="ru",
        premise="Москва после Великого договора.",
        canon=("Канон мира.",),
        setting_rules="Законы мира.",
        characters="npc-one: Базовый NPC.",
        relationship_ontology={
            "axes": {
                "loyalty": {"min": -100, "max": 100, "per_turn_cap": 20}
            },
            "events": {"kept_agreement": {"axis": "loyalty", "weight": 10}},
            "character_weights": {},
            "trust_mapping": {"kind": "linear", "in": [-10, 10], "out": [-40, 40]},
        },
        seed_lore_cards=({"cards": [{"id": "world-card"}]},),
    )
    scenario = ScenarioSnapshot(
        schema_version=SCENARIO_SNAPSHOT_SCHEMA_VERSION,
        scenario_id="test-scenario",
        title="Тестовый сценарий",
        world_id=world.world_id,
        source="preset",
        player_role="Новый сотрудник.",
        style="book",
        format="plain_scene_text",
        difficulty=None,
        detail_level="default",
        narrator_system="Веди сцену.",
        narrator_note="Сохраняй агентность игрока.",
        opening="Начинается смена.",
        initial_state={
            "player": {},
            "characters": {"npc-one": {"trust": 5}},
            "factions": {},
            "locations": {},
            "relationships": {
                "npc-one-to-player": {
                    "from": "npc-one",
                    "to": "player",
                    "trust": 5,
                }
            },
        },
        active_character_ids=("npc-one",),
        starting_relationships={
            "npc-one-to-player": {
                "from": "npc-one",
                "to": "player",
                "trust": 5,
            }
        },
    )
    return {"world_snapshot": world, "scenario_snapshot": scenario}


def _create_party(engine: RPTurnEngine, party_id: str) -> None:
    engine.create_party(
        owner_user_id="owner-one", party_id=party_id, **_party_source()
    )


def _commit_first_turn(engine: RPTurnEngine, party_id: str) -> None:
    engine.commit_turn(
        owner_user_id="owner-one",
        party_id=party_id,
        request_id=f"request-{party_id}",
        idempotency_key=f"key-{party_id}",
        expected_version=0,
        player_text="Я выполняю договорённость.",
        narrator_text="Свидетель кивает.",
    )


def _claim_service_job(engine: RPTurnEngine, job_type: str) -> RPServiceJob:
    while True:
        job = engine.claim_service_job()
        assert job is not None
        assert job.claim_token is not None
        if job.job_type == job_type:
            return job
        engine.complete_service_job(
            job_id=job.id,
            claim_token=job.claim_token,
            result={"kind": job.job_type, "result": "not_exercised"},
        )


class _AtomicModelFake:
    def __init__(
        self,
        *,
        relationships: RPRelationshipResult | None = None,
        lore: RPRuntimeLoreResult | None = None,
    ):
        self.relationships = relationships
        self.lore = lore

    async def extract_relationships(self, **_: object) -> RPRelationshipResult:
        assert self.relationships is not None
        return self.relationships

    async def extract_runtime_lore(self, **_: object) -> RPRuntimeLoreResult:
        assert self.lore is not None
        return self.lore

    async def update_story_memory(self, **_: object) -> object:
        raise AssertionError("story-memory model must not be called by these tests")


class _AdministratorModelFake:
    def __init__(self) -> None:
        self.reviewed_parties: list[str] = []

    async def review_party(self, *, party: object, **_: object) -> RPAdministratorResult:
        party_id = str(getattr(party, "id"))
        self.reviewed_parties.append(party_id)
        return RPAdministratorResult(
            result="suggest",
            target_slot="narrator_guidance",
            after=f"Режиссёрская поправка для {party_id}.",
        )


def test_invalid_relationship_candidate_does_not_erase_valid_sibling(
    tmp_path: Path,
) -> None:
    engine = RPTurnEngine(tmp_path / "rp-clean.db")
    _create_party(engine, "party-relationships")
    _commit_first_turn(engine, "party-relationships")
    job = _claim_service_job(engine, "relationships")
    model = _AtomicModelFake(
        relationships=RPRelationshipResult(
            candidates=(
                RPRelationshipCandidate(
                    character_id="unknown-character",
                    direction="character_to_player",
                    event_id="kept_agreement",
                    evidence_span_ids=(1,),
                ),
                RPRelationshipCandidate(
                    character_id="npc-one",
                    direction="character_to_player",
                    event_id="kept_agreement",
                    evidence_span_ids=(1,),
                ),
            )
        )
    )

    result = asyncio.run(RPAtomicServiceHandler(engine, model).handle(job))

    assert result["accepted"] == 1
    assert result["inserted"] == 1
    assert result["rejected"] == [
        {"index": 0, "reason": "unknown_character_id"}
    ]
    context = engine.derived_context(
        owner_user_id="owner-one", party_id="party-relationships"
    )
    assert len(context.relationship_causes) == 1
    assert context.relationship_causes[0].character_id == "npc-one"
    assert context.relationship_causes[0].event_id == "kept_agreement"
    assert context.relationship_causes[0].delta == 10
    prompt = RPNarratorPromptBuilder().build_turn(
        party=engine.get_party(
            owner_user_id="owner-one", party_id="party-relationships"
        ),
        turns=engine.list_turns(
            owner_user_id="owner-one", party_id="party-relationships"
        ),
        memory=None,
        player_text="Следующее действие.",
        derived=context,
    )
    assert "kept_agreement" in next(
        block.content for block in prompt.messages if block.block_id == "party_relationships"
    )
    assert '"direction":"character_to_player"' in next(
        block.content for block in prompt.messages if block.block_id == "party_relationships"
    )


def test_starting_relationships_are_visible_before_first_derived_cause(
    tmp_path: Path,
) -> None:
    engine = RPTurnEngine(tmp_path / "rp-clean.db")
    _create_party(engine, "party-seed-relationships")
    party = engine.get_party(
        owner_user_id="owner-one", party_id="party-seed-relationships"
    )

    prompt = RPNarratorPromptBuilder().build_turn(
        party=party,
        turns=(),
        memory=None,
        player_text="Начинаю.",
        derived=engine.derived_context(
            owner_user_id="owner-one", party_id="party-seed-relationships"
        ),
    )

    relationship_block = next(
        block for block in prompt.messages if block.block_id == "party_relationships"
    )
    assert '"axis":"loyalty"' in relationship_block.content
    assert '"character_id":"npc-one"' in relationship_block.content
    assert '"value":20' in relationship_block.content
    assert '"npc-one-to-player"' in relationship_block.content

    opening = RPNarratorPromptBuilder().build_opening(party=party)
    opening_relationships = next(
        block for block in opening.messages if block.block_id == "party_relationships"
    )
    assert '"npc-one-to-player"' in opening_relationships.content


def test_runtime_lore_is_persisted_as_typed_runtime_origin(tmp_path: Path) -> None:
    engine = RPTurnEngine(tmp_path / "rp-clean.db")
    _create_party(engine, "party-lore")
    _commit_first_turn(engine, "party-lore")
    job = _claim_service_job(engine, "runtime_lore")
    model = _AtomicModelFake(
        lore=RPRuntimeLoreResult(
            result="draft",
            kind="location",
            title="Архив на Арбате",
            content="В архиве хранятся протоколы наблюдения.",
            keywords=("архив", "Арбат"),
            evidence_span_ids=(2,),
        )
    )

    result = asyncio.run(RPAtomicServiceHandler(engine, model).handle(job))

    assert result["result"] == "draft"
    context = engine.derived_context(
        owner_user_id="owner-one", party_id="party-lore"
    )
    assert len(context.runtime_lore_cards) == 1
    card = context.runtime_lore_cards[0]
    assert card.kind == "location"
    assert card.origin == "runtime"
    assert card.keywords == ("архив", "Арбат")
    assert card.evidence_span_ids == (2,)
    prompt = RPNarratorPromptBuilder().build_turn(
        party=engine.get_party(owner_user_id="owner-one", party_id="party-lore"),
        turns=engine.list_turns(owner_user_id="owner-one", party_id="party-lore"),
        memory=None,
        player_text="Я ищу архив.",
        derived=context,
    )
    lore_block = next(block for block in prompt.messages if block.block_id == "lore")
    assert "Архив на Арбате" in lore_block.content
    assert '"origin":"runtime"' in lore_block.content


def test_runtime_lore_is_not_marked_successful_when_it_cannot_reach_prompt(
    tmp_path: Path,
) -> None:
    source = _party_source()
    world = source["world_snapshot"]
    assert isinstance(world, WorldSnapshot)
    oversized_world = world.model_copy(
        update={"seed_lore_cards": ({"content": "W" * 13_000},)}
    )
    engine = RPTurnEngine(tmp_path / "rp-clean.db")
    engine.create_party(
        owner_user_id="owner-one",
        party_id="party-lore-budget",
        world_snapshot=oversized_world,
        scenario_snapshot=source["scenario_snapshot"],
    )
    _commit_first_turn(engine, "party-lore-budget")
    job = _claim_service_job(engine, "runtime_lore")
    model = _AtomicModelFake(
        lore=RPRuntimeLoreResult(
            result="draft",
            kind="event",
            title="Слишком большая карточка",
            content="L" * 3_500,
            keywords=("событие",),
            evidence_span_ids=(2,),
        )
    )

    with pytest.raises(RPModelOutputRejected, match="protected Lore prompt budget"):
        asyncio.run(RPAtomicServiceHandler(engine, model).handle(job))

    assert engine.derived_context(
        owner_user_id="owner-one", party_id="party-lore-budget"
    ).runtime_lore_cards == ()


def test_relationship_jobs_are_source_ordered_and_clamped_transactionally(
    tmp_path: Path,
) -> None:
    source = _party_source()
    world = source["world_snapshot"]
    assert isinstance(world, WorldSnapshot)
    ontology = dict(world.relationship_ontology)
    ontology["axes"] = {
        "loyalty": {"min": -100, "max": 25, "per_turn_cap": 20}
    }
    bounded_world = world.model_copy(update={"relationship_ontology": ontology})
    database = tmp_path / "rp-clean.db"
    engine = RPTurnEngine(database)
    engine.create_party(
        owner_user_id="owner-one",
        party_id="party-relationship-race",
        world_snapshot=bounded_world,
        scenario_snapshot=source["scenario_snapshot"],
    )
    _commit_first_turn(engine, "party-relationship-race")
    engine.commit_turn(
        owner_user_id="owner-one",
        party_id="party-relationship-race",
        request_id="request-race-two",
        idempotency_key="key-race-two",
        expected_version=1,
        player_text="Я снова выполняю договорённость.",
        narrator_text="Свидетель снова подтверждает поступок.",
    )
    first_job = _claim_service_job(engine, "relationships")
    independent = RPTurnEngine(database)
    while skipped := independent.claim_service_job():
        assert skipped.job_type != "relationships"
        assert skipped.claim_token is not None
        independent.complete_service_job(
            job_id=skipped.id,
            claim_token=skipped.claim_token,
            result={"kind": skipped.job_type, "result": "not_exercised"},
        )
    model = _AtomicModelFake(
        relationships=RPRelationshipResult(
            candidates=(
                RPRelationshipCandidate(
                    character_id="npc-one",
                    direction="character_to_player",
                    event_id="kept_agreement",
                    evidence_span_ids=(1,),
                ),
            )
        )
    )
    first_result = asyncio.run(RPAtomicServiceHandler(engine, model).handle(first_job))
    assert first_job.claim_token is not None
    engine.complete_service_job(
        job_id=first_job.id,
        claim_token=first_job.claim_token,
        result=first_result,
    )
    second_job = independent.claim_service_job()
    assert second_job is not None
    assert second_job.job_type == "relationships"
    second_result = asyncio.run(
        RPAtomicServiceHandler(independent, model).handle(second_job)
    )

    context = engine.derived_context(
        owner_user_id="owner-one", party_id="party-relationship-race"
    )
    assert sum(cause.delta for cause in context.relationship_causes) == 5
    assert [first_result["accepted"], second_result["accepted"]] == [1, 0]
    assert any(
        item["reason"] == "relationship_range_reached"
        for item in second_result["rejected"]
    )


def test_administrator_uses_separate_handler_and_manual_owner_scoped_decisions(
    tmp_path: Path,
) -> None:
    engine = RPTurnEngine(tmp_path / "rp-clean.db")
    for party_id in ("party-accept", "party-reject"):
        _create_party(engine, party_id)
        _commit_first_turn(engine, party_id)

    model = _AdministratorModelFake()
    handler = RPAdministratorHandler(engine, model)
    proposals: dict[str, RPAdministratorProposal] = {}
    for party_id in ("party-accept", "party-reject"):
        job = engine.claim_administrator_job()
        assert job is not None
        assert job.party_id == party_id
        assert job.claim_token is not None
        proposal = asyncio.run(handler.handle(job))
        assert isinstance(proposal, RPAdministratorProposal)
        proposals[party_id] = proposal
        engine.complete_administrator_job(
            job_id=job.id, claim_token=job.claim_token
        )

    assert model.reviewed_parties == ["party-accept", "party-reject"]
    assert all(
        job.status == "pending"
        for job in engine.list_service_jobs(
            owner_user_id="owner-one", party_id="party-accept"
        )
    )

    with pytest.raises(RPPartyNotFound):
        engine.decide_administrator_proposal(
            owner_user_id="intruder",
            party_id="party-accept",
            proposal_id=proposals["party-accept"].id,
            decision="accept",
        )

    accepted = engine.decide_administrator_proposal(
        owner_user_id="owner-one",
        party_id="party-accept",
        proposal_id=proposals["party-accept"].id,
        decision="accept",
    )
    repeated = engine.decide_administrator_proposal(
        owner_user_id="owner-one",
        party_id="party-accept",
        proposal_id=proposals["party-accept"].id,
        decision="accept",
    )
    accepted_party = engine.get_party(
        owner_user_id="owner-one", party_id="party-accept"
    )
    accepted_context = engine.derived_context(
        owner_user_id="owner-one", party_id="party-accept"
    )

    assert accepted.status == repeated.status == "accepted"
    assert accepted.applied_party_version == 2
    assert accepted_party.current_version == 2
    assert accepted_context.administrator_guidance is not None
    assert accepted_context.administrator_guidance.party_version == 2
    assert (
        accepted_context.administrator_guidance.content
        == proposals["party-accept"].after_text
    )
    prompt = RPNarratorPromptBuilder().build_turn(
        party=accepted_party,
        turns=engine.list_turns(
            owner_user_id="owner-one", party_id="party-accept"
        ),
        memory=None,
        player_text="Продолжаю.",
        derived=accepted_context,
    )
    assert proposals["party-accept"].after_text in next(
        block.content
        for block in prompt.messages
        if block.block_id == "administrator_guidance"
    )

    rejected = engine.decide_administrator_proposal(
        owner_user_id="owner-one",
        party_id="party-reject",
        proposal_id=proposals["party-reject"].id,
        decision="reject",
    )
    rejected_party = engine.get_party(
        owner_user_id="owner-one", party_id="party-reject"
    )
    rejected_context = engine.derived_context(
        owner_user_id="owner-one", party_id="party-reject"
    )

    assert rejected.status == "rejected"
    assert rejected.applied_party_version is None
    assert rejected_party.current_version == 1
    assert rejected_context.administrator_guidance is None


def test_administrator_review_cannot_rebase_itself_over_an_intervening_turn(
    tmp_path: Path,
) -> None:
    engine = RPTurnEngine(tmp_path / "rp-clean.db")
    _create_party(engine, "party-race")
    _commit_first_turn(engine, "party-race")
    job = engine.claim_administrator_job()
    assert job is not None
    assert job.claim_token is not None

    class InterveningTurnModel:
        async def review_party(self, **_: object) -> RPAdministratorResult:
            engine.commit_turn(
                owner_user_id="owner-one",
                party_id="party-race",
                request_id="intervening-request",
                idempotency_key="intervening-key",
                expected_version=1,
                player_text="Я действую, пока идёт анализ.",
                narrator_text="Сцена уже изменилась.",
            )
            return RPAdministratorResult(
                result="suggest",
                target_slot="narrator_guidance",
                after="Устаревший совет.",
            )

    result = asyncio.run(
        RPAdministratorHandler(engine, InterveningTurnModel()).handle(job)
    )

    assert result == {"kind": "administrator", "result": "stale_review"}
    assert engine.list_administrator_proposals(
        owner_user_id="owner-one", party_id="party-race"
    ) == ()
    assert engine.get_party(
        owner_user_id="owner-one", party_id="party-race"
    ).current_version == 2


def test_administrator_does_not_review_an_expired_source_version(
    tmp_path: Path,
) -> None:
    engine = RPTurnEngine(tmp_path / "rp-clean.db")
    _create_party(engine, "party-expired-review")
    _commit_first_turn(engine, "party-expired-review")
    engine.commit_turn(
        owner_user_id="owner-one",
        party_id="party-expired-review",
        request_id="request-after-review-window",
        idempotency_key="key-after-review-window",
        expected_version=1,
        player_text="Я продолжаю до завершения анализа.",
        narrator_text="История уже ушла вперёд.",
    )
    job = engine.claim_administrator_job()
    assert job is not None
    assert job.source_version == 1
    assert RPTurnEngine(tmp_path / "rp-clean.db").claim_administrator_job() is None

    class MustNotReview:
        async def review_party(self, **_: object) -> RPAdministratorResult:
            raise AssertionError("expired Administrator window reached the model")

    result = asyncio.run(RPAdministratorHandler(engine, MustNotReview()).handle(job))

    assert result == {
        "kind": "administrator",
        "result": "no_proposal",
        "reason": "source_version_expired",
    }
    assert engine.list_administrator_proposals(
        owner_user_id="owner-one", party_id="party-expired-review"
    ) == ()


def test_administrator_does_not_version_bump_for_unchanged_guidance(
    tmp_path: Path,
) -> None:
    engine = RPTurnEngine(tmp_path / "rp-clean.db")
    _create_party(engine, "party-noop-guidance")
    _commit_first_turn(engine, "party-noop-guidance")
    first_job = engine.claim_administrator_job()
    assert first_job is not None
    first_proposal = asyncio.run(
        RPAdministratorHandler(
            engine, _AdministratorModelFake(), cadence_turns=1
        ).handle(first_job)
    )
    assert isinstance(first_proposal, RPAdministratorProposal)
    assert first_job.claim_token is not None
    engine.complete_administrator_job(
        job_id=first_job.id, claim_token=first_job.claim_token
    )
    engine.decide_administrator_proposal(
        owner_user_id="owner-one",
        party_id="party-noop-guidance",
        proposal_id=first_proposal.id,
        decision="accept",
    )
    engine.commit_turn(
        owner_user_id="owner-one",
        party_id="party-noop-guidance",
        request_id="request-after-guidance",
        idempotency_key="key-after-guidance",
        expected_version=2,
        player_text="Продолжаю после принятой рекомендации.",
        narrator_text="Сцена продолжается.",
    )
    second_job = engine.claim_administrator_job()
    assert second_job is not None

    class SameGuidanceModel:
        def __init__(self) -> None:
            self.before_text = ""

        async def review_party(
            self, *, before_text: str, **_: object
        ) -> RPAdministratorResult:
            self.before_text = before_text
            return RPAdministratorResult(
                result="suggest",
                target_slot="narrator_guidance",
                after=before_text,
            )

    model = SameGuidanceModel()
    result = asyncio.run(
        RPAdministratorHandler(engine, model, cadence_turns=1).handle(second_job)
    )

    assert model.before_text == first_proposal.after_text
    assert result == {
        "kind": "administrator",
        "result": "no_proposal",
        "reason": "unchanged_guidance",
    }
    assert engine.get_party(
        owner_user_id="owner-one", party_id="party-noop-guidance"
    ).current_version == 3
    assert len(
        engine.list_administrator_proposals(
            owner_user_id="owner-one", party_id="party-noop-guidance"
        )
    ) == 1
