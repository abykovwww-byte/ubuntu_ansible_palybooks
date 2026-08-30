"""FastAPI entrypoint for RP Gateway."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.config import Settings, get_settings
from app.core.json_patch import PatchError
from app.models.schemas import (
    AutoTestCreate,
    ChatCompletionRequest,
    ChatMessage,
    HealthResponse,
    LoginRequest,
    Outcome,
    SceneAllowance,
    PatchEnvelope,
    PatchOperation,
    PartyCharacterStateEditRequest,
    PartyBranchCreate,
    PartyCheckRequest,
    PartyCreate,
    RPAdministratorProposalDecision,
    RPPartyCreate,
    RPPartyMessageRequest,
    RPPartyStartRequest,
    RPScenarioFreeCreate,
    PartyDatasetUpdate,
    PartyLoreCardDraft,
    PartyLoreCardDraftRequest,
    PartyLoreCardCreate,
    PartyLoreCardUpdate,
    PartyGMCorrectionDecision,
    PartyMemorySummarizeRequest,
    PartyMessageRequest,
    PartyModelUpdate,
    PartyPromptPreviewRequest,
    PartyStartRequest,
    PartyTurnDatasetUpdate,
    TurnTraceAnnotationCreate,
    TurnFeedbackUpdate,
    PartyCheckpointCreate,
    PlayerCharacterCreate,
    PlayerCharacterDraftRequest,
    ProviderApiKeyCreate,
    ProviderApiKeyUpdate,
    ServiceModelUpdate,
    UserCreate,
    UserDeleteRequest,
    UserPasswordUpdate,
    UserStatusUpdate,
    WorldPromptCreate,
    WorldApplyRequest,
    WorldInstructionRequest,
    WorldClockMarkerConfirm,
    WorldPackVisibilityUpdate,
    StatePatch,
)
from app.services.adjudicator import Adjudicator, RequestAlreadyRunning, SceneContinuityError
from app.rp.content import (
    SUPPORTED_WORLD_ID,
    ScenarioPresetNotFound,
    WorldScenarioLoader,
    WorldSourceError,
)
from app.rp.mechanics import RPAdministratorHandler, RPAtomicServiceHandler
from app.rp.narrator import (
    RPNarratorService,
    RPNarratorUnavailable,
)
from app.rp.provider import (
    RPAdministratorProvider,
    RPAtomicServiceProvider,
    RPNarratorProvider,
)
from app.rp.runner import RPRunner
from app.rp.turn_engine import (
    RPAdministratorProposalConflict,
    RPIdempotencyConflict,
    RPPartyNotFound,
    RPPartyVersionConflict,
    RPTurnEngine,
)
from app.services.auth_store import AuthStore, AuthUser
from app.services.autotest import AutoPlayerClient
from app.services.character_view import party_character_sheets
from app.services.context_budget import estimate_tokens, model_context_limit_tokens, split_turns_by_token_budget
from app.services.context_estimator import estimate_party_context
from app.services.memory import MemorySummarizer
from app.services.narrative import (
    ProviderRateLimitError,
    NarrativeClient,
    archived_memory_retrieval_block,
    party_lore_cards_block,
    prompt_cache_observability,
    prompt_assembly_diagnostics,
    response_text,
    uncompacted_archive_fallback_block,
)
from app.services.provider_catalog import (
    normalize_provider,
    provider_api_key,
    provider_base_url,
    validate_narrator_settings,
)
from app.services.service_model_client import ServiceModelClient, service_prompt_text
from app.services.service_models import (
    SERVICE_MODEL_SETTING_KEY,
    service_model_choice,
    service_model_choices,
    service_model_settings,
)
from app.services.party_store import PartyStore
from app.services.prompt_tools import PromptInspector
from app.services.rp_history import (
    AUTO_START_HISTORY_MESSAGE,
    eligible_rp_turns,
    raw_history_window,
    recent_rp_scan_text,
    removable_covered_history_units,
    rp_turn_messages,
    story_memory_safe_coverage,
)
from app.services.rp_gm import RPGMService
from app.services.rp_story_memory import RPStoryMemoryUpdater
from app.services.rp_supervisor import (
    RPSupervisorService,
    load_rp_supervisor_contract,
)
from app.services.relationship_attribution import normalized_aliases
from app.services.scene_state import (
    SceneMaterialization,
    build_scene_transition_allowance,
    fallback_scene_state,
    initial_scene_state,
    materialize_scene_bundle,
    scene_state_boundary_block,
    unresolved_noncanonical_fallback_turns,
)
from app.services.state_store import StateStore, StateVersionConflict
from app.services.turn_trace import TurnTraceAssembler
from app.services.validator import safe_fallback
from app.services.world_instructor import WorldInstructor
from app.services.world_clock import (
    WorldClockBusy,
    WorldClockService,
    load_world_clock_contract,
)


logger = logging.getLogger(__name__)

LORE_CARD_DRAFT_MODEL = "deepseek/deepseek-v4-pro"
LORE_CARD_DRAFT_INPUT_MAX_CHARS = 8_000
LORE_CARD_DRAFT_OUTPUT_MAX_TOKENS = 400
RP_ANONYMOUS_OWNER = "anonymous"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    if settings.scenario_type != "rp":
        raise RuntimeError("RP gateway requires SCENARIO_TYPE=rp")
    store = StateStore(settings.sqlite_path, settings.campaign_id, settings.world_state_path)
    auth_store = AuthStore(settings)
    party_store = PartyStore(settings, default_owner_user_id=auth_store.default_owner_user_id())
    rp_engine: RPTurnEngine | None = None
    rp_runner: RPRunner | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if rp_runner is not None:
            recovered = await rp_runner.start()
            if any(recovered.values()):
                logger.warning("recovered_rp_rebuild_work %s", recovered)
        try:
            for party in party_store.list_parties():
                if party.status != "active":
                    continue
                if settings.rp_rebuild_enabled:
                    continue
                party_state_store = party_store.store_for_party(party.id)
                recovered = party_state_store.recover_interrupted_work()
                if any(recovered.values()):
                    logger.warning("recovered_interrupted_work party_id=%s %s", party.id, recovered)
                if any(job["status"] in {"pending", "running"} for job in party_state_store.service_jobs(limit=20)):
                    try:
                        party_runtime = runtime_settings_for_party(party)
                    except ValueError as exc:
                        logger.warning("party_runtime_disabled party_id=%s error=%s", party.id, exc)
                        continue
                    Adjudicator(
                        party_runtime,
                        party_state_store,
                        relationship_model=relationship_model_for_party(party),
                        scene_contract=scene_contract_for_party(party),
                        world_clock_contract=world_clock_contract_for_party(
                            party,
                            effective_revision=party_runtime.rp_contract_revision,
                        ),
                        rp_supervisor_contract=rp_supervisor_contract_for_party(party),
                    ).schedule_service_jobs()
            for branch in party_store.list_all_party_branches():
                if settings.rp_rebuild_enabled:
                    continue
                branch_store = party_store.store_for_branch(branch["party_id"], branch["id"])
                recovered = branch_store.recover_interrupted_work()
                if any(recovered.values()):
                    logger.warning("recovered_interrupted_branch_work branch_id=%s %s", branch["id"], recovered)
            for run in party_store.resumable_autotest_runs():
                if settings.rp_rebuild_enabled:
                    continue
                schedule_autotest(run["id"])
            yield
        finally:
            if rp_runner is not None:
                await rp_runner.stop()

    app = FastAPI(title="RP Gateway", version="0.5.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.auth_store = auth_store
    app.state.adjudicator = Adjudicator(settings, store)
    app.state.party_store = party_store
    app.state.autotest_tasks = {}
    app.state.rp_engine = None
    app.state.rp_runner = None

    def settings_with_global_service_model(base: Settings) -> Settings:
        choice_id = auth_store.get_global_setting(SERVICE_MODEL_SETTING_KEY, base.service_model_choice)
        return replace(base, service_model_choice=choice_id)

    def settings_with_provider_key(base: Settings, party: Any | None = None) -> Settings:
        if party is None:
            return settings_with_global_service_model(base)
        updates: dict[str, Any] = {}
        key_fields = {
            "gemini": "gemini_api_key",
            "openrouter": "openrouter_api_key",
        }
        provider_owner_id = getattr(party, "owner_user_id", None)
        if provider_owner_id == RP_ANONYMOUS_OWNER:
            provider_owner_id = None
        bound_provider = normalize_provider(
            str(getattr(party, "narrator_provider", ""))
        )
        bound_base_url = getattr(party, "narrator_base_url", None)
        bound_secret: str | None = None
        for provider, field_name in key_fields.items():
            base_url = (
                str(bound_base_url)
                if bound_base_url and bound_provider == provider
                else provider_base_url(base, provider)
            )
            secret = auth_store.default_provider_secret(
                base_url,
                provider=provider,
                owner_user_id=provider_owner_id,
                party_id=party.id,
                exact_base_url=bool(
                    bound_base_url and bound_provider == provider
                ),
            )
            if secret:
                updates[field_name] = secret
                if provider == bound_provider:
                    bound_secret = secret
        if (
            bound_base_url
            and bound_provider in key_fields
            and str(bound_base_url).rstrip("/")
            != provider_base_url(base, bound_provider).rstrip("/")
            and not bound_secret
        ):
            raise ValueError(
                "custom Party narrator endpoint requires an exact Party BYOK key"
            )
        if bound_base_url:
            endpoint_fields = {
                "local": "local_llm_base_url",
                "gemini": "gemini_api_base",
                "openrouter": "openrouter_api_base",
            }
            endpoint_field = endpoint_fields.get(bound_provider)
            if endpoint_field:
                updates[endpoint_field] = str(bound_base_url)
        hydrated = replace(base, **updates) if updates else base
        selected_key = provider_api_key(hydrated, hydrated.llm_provider)
        if selected_key != hydrated.llm_api_key:
            hydrated = replace(hydrated, llm_api_key=selected_key)
        return settings_with_global_service_model(hydrated)

    def runtime_settings_for_party(party: Any) -> Settings:
        return settings_with_provider_key(settings_for_party(settings, party), party)

    if settings.rp_rebuild_enabled:
        rp_engine = RPTurnEngine(settings.rp_sqlite_path)
        atomic_choice_id = auth_store.get_global_setting(
            SERVICE_MODEL_SETTING_KEY, settings.service_model_choice
        )
        atomic_choice = service_model_choice(settings, atomic_choice_id)
        administrator_choice = service_model_choice(
            settings, settings.rp_administrator_model_choice
        )

        def checked_role_choice(
            choice: dict[str, Any], *, enabled: bool, role: str
        ) -> dict[str, Any]:
            if choice["provider"] in {"local", "openrouter"} and choice["model"]:
                if enabled and not choice.get("available", False):
                    raise ValueError(
                        f"{role} model choice is unavailable: {choice['id']}"
                    )
                return choice
            if enabled:
                raise ValueError(
                    f"{role} model choice is retired or unsupported: {choice['id']}"
                )
            return {
                "id": "disabled-local-placeholder",
                "provider": "local",
                "model": settings.local_llm_model_alias,
            }

        atomic_choice = checked_role_choice(
            atomic_choice,
            enabled=settings.rp_atomic_service_enabled,
            role="atomic service",
        )
        administrator_choice = checked_role_choice(
            administrator_choice,
            enabled=settings.rp_administrator_enabled,
            role="Administrator",
        )
        atomic_settings = (
            settings
            if atomic_choice["id"] == "disabled-local-placeholder"
            else service_model_settings(settings, atomic_choice["id"])
        )
        administrator_settings = (
            settings
            if administrator_choice["id"] == "disabled-local-placeholder"
            else service_model_settings(settings, administrator_choice["id"])
        )
        atomic_model = RPAtomicServiceProvider(
            atomic_settings,
            provider=str(atomic_choice["provider"]),
            model=str(atomic_choice["model"]),
        )
        administrator_model = RPAdministratorProvider(
            administrator_settings,
            provider=str(administrator_choice["provider"]),
            model=str(administrator_choice["model"]),
        )
        rp_runner = RPRunner(
            rp_engine,
            RPAtomicServiceHandler(rp_engine, atomic_model),
            RPAdministratorHandler(rp_engine, administrator_model),
            service_enabled=settings.rp_atomic_service_enabled,
            administrator_enabled=settings.rp_administrator_enabled,
            poll_interval=settings.rp_runner_poll_interval_seconds,
        )
        app.state.rp_engine = rp_engine
        app.state.rp_runner = rp_runner

    def ensure_party_playable(party: Any) -> None:
        if party.status == "archived":
            raise HTTPException(status_code=409, detail="archived party is terminal")
        try:
            party_store.require_active_model_profile(party.model_profile_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def require_rebuilt_model_profile(model_profile_id: str) -> Any:
        profile = party_store.require_active_model_profile(model_profile_id)
        if not party_store.model_profile_is_visible(profile):
            raise ValueError(f"model profile is unavailable for RP: {model_profile_id}")
        return profile
    app.state.adjudicator = Adjudicator(settings_with_global_service_model(settings), store)

    def runtime_settings_for_profile(profile: Any, cache_session_id: str, party: Any | None = None) -> Settings:
        return settings_with_provider_key(settings_for_model_profile(settings, profile, cache_session_id), party)

    def runtime_settings_for_branch(party: Any, branch_id: str) -> Settings:
        branch = party_store.get_party_branch(party.id, branch_id, owner_user_id=party.owner_user_id)
        branch_revision = int(branch["rp_contract_revision"])
        return replace(
            settings_with_provider_key(
                settings_for_party(settings, party, effective_revision=branch_revision),
                party,
            ),
            prompt_cache_session_id=f"rp-party:{party.id}:branch:{branch_id}",
            rp_contract_revision=branch_revision,
        )

    async def run_autotest(run_id: str) -> None:
        try:
            while True:
                run = party_store.get_autotest_run(run_id)
                if run["status"] in {"completed", "failed", "stopped"}:
                    return
                if run["stop_requested"] or run["status"] == "stopping":
                    party_store.update_autotest_run(run_id, status="stopped", current_phase="stopped")
                    return
                completed_turns = int(run["completed_turns"])
                requested_turns = int(run["requested_turns"])
                if completed_turns >= requested_turns:
                    party_store.update_autotest_run(run_id, status="completed", current_phase="done")
                    return

                if run.get("branch_id"):
                    party = party_store.get_party(run["source_party_id"], owner_user_id=run["owner_user_id"])
                    party_state_store = party_store.store_for_branch(
                        party.id,
                        run["branch_id"],
                        owner_user_id=run["owner_user_id"],
                    )
                    party_settings = runtime_settings_for_branch(party, run["branch_id"])
                else:
                    # Backward compatibility for runs created before checkpoint branches existed.
                    party = party_store.get_party(run["test_party_id"], owner_user_id=run["owner_user_id"])
                    party_state_store = party_store.store_for_party(party.id, owner_user_id=run["owner_user_id"])
                    party_settings = runtime_settings_for_party(party)
                player_profile = party_store.get_model_profile(run["player_model_profile_id"])
                player_settings = runtime_settings_for_profile(player_profile, f"rp-autotest-player:{run_id}", party)
                turn_number = completed_turns + 1
                request_id = f"autotest_{run_id}_{turn_number}"

                party_store.update_autotest_run(run_id, current_phase="player", error=None)
                action = await AutoPlayerClient(player_settings, player_profile).next_action(
                    player_prompt=run["player_prompt"],
                    player_character=party.player_character,
                    scenario_type=party.scenario_type,
                    history=party_state_store.turn_history(limit=32),
                    request_id=f"{request_id}_player",
                )
                run = party_store.get_autotest_run(run_id)
                if run["stop_requested"]:
                    party_store.update_autotest_run(
                        run_id,
                        status="stopped",
                        current_phase="stopped",
                        last_player_action=action,
                    )
                    return

                party_store.update_autotest_run(
                    run_id,
                    current_phase="narrator",
                    last_player_action=action,
                )
                narrator_profile = party.model_profile or party_store.get_model_profile(party.model_profile_id)
                chat_request = party_chat_request(
                    party_state_store,
                    narrator_profile.model,
                    PartyMessageRequest(
                        content=action,
                        idempotency_key=f"autotest:{run_id}:turn:{turn_number}",
                    ),
                    party_settings,
                    provider=narrator_profile.provider,
                    narrator_settings=party.narrator_settings,
                )
                narrator_response = await Adjudicator(
                    party_settings,
                    party_state_store,
                    relationship_model=relationship_model_for_party(party),
                    scene_contract=scene_contract_for_party(party),
                    world_clock_contract=world_clock_contract_for_party(
                        party,
                        effective_revision=party_settings.rp_contract_revision,
                    ),
                    rp_supervisor_contract=rp_supervisor_contract_for_party(party),
                ).handle_chat(
                    chat_request,
                    authorization=None,
                    idempotency_key=f"autotest:{run_id}:turn:{turn_number}",
                    request_id=f"{request_id}_narrator",
                    allow_gateway_fallback=(
                        party_settings.rp_contract_revision >= 7
                    ),
                )
                fallback_turns = int(run.get("fallback_turns") or 0)
                choices = narrator_response.get("choices") or []
                if choices and choices[0].get("finish_reason") == "provider_fallback":
                    fallback_turns += 1
                party_store.update_autotest_run(
                    run_id,
                    completed_turns=turn_number,
                    fallback_turns=fallback_turns,
                    current_phase="player" if turn_number < requested_turns else "done",
                    status="running" if turn_number < requested_turns else "completed",
                    last_player_action=action,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("autotest_run_failed run_id=%s", run_id)
            party_store.update_autotest_run(
                run_id,
                status="failed",
                current_phase="failed",
                error=f"{type(exc).__name__}: {exc}"[:500],
            )

    def schedule_autotest(run_id: str) -> None:
        existing = app.state.autotest_tasks.get(run_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(run_autotest(run_id))
        app.state.autotest_tasks[run_id] = task

        def forget_task(_task: asyncio.Task[Any]) -> None:
            app.state.autotest_tasks.pop(run_id, None)

        task.add_done_callback(forget_task)

    def current_user(request: Request) -> AuthUser | None:
        if not settings.auth_enabled:
            return None
        user = getattr(request.state, "user", None)
        if not isinstance(user, AuthUser):
            raise HTTPException(status_code=401, detail="authentication required")
        return user

    def owner_user_id(request: Request) -> str | None:
        user = current_user(request)
        return user.id if user else None

    def rebuilt_rp_request(request: Request) -> bool:
        return settings.rp_rebuild_enabled

    def rp_owner_user_id(request: Request) -> str:
        user = current_user(request)
        return user.id if user else RP_ANONYMOUS_OWNER

    def rp_auth_owner_user_id(request: Request) -> str | None:
        owner_id = rp_owner_user_id(request)
        return None if owner_id == RP_ANONYMOUS_OWNER else owner_id

    def require_rp_engine() -> RPTurnEngine:
        if rp_engine is None:
            raise HTTPException(status_code=503, detail="rebuilt RP runtime is disabled")
        return rp_engine

    def persisted_rebuilt_parties_for_owner(owner_user_id: str) -> tuple[Any, ...]:
        engine = rp_engine
        if engine is None:
            database_path = Path(settings.rp_sqlite_path)
            if not database_path.exists():
                return ()
            try:
                engine = RPTurnEngine(database_path)
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="cannot verify persisted rebuilt RP ownership",
                ) from exc
        return engine.list_parties(owner_user_id=owner_user_id)

    def rp_world_loader() -> WorldScenarioLoader:
        return WorldScenarioLoader(Path(settings.worldpacks_path) / SUPPORTED_WORLD_ID)

    def rp_party_payload(party: Any) -> dict[str, Any]:
        return {
            "id": party.id,
            "title": party.title,
            "scenario_type": "rp",
            "status": "active",
            "world_id": party.world_snapshot.world_id,
            "world_title": party.world_snapshot.title,
            "scenario_id": party.scenario_snapshot.scenario_id,
            "scenario_title": party.scenario_snapshot.title,
            "scenario_source": party.scenario_snapshot.source,
            "model_profile_id": party.narrator_profile_id,
            "narrator_provider": party.narrator_provider,
            "narrator_model": party.narrator_model,
            "narrator_settings": party.narrator_settings,
            "world_hash": party.world_hash,
            "scenario_hash": party.scenario_hash,
            "current_version": party.current_version,
            "created_at": party.created_at,
            "updated_at": party.updated_at,
        }

    def rp_turn_payload(turn: Any) -> dict[str, Any]:
        return {
            "id": turn.id,
            "party_id": turn.party_id,
            "turn_kind": turn.turn_kind,
            "request_id": turn.request_id,
            "idempotency_key": turn.idempotency_key,
            "expected_version": turn.expected_version,
            "committed_version": turn.committed_version,
            "player_text": turn.player_text,
            "narrator_text": turn.narrator_text,
            "created_at": turn.created_at,
        }

    def rp_request_payload(narration_request: Any) -> dict[str, Any]:
        return {
            "id": narration_request.id,
            "party_id": narration_request.party_id,
            "turn_kind": narration_request.turn_kind,
            "request_id": narration_request.request_id,
            "idempotency_key": narration_request.idempotency_key,
            "expected_version": narration_request.expected_version,
            "player_text": narration_request.player_text,
            "status": narration_request.status,
            "turn_id": narration_request.turn_id,
            "last_error": narration_request.last_error,
            "created_at": narration_request.created_at,
            "updated_at": narration_request.updated_at,
        }

    def rp_job_payload(job: Any) -> dict[str, Any]:
        payload = {
            "id": job.id,
            "party_id": job.party_id,
            "source_turn_id": job.source_turn_id,
            "source_version": job.source_version,
            "status": job.status,
            "attempts": job.attempts,
            "max_attempts": job.max_attempts,
            "result": job.result,
            "last_error": job.last_error,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        }
        if hasattr(job, "job_type"):
            payload["job_type"] = job.job_type
        else:
            payload.update(
                {
                    "job_type": "administrator",
                    "window_start_version": job.window_start_version,
                    "window_end_version": job.window_end_version,
                    "evidence_versions": list(job.evidence_versions),
                    "window_hash": job.window_hash,
                }
            )
        return payload

    def rp_proposal_payload(proposal: Any) -> dict[str, Any]:
        return {
            "id": proposal.id,
            "party_id": proposal.party_id,
            "administrator_job_id": proposal.administrator_job_id,
            "kind": proposal.kind,
            "target_slot": proposal.target_slot,
            "before_text": proposal.before_text,
            "after_text": proposal.after_text,
            "base_party_version": proposal.base_party_version,
            "base_guidance_revision": proposal.base_guidance_revision,
            "evidence_versions": list(proposal.evidence_versions),
            "window_hash": proposal.window_hash,
            "status": proposal.status,
            "applied_party_version": proposal.applied_party_version,
            "created_at": proposal.created_at,
            "decided_at": proposal.decided_at,
        }

    def rp_role_status(
        *,
        role: str,
        enabled: bool,
        provider: str,
        model: str,
        work: tuple[Any, ...],
    ) -> dict[str, Any]:
        last_error = next(
            (item.last_error for item in reversed(work) if item.last_error), None
        )
        return {
            "role": role,
            "enabled": enabled,
            "kill_switch": not enabled,
            "provider": provider,
            "model": model,
            "status": work[-1].status if work else "idle",
            "success_count": sum(item.status == "succeeded" for item in work),
            "error_count": sum(item.status == "failed" for item in work),
            "last_error": last_error,
        }

    def rp_narrator_service_for(party: Any, request_id: str) -> RPNarratorService:
        provider = RPNarratorProvider(
            settings_with_provider_key(settings, party),
            provider=party.narrator_provider,
            model=party.narrator_model,
            narrator_settings=party.narrator_settings,
            party_id=party.id,
            request_id=request_id,
        )
        return RPNarratorService(
            require_rp_engine(),
            provider,
            atomic_service_enabled=settings.rp_atomic_service_enabled,
            derived_wait_seconds=settings.rp_derived_wait_seconds,
            derived_poll_interval=settings.rp_runner_poll_interval_seconds,
        )

    def ensure_rebuilt_narrator_binding(party: Any) -> None:
        try:
            profile = require_rebuilt_model_profile(party.narrator_profile_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if (
            normalize_provider(profile.provider) != party.narrator_provider
            or profile.base_url != party.narrator_base_url
            or profile.model != party.narrator_model
            or party.narrator_model in settings.llm_disabled_models
        ):
            raise HTTPException(
                status_code=409,
                detail="party narrator binding is retired or no longer matches its profile",
            )

    def turn_trace_scope(
        request: Request,
        party_id: str,
        branch_id: str | None,
    ) -> tuple[Any, dict[str, Any] | None, StateStore]:
        require_admin(request)
        require_legacy_party_store_runtime()
        party = party_store.get_party(party_id, owner_user_id=None)
        if branch_id:
            branch = party_store.get_party_branch(party_id, branch_id, owner_user_id=None)
            trace_store = party_store.store_for_branch(party_id, branch_id, owner_user_id=None)
            return party, branch, trace_store
        return party, None, party_store.store_for_party(party_id, owner_user_id=None)

    def require_legacy_party_store_runtime() -> None:
        if settings.rp_rebuild_enabled:
            raise HTTPException(
                status_code=410,
                detail="legacy PartyStore operation is unavailable after rebuilt cutover",
            )

    def require_admin(request: Request) -> AuthUser | None:
        user = current_user(request)
        if settings.auth_enabled and (not user or not user.is_admin):
            raise HTTPException(status_code=403, detail="admin role required")
        return user

    def can_view_private_worldpacks(request: Request) -> bool:
        user = current_user(request)
        return not settings.auth_enabled or bool(user and user.is_admin)

    def accessible_worldpack(request: Request, worldpack_id: str) -> Any:
        return party_store.get_worldpack(
            worldpack_id,
            owner_user_id=owner_user_id(request),
            include_private=can_view_private_worldpacks(request),
        )

    def rebuilt_party_http_route_allowed(method: str, path: str) -> bool:
        if path == "/api/parties":
            return method in {"GET", "POST"}
        allowed = (
            ("GET", r"/api/parties/[^/]+"),
            ("GET", r"/api/parties/[^/]+/(history|memory|service-jobs|lore-cards|supervisor)"),
            ("GET", r"/api/parties/[^/]+/requests/[^/]+"),
            ("POST", r"/api/parties/[^/]+/(start|messages)"),
            ("GET", r"/api/parties/[^/]+/byok"),
            ("POST", r"/api/parties/[^/]+/byok"),
            (("PATCH", "DELETE"), r"/api/parties/[^/]+/byok/[^/]+"),
            ("GET", r"/api/parties/[^/]+/administrator/proposals"),
            (
                "POST",
                r"/api/parties/[^/]+/administrator/proposals/[0-9]+/decision",
            ),
            ("GET", r"/api/parties/[^/]+/turn-traces(?:/[^/]+)?"),
            (
                "POST",
                r"/api/parties/[^/]+/turn-traces/[^/]+/annotations",
            ),
        )
        for allowed_method, pattern in allowed:
            methods = (
                allowed_method
                if isinstance(allowed_method, tuple)
                else (allowed_method,)
            )
            if method in methods and re.fullmatch(pattern, path):
                return True
        return False

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        if settings.auth_enabled and path.startswith("/api/"):
            if not path.startswith("/api/auth/"):
                token = request.cookies.get(settings.auth_session_cookie_name)
                user = auth_store.user_for_session(token)
                if user is None:
                    return JSONResponse(
                        {"detail": "authentication required"}, status_code=401
                    )
                request.state.user = user
        if (
            settings.rp_rebuild_enabled
            and path.startswith("/api/parties")
            and not rebuilt_party_http_route_allowed(request.method, path)
        ):
            return JSONResponse(
                {
                    "detail": (
                        "This legacy RP operation is unavailable after the rebuilt "
                        "runtime cutover"
                    )
                },
                status_code=410,
            )
        if (
            settings.rp_rebuild_enabled
            and request.method == "POST"
            and path == "/api/worldpacks/prompt"
        ):
            return JSONResponse(
                {
                    "detail": (
                        "Player templates and prompt WorldPacks were replaced by "
                        "World/Scenario party creation"
                    )
                },
                status_code=410,
            )
        legacy_global_paths = {
            "/api/state",
            "/api/state/history",
            "/api/state/patch/preview",
            "/api/state/patch/apply",
            "/api/world/proposals",
            "/api/world/instruct",
            "/api/world/apply",
            "/api/turn/rollback",
        }
        if settings.rp_rebuild_enabled and path in legacy_global_paths:
            return JSONResponse(
                {
                    "detail": (
                        "The global legacy RP StateStore is unavailable after the "
                        "rebuilt runtime cutover"
                    )
                },
                status_code=410,
            )
        return await call_next(request)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        try:
            store.get_state()
            if settings.rp_rebuild_enabled:
                require_rp_engine().list_parties(
                    owner_user_id=RP_ANONYMOUS_OWNER
                )
                if rp_runner is None or not rp_runner.running:
                    raise RuntimeError("rebuilt RP runner is not running")
            database = "ok"
        except Exception:  # noqa: BLE001
            database = "error"
        status = "ok" if database == "ok" else "error"
        return HealthResponse(status=status, campaign_id=settings.campaign_id, database=database)

    @app.get("/api/auth/me")
    def auth_me(request: Request) -> dict[str, Any]:
        user = auth_store.user_for_session(request.cookies.get(settings.auth_session_cookie_name)) if settings.auth_enabled else None
        return {
            "auth_enabled": settings.auth_enabled,
            "authenticated": user is not None or not settings.auth_enabled,
            "user": user.public_dict() if user else None,
        }

    @app.post("/api/auth/login")
    def auth_login(request: LoginRequest, response: Response) -> dict[str, Any]:
        if not settings.auth_enabled:
            return {"auth_enabled": False, "authenticated": True, "user": None}
        try:
            user = auth_store.authenticate(request.username, request.password)
        except ValueError:
            user = None
        if user is None:
            raise HTTPException(status_code=401, detail="invalid username or password")
        token = auth_store.create_session(user.id)
        response.set_cookie(
            settings.auth_session_cookie_name,
            token,
            max_age=settings.auth_session_ttl_seconds,
            httponly=True,
            secure=settings.auth_cookie_secure,
            samesite="lax",
        )
        return {"auth_enabled": True, "authenticated": True, "user": user.public_dict()}

    @app.post("/api/auth/logout")
    def auth_logout(request: Request, response: Response) -> dict[str, Any]:
        auth_store.delete_session(request.cookies.get(settings.auth_session_cookie_name))
        response.delete_cookie(settings.auth_session_cookie_name)
        return {"logged_out": True}

    @app.get("/api/admin/users")
    def admin_list_users(request: Request) -> dict[str, Any]:
        require_admin(request)
        users = auth_store.list_users()
        return {
            "users": [
                {
                    **user.public_dict(),
                    "party_count": len(party_store.list_parties(owner_user_id=user.id)),
                    "character_count": len(party_store.list_player_characters(owner_user_id=user.id)),
                }
                for user in users
            ]
        }

    @app.post("/api/admin/users")
    def admin_create_user(request: Request, payload: UserCreate) -> dict[str, Any]:
        require_admin(request)
        try:
            user = auth_store.create_user(payload.username, payload.password, payload.role)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"user": user.public_dict()}

    @app.patch("/api/admin/users/{user_id}/password")
    def admin_set_user_password(request: Request, user_id: str, payload: UserPasswordUpdate) -> dict[str, Any]:
        require_admin(request)
        try:
            user = auth_store.set_password(user_id, payload.password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"user": user.public_dict()}

    @app.patch("/api/admin/users/{user_id}/status")
    def admin_set_user_status(request: Request, user_id: str, payload: UserStatusUpdate) -> dict[str, Any]:
        require_admin(request)
        try:
            user = auth_store.set_user_status(user_id, payload.status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"user": user.public_dict()}

    @app.delete("/api/admin/users/{user_id}")
    def admin_delete_user(request: Request, user_id: str, payload: UserDeleteRequest = UserDeleteRequest()) -> dict[str, Any]:
        admin = require_admin(request)
        if admin and admin.id == user_id:
            raise HTTPException(status_code=400, detail="cannot delete the current admin session user")
        try:
            if persisted_rebuilt_parties_for_owner(user_id):
                raise ValueError("user still owns rebuilt RP parties")
            if payload.delete_data:
                party_store.delete_user_data(user_id)
            elif party_store.has_retired_non_rp_user_data(user_id):
                raise ValueError("user owns retired non-RP data; cleanup requires explicit O2")
            elif party_store.list_parties(owner_user_id=user_id) or party_store.list_player_characters(
                owner_user_id=user_id
            ):
                raise ValueError("user still owns parties or characters")
            auth_store.delete_user(user_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"deleted": True, "user_id": user_id, "deleted_data": payload.delete_data}

    @app.get("/api/admin/global-settings/service-model")
    def admin_get_service_model(request: Request) -> dict[str, Any]:
        require_admin(request)
        runtime = settings_with_global_service_model(settings)
        return {
            "term": "Служебная модель",
            "scope": "Весь RP Stack: все текущие и будущие партии всех пользователей",
            "uses": ["Долговременная память", "Изменение мира", "Генерация персонажей"],
            "choice_id": runtime.service_model_choice,
            "selected": service_model_choice(runtime),
            "choices": service_model_choices(runtime),
        }

    @app.patch("/api/admin/global-settings/service-model")
    def admin_set_service_model(request: Request, payload: ServiceModelUpdate) -> dict[str, Any]:
        nonlocal atomic_choice
        require_admin(request)
        choices = service_model_choices(settings)
        selected = next((choice for choice in choices if choice["id"] == payload.choice_id), None)
        if selected is None:
            raise HTTPException(status_code=400, detail="unknown service model choice")
        if not selected["available"]:
            detail = "local service model is disabled" if selected["provider"] == "local" else "server OpenRouter API key is not configured"
            raise HTTPException(status_code=400, detail=detail)
        replacement_choice: dict[str, Any] | None = None
        replacement_handler: RPAtomicServiceHandler | None = None
        if settings.rp_rebuild_enabled:
            replacement_choice = checked_role_choice(
                selected,
                enabled=settings.rp_atomic_service_enabled,
                role="atomic service",
            )
            replacement_settings = service_model_settings(
                settings, replacement_choice["id"]
            )
            replacement_handler = RPAtomicServiceHandler(
                require_rp_engine(),
                RPAtomicServiceProvider(
                    replacement_settings,
                    provider=str(replacement_choice["provider"]),
                    model=str(replacement_choice["model"]),
                ),
            )
        auth_store.set_global_setting(SERVICE_MODEL_SETTING_KEY, payload.choice_id)
        if replacement_choice is not None and replacement_handler is not None:
            if rp_runner is None:
                raise HTTPException(status_code=503, detail="rebuilt RP runner is disabled")
            rp_runner.service_handler = replacement_handler
            atomic_choice = replacement_choice
        runtime = settings_with_global_service_model(settings)
        return {
            "term": "Служебная модель",
            "scope": "Весь RP Stack: все текущие и будущие партии всех пользователей",
            "uses": ["Долговременная память", "Изменение мира", "Генерация персонажей"],
            "choice_id": runtime.service_model_choice,
            "selected": service_model_choice(runtime),
            "choices": service_model_choices(runtime),
        }

    @app.patch("/api/admin/datasets/parties/{party_id}")
    def admin_update_party_dataset(
        request: Request,
        party_id: str,
        payload: PartyDatasetUpdate,
    ) -> dict[str, Any]:
        admin = require_admin(request)
        require_legacy_party_store_runtime()
        try:
            party_store.get_party(
                party_id, owner_user_id=admin.id if admin else None
            )
            party = party_store.update_party_dataset(
                party_id,
                review_status=payload.review_status,
                tags=payload.tags,
                owner_user_id=admin.id if admin else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"party": party.model_dump(mode="json")}

    @app.get("/api/admin/datasets/parties/{party_id}/turns")
    def admin_list_dataset_turns(
        request: Request,
        party_id: str,
        branch_id: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        admin = require_admin(request)
        require_legacy_party_store_runtime()
        try:
            party_store.get_party(
                party_id, owner_user_id=admin.id if admin else None
            )
            turns = party_store.list_dataset_turns(
                party_id,
                branch_id=branch_id,
                owner_user_id=admin.id if admin else None,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"party_id": party_id, "branch_id": branch_id, "turns": turns}

    @app.put("/api/admin/datasets/parties/{party_id}/turns/{turn_id}")
    def admin_label_dataset_turn(
        request: Request,
        party_id: str,
        turn_id: int,
        payload: PartyTurnDatasetUpdate,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        admin = require_admin(request)
        require_legacy_party_store_runtime()
        try:
            party_store.get_party(
                party_id, owner_user_id=admin.id if admin else None
            )
            label = party_store.set_turn_dataset_label(
                party_id,
                turn_id,
                branch_id=branch_id,
                review_status=payload.review_status,
                tags=payload.tags,
                notes=payload.notes,
                owner_user_id=admin.id if admin else None,
                updated_by_user_id=admin.id if admin else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"party_id": party_id, "branch_id": branch_id, "label": label}

    @app.get("/api/admin/datasets/export.jsonl")
    def admin_export_dataset(
        request: Request,
        scenario_type: str | None = None,
        include_branches: bool = True,
    ) -> StreamingResponse:
        admin = require_admin(request)
        if scenario_type and scenario_type not in {"rp", "novel"}:
            raise HTTPException(status_code=400, detail="scenario_type must be rp or novel")
        legacy_party_ids = set() if settings.rp_rebuild_enabled else None
        export = party_store.export_dataset_records(
            owner_user_id=admin.id if admin else None,
            scenario_type=scenario_type,
            include_branches=include_branches,
            party_ids=legacy_party_ids,
        )
        body = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in export["records"])
        return StreamingResponse(
            iter([body]),
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": 'attachment; filename="rp-gateway-sft-v1.jsonl"',
                "X-Dataset-Approved-Turns": str(export["approved_turns"]),
                "X-Dataset-Skipped-Missing-Prompt": str(export["skipped_missing_prompt"]),
            },
        )

    @app.get("/api/state")
    def get_state(request: Request) -> dict[str, Any]:
        require_admin(request)
        return {"campaign_id": settings.campaign_id, "state": store.get_state()}

    @app.get("/api/state/history")
    def get_history(request: Request, limit: int = 50) -> dict[str, Any]:
        require_admin(request)
        return {"campaign_id": settings.campaign_id, "history": store.history(limit=limit)}

    @app.get("/api/worldpacks")
    def list_worldpacks(
        request: Request, scenario_type: str | None = None
    ) -> dict[str, Any]:
        if rebuilt_rp_request(request):
            if scenario_type not in {None, "rp"}:
                raise HTTPException(
                    status_code=400,
                    detail="scenario_type must be rp after rebuilt cutover",
                )
            try:
                loader = rp_world_loader()
                world = loader.load_world_definition()
                presets = loader.load_presets()
            except WorldSourceError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            return {
                "worldpacks": [
                    {
                        "id": world.id,
                        "title": world.title,
                        "language": world.language,
                        "premise": world.premise,
                        "scenario_presets": [
                            {
                                "id": preset.id,
                                "title": preset.title,
                                "player_role": preset.player_role,
                                "style": preset.style,
                                "format": preset.format,
                                "difficulty": preset.difficulty,
                                "detail_level": preset.detail_level,
                            }
                            for preset in presets
                        ],
                    }
                ]
            }
        packs = party_store.list_worldpacks(
            owner_user_id=owner_user_id(request),
            include_private=can_view_private_worldpacks(request),
        )
        return {"worldpacks": [pack.model_dump(mode="json") for pack in packs]}

    @app.patch("/api/admin/worldpacks/{worldpack_id}/visibility")
    def admin_set_worldpack_visibility(
        request: Request,
        worldpack_id: str,
        payload: WorldPackVisibilityUpdate,
    ) -> dict[str, Any]:
        require_admin(request)
        try:
            if settings.rp_rebuild_enabled:
                raise HTTPException(
                    status_code=410,
                    detail="legacy WorldPack administration is unavailable after rebuilt cutover",
                )
            pack = party_store.set_worldpack_visibility(worldpack_id, payload.visibility)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"worldpack": pack.model_dump(mode="json")}

    @app.post("/api/worldpacks/prompt")
    def create_prompt_worldpack(request: Request, payload: WorldPromptCreate) -> dict[str, Any]:
        try:
            pack = party_store.create_prompt_worldpack(payload, owner_user_id=owner_user_id(request))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"worldpack": pack.model_dump(mode="json")}

    @app.get("/api/worldpacks/{worldpack_id}")
    def get_worldpack(request: Request, worldpack_id: str) -> dict[str, Any]:
        if rebuilt_rp_request(request):
            if worldpack_id != SUPPORTED_WORLD_ID:
                raise HTTPException(status_code=404, detail="worldpack not found")
            worldpack = list_worldpacks(request)["worldpacks"][0]
            try:
                preset = rp_world_loader().materialize_preset(
                    worldpack["scenario_presets"][0]["id"]
                )
            except (WorldSourceError, ScenarioPresetNotFound, IndexError) as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            worldpack["free_scenario_seed"] = {
                "source": "free",
                "scenario_id": "free-scenario",
                "title": "Свободный сценарий",
                "player_role": preset.player_role,
                "style": preset.style,
                "format": preset.format,
                "difficulty": preset.difficulty,
                "detail_level": preset.detail_level,
                "narrator_system": preset.narrator_system,
                "narrator_note": preset.narrator_note,
                "opening": preset.opening,
                "initial_state": preset.initial_state,
                "active_character_ids": list(preset.active_character_ids),
                "local_overrides": preset.local_overrides,
            }
            return {"worldpack": worldpack}
        try:
            pack = accessible_worldpack(request, worldpack_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"worldpack": pack.model_dump(mode="json")}

    @app.get("/api/worldpacks/{worldpack_id}/player-templates")
    def player_templates(request: Request, worldpack_id: str) -> dict[str, Any]:
        if rebuilt_rp_request(request):
            raise HTTPException(
                status_code=410,
                detail="player templates are unavailable after rebuilt cutover",
            )
        try:
            templates = party_store.player_templates(
                worldpack_id,
                owner_user_id=owner_user_id(request),
                include_private=can_view_private_worldpacks(request),
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"worldpack_id": worldpack_id, "templates": [template.model_dump(mode="json") for template in templates]}

    @app.get("/api/player-characters")
    def list_player_characters(request: Request, worldpack_id: str | None = None) -> dict[str, Any]:
        if rebuilt_rp_request(request):
            raise HTTPException(
                status_code=410,
                detail="legacy player characters are unavailable after rebuilt cutover",
            )
        characters = party_store.list_player_characters(worldpack_id=worldpack_id, owner_user_id=owner_user_id(request))
        return {"player_characters": [character.model_dump(mode="json") for character in characters]}

    @app.post("/api/player-characters/draft")
    def draft_player_character(request: Request, payload: PlayerCharacterDraftRequest) -> dict[str, Any]:
        if rebuilt_rp_request(request):
            raise HTTPException(
                status_code=410,
                detail="legacy player character drafts are unavailable after rebuilt cutover",
            )
        try:
            pack = accessible_worldpack(request, payload.worldpack_id)
        except ValueError as exc:
            status_code = 404 if str(exc).startswith("worldpack not found:") else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        try:
            opening_id, player_role = party_store.resolve_player_character_opening(pack, payload.opening_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        description = payload.concept.strip() or player_role
        draft = {
            "worldpack_id": payload.worldpack_id,
            "name": payload.name,
            "description": description,
            "profile": {
                "source": "light-gui-draft",
                "worldpack_id": payload.worldpack_id,
                "world_title": pack.title,
                "concept": description,
            },
        }
        if opening_id is not None:
            draft["opening_id"] = opening_id
            draft["profile"]["opening_id"] = opening_id
            draft["profile"]["player_role"] = player_role
        return {"draft": draft}

    @app.post("/api/player-characters")
    def create_player_character(request: Request, payload: PlayerCharacterCreate) -> dict[str, Any]:
        if rebuilt_rp_request(request):
            raise HTTPException(
                status_code=410,
                detail="legacy player characters are unavailable after rebuilt cutover",
            )
        try:
            accessible_worldpack(request, payload.worldpack_id)
            character = party_store.create_player_character(payload, owner_user_id=owner_user_id(request))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"player_character": character.model_dump(mode="json")}

    @app.get("/api/model-profiles")
    def list_model_profiles() -> dict[str, Any]:
        party_store.settings = settings_with_provider_key(settings)
        profiles = party_store.list_model_profiles()
        return {"model_profiles": [profile.model_dump(mode="json") for profile in profiles]}

    @app.get("/api/parties")
    def list_parties(request: Request) -> dict[str, Any]:
        if rebuilt_rp_request(request):
            parties = require_rp_engine().list_parties(
                owner_user_id=rp_owner_user_id(request)
            )
            return {"parties": [rp_party_payload(party) for party in parties]}
        return {"parties": [party.model_dump(mode="json") for party in party_store.list_parties(owner_user_id=owner_user_id(request))]}

    @app.post("/api/parties")
    def create_party(
        request: Request, payload: PartyCreate | RPPartyCreate
    ) -> dict[str, Any]:
        if rebuilt_rp_request(request) and isinstance(payload, RPPartyCreate):
            try:
                loader = rp_world_loader()
                world_snapshot = loader.materialize_world()
                if isinstance(payload.scenario, RPScenarioFreeCreate):
                    scenario_snapshot = loader.materialize_free_scenario(
                        scenario_id=payload.scenario.scenario_id,
                        title=payload.scenario.title,
                        player_role=payload.scenario.player_role,
                        style=payload.scenario.style,
                        format=payload.scenario.format,
                        difficulty=payload.scenario.difficulty,
                        detail_level=payload.scenario.detail_level,
                        narrator_system=payload.scenario.narrator_system,
                        narrator_note=payload.scenario.narrator_note,
                        opening=payload.scenario.opening,
                        initial_state=payload.scenario.initial_state,
                        active_character_ids=tuple(
                            payload.scenario.active_character_ids
                        ),
                        local_overrides=payload.scenario.local_overrides,
                    )
                else:
                    scenario_snapshot = loader.materialize_preset(
                        payload.scenario.preset_id
                    )
                profile = require_rebuilt_model_profile(payload.model_profile_id)
                narrator_settings = (
                    payload.narrator_settings.model_dump(
                        mode="json", exclude_none=True
                    )
                    if payload.narrator_settings is not None
                    else {}
                )
                narrator_settings = validate_narrator_settings(
                    profile.provider, profile.model, narrator_settings
                )
                party = require_rp_engine().create_party(
                    owner_user_id=rp_owner_user_id(request),
                    party_id=f"party_{uuid.uuid4().hex[:12]}",
                    title=payload.title,
                    world_snapshot=world_snapshot,
                    scenario_snapshot=scenario_snapshot,
                    narrator_profile_id=profile.id,
                    narrator_provider=profile.provider,
                    narrator_base_url=profile.base_url,
                    narrator_model=profile.model,
                    narrator_settings=narrator_settings,
                )
            except (ValueError, WorldSourceError, ScenarioPresetNotFound) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"party": rp_party_payload(party)}
        if rebuilt_rp_request(request):
            raise HTTPException(
                status_code=422,
                detail="legacy party creation is unavailable after rebuilt cutover",
            )
        if isinstance(payload, RPPartyCreate):
            raise HTTPException(
                status_code=422, detail="rebuilt RP runtime is not active"
            )
        try:
            accessible_worldpack(request, payload.worldpack_id)
            party = party_store.create_party(payload, owner_user_id=owner_user_id(request))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"party": party.model_dump(mode="json")}

    @app.get("/api/parties/{party_id}")
    def get_party(request: Request, party_id: str) -> dict[str, Any]:
        if rebuilt_rp_request(request):
            try:
                party = require_rp_engine().get_party(
                    owner_user_id=rp_owner_user_id(request), party_id=party_id
                )
            except RPPartyNotFound as exc:
                raise HTTPException(status_code=404, detail="party not found") from exc
            return {"party": rp_party_payload(party)}
        try:
            party = party_store.get_party(
                party_id,
                owner_user_id=owner_user_id(request),
                allow_retired_read=True,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"party": party.model_dump(mode="json")}

    @app.get("/api/parties/{party_id}/byok")
    def list_party_byok(request: Request, party_id: str) -> dict[str, Any]:
        owner_id = (
            rp_auth_owner_user_id(request)
            if rebuilt_rp_request(request)
            else owner_user_id(request)
        )
        try:
            if rebuilt_rp_request(request):
                require_rp_engine().get_party(
                    owner_user_id=rp_owner_user_id(request), party_id=party_id
                )
            else:
                party_store.get_party(party_id, owner_user_id=owner_id)
        except (ValueError, RPPartyNotFound) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "party_id": party_id,
            "api_keys": [key.public_dict() for key in auth_store.list_provider_api_keys(owner_id, party_id)],
        }

    @app.post("/api/parties/{party_id}/byok")
    def create_party_byok(request: Request, party_id: str, payload: ProviderApiKeyCreate) -> dict[str, Any]:
        owner_id = (
            rp_auth_owner_user_id(request)
            if rebuilt_rp_request(request)
            else owner_user_id(request)
        )
        try:
            if rebuilt_rp_request(request):
                party = require_rp_engine().get_party(
                    owner_user_id=rp_owner_user_id(request), party_id=party_id
                )
                requested_base_url = (
                    payload.base_url or auth_store.provider_base_url(payload.provider)
                ).rstrip("/")
                if (
                    normalize_provider(payload.provider) != party.narrator_provider
                    or requested_base_url != party.narrator_base_url.rstrip("/")
                ):
                    raise ValueError(
                        "BYOK provider and base_url must match the Party narrator binding"
                    )
                key_base_url = party.narrator_base_url
            else:
                party_store.get_party(party_id, owner_user_id=owner_id)
                key_base_url = payload.base_url
            key = auth_store.create_provider_api_key(
                label=payload.label,
                secret_value=payload.api_key,
                provider=payload.provider,
                base_url=key_base_url,
                is_default=payload.is_default,
                owner_user_id=owner_id,
                party_id=party_id,
            )
        except (ValueError, RPPartyNotFound) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"party_id": party_id, "api_key": key.public_dict()}

    @app.patch("/api/parties/{party_id}/byok/{key_id}")
    def update_party_byok(
        request: Request,
        party_id: str,
        key_id: str,
        payload: ProviderApiKeyUpdate,
    ) -> dict[str, Any]:
        owner_id = (
            rp_auth_owner_user_id(request)
            if rebuilt_rp_request(request)
            else owner_user_id(request)
        )
        try:
            if rebuilt_rp_request(request):
                require_rp_engine().get_party(
                    owner_user_id=rp_owner_user_id(request), party_id=party_id
                )
            else:
                party_store.get_party(party_id, owner_user_id=owner_id)
            key = auth_store.update_provider_api_key(
                key_id,
                label=payload.label,
                secret_value=payload.api_key,
                is_default=payload.is_default,
                owner_user_id=owner_id,
                party_id=party_id,
            )
        except (ValueError, RPPartyNotFound) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"party_id": party_id, "api_key": key.public_dict()}

    @app.delete("/api/parties/{party_id}/byok/{key_id}")
    def delete_party_byok(request: Request, party_id: str, key_id: str) -> dict[str, Any]:
        owner_id = (
            rp_auth_owner_user_id(request)
            if rebuilt_rp_request(request)
            else owner_user_id(request)
        )
        try:
            if rebuilt_rp_request(request):
                require_rp_engine().get_party(
                    owner_user_id=rp_owner_user_id(request), party_id=party_id
                )
            else:
                party_store.get_party(party_id, owner_user_id=owner_id)
            auth_store.delete_provider_api_key(key_id, owner_id, party_id)
        except (ValueError, RPPartyNotFound) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"deleted": True, "party_id": party_id, "api_key_id": key_id}

    @app.post("/api/parties/{party_id}/activate")
    def activate_party(request: Request, party_id: str) -> dict[str, Any]:
        try:
            existing = party_store.get_party(
                party_id,
                owner_user_id=owner_user_id(request),
                allow_retired_read=True,
            )
            ensure_party_playable(existing)
            party = party_store.activate_party(party_id, owner_user_id=owner_user_id(request))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"party": party.model_dump(mode="json")}

    @app.post("/api/parties/{party_id}/complete")
    def complete_party(request: Request, party_id: str) -> dict[str, Any]:
        user = current_user(request)
        party_owner_id = None if user and user.is_admin else (user.id if user else None)
        try:
            party = party_store.complete_party(party_id, owner_user_id=party_owner_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"party": party.model_dump(mode="json")}

    @app.delete("/api/parties/{party_id}")
    def delete_party(request: Request, party_id: str) -> dict[str, Any]:
        owner_id = owner_user_id(request)
        try:
            party_store.get_party(party_id, owner_user_id=owner_id)
            auth_store.delete_party_provider_api_keys(owner_id, party_id)
            party_store.delete_party(party_id, owner_user_id=owner_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"deleted": True, "party_id": party_id}

    @app.patch("/api/parties/{party_id}/model")
    def update_party_model(request: Request, party_id: str, payload: PartyModelUpdate) -> dict[str, Any]:
        try:
            narrator_settings = (
                payload.narrator_settings.model_dump(mode="json", exclude_none=True)
                if payload.narrator_settings is not None
                else None
            )
            party = party_store.update_party_model(
                party_id,
                payload.model_profile_id,
                owner_user_id=owner_user_id(request),
                narrator_settings=narrator_settings,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"party": party.model_dump(mode="json")}

    @app.get("/api/parties/{party_id}/state")
    def get_party_state(request: Request, party_id: str) -> dict[str, Any]:
        try:
            party = party_store.get_party(
                party_id,
                owner_user_id=owner_user_id(request),
                allow_retired_read=True,
            )
            party_state = party_store.store_for_party(
                party_id,
                owner_user_id=owner_user_id(request),
                allow_retired_read=True,
            ).get_state()
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"party_id": party.id, "state_campaign_id": party.state_campaign_id, "state": party_state}

    @app.get("/api/parties/{party_id}/history")
    def get_party_history(request: Request, party_id: str, limit: int = 50) -> dict[str, Any]:
        if rebuilt_rp_request(request):
            try:
                turns = require_rp_engine().list_turns(
                    owner_user_id=rp_owner_user_id(request), party_id=party_id
                )
            except RPPartyNotFound as exc:
                raise HTTPException(status_code=404, detail="party not found") from exc
            bounded_limit = min(max(int(limit), 1), 500)
            return {
                "party_id": party_id,
                "turns": [rp_turn_payload(turn) for turn in turns[-bounded_limit:]],
            }
        try:
            party_store.get_party(
                party_id,
                owner_user_id=owner_user_id(request),
                allow_retired_read=True,
            )
            party_state_store = party_store.store_for_party(
                party_id,
                owner_user_id=owner_user_id(request),
                allow_retired_read=True,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "party_id": party_id,
            "turns": party_state_store.turn_history(limit=limit),
            "state_versions": party_state_store.history(limit=limit),
        }

    @app.post("/api/parties/{party_id}/world-clock/markers/{marker_id}/confirm")
    def confirm_party_world_clock_marker(
        request: Request,
        party_id: str,
        marker_id: str,
        confirmation: WorldClockMarkerConfirm,
        x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
    ) -> dict[str, Any]:
        try:
            party = party_store.get_party(party_id, owner_user_id=owner_user_id(request))
            ensure_party_playable(party)
            if party.scenario_type != "rp" or int(party.rp_contract_revision) < 10:
                raise ValueError("world clock markers require an RP revision-10 party")
            contract = world_clock_contract_for_party(party)
            if contract is None:
                raise ValueError("world clock is not declared by this WorldPack")
            party_state_store = party_store.store_for_party(
                party_id,
                owner_user_id=owner_user_id(request),
            )
            result = party_state_store.confirm_world_clock_marker(
                contract,
                marker_id=marker_id,
                request_id=(
                    confirmation.idempotency_key
                    or x_request_id
                    or f"world_clock_marker_{uuid.uuid4().hex}"
                ),
            )
        except WorldClockBusy as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "world_clock_busy", "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"party_id": party_id, **result}

    @app.get("/api/parties/{party_id}/turn-traces")
    def get_party_turn_traces(
        request: Request,
        response: Response,
        party_id: str,
        branch_id: str | None = None,
        limit: int = 30,
        before: str | None = None,
    ) -> dict[str, Any]:
        try:
            party, branch, trace_store = turn_trace_scope(request, party_id, branch_id)
            payload = TurnTraceAssembler(trace_store, party, branch).list_traces(
                limit=limit,
                before=before,
            )
        except ValueError as exc:
            if "trace cursor" in str(exc):
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        response.headers["Cache-Control"] = "no-store"
        return payload

    @app.get("/api/turn-traces/parties")
    def list_turn_trace_parties(request: Request, response: Response) -> dict[str, Any]:
        require_admin(request)
        parties = party_store.list_parties(owner_user_id=None)
        if settings.rp_rebuild_enabled:
            parties = []
        response.headers["Cache-Control"] = "no-store"
        return {"parties": [party.model_dump(mode="json") for party in parties]}

    @app.get("/api/turn-traces/parties/{party_id}/branches")
    def list_turn_trace_branches(
        request: Request,
        response: Response,
        party_id: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        require_admin(request)
        try:
            if settings.rp_rebuild_enabled:
                raise HTTPException(
                    status_code=410,
                    detail="legacy turn-trace branches are unavailable after rebuilt cutover",
                )
            party_store.get_party(party_id, owner_user_id=None)
            branches = party_store.list_party_branches(
                party_id,
                owner_user_id=None,
                limit=min(max(limit, 1), 100),
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        response.headers["Cache-Control"] = "no-store"
        return {"party_id": party_id, "branches": branches}

    @app.get("/api/parties/{party_id}/turn-traces/{request_id}")
    def get_party_turn_trace(
        request: Request,
        response: Response,
        party_id: str,
        request_id: str,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            party, branch, trace_store = turn_trace_scope(request, party_id, branch_id)
            payload = TurnTraceAssembler(trace_store, party, branch).trace(request_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        response.headers["Cache-Control"] = "no-store"
        return payload

    @app.post("/api/parties/{party_id}/turn-traces/{request_id}/annotations")
    def add_party_turn_trace_annotation(
        request: Request,
        response: Response,
        party_id: str,
        request_id: str,
        payload: TurnTraceAnnotationCreate,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            party, branch, trace_store = turn_trace_scope(request, party_id, branch_id)
            user = current_user(request)
            result = TurnTraceAssembler(trace_store, party, branch).add_annotation(
                request_id=request_id,
                annotation_id=payload.annotation_id,
                phase_key=payload.phase_key,
                body=payload.body,
                author_user_id=user.id if user else None,
            )
        except ValueError as exc:
            detail = str(exc)
            if "already belongs" in detail:
                raise HTTPException(status_code=409, detail=detail) from exc
            if "not found" in detail and "phase" not in detail:
                raise HTTPException(status_code=404, detail=detail) from exc
            raise HTTPException(status_code=422, detail=detail) from exc
        response.headers["Cache-Control"] = "no-store"
        return result

    @app.put("/api/parties/{party_id}/turns/{turn_id}/feedback")
    def update_party_turn_feedback(
        request: Request,
        party_id: str,
        turn_id: int,
        payload: TurnFeedbackUpdate,
    ) -> dict[str, Any]:
        try:
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(request))
            feedback = party_state_store.set_turn_feedback(
                turn_id,
                rating=payload.rating or ("positive" if payload.liked else "none"),
                source_ui="light-gui",
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"party_id": party_id, "feedback": feedback}

    @app.get("/api/parties/{party_id}/requests/{request_id}")
    def get_party_request(request: Request, party_id: str, request_id: str) -> dict[str, Any]:
        if rebuilt_rp_request(request):
            engine = require_rp_engine()
            owner_id = rp_owner_user_id(request)
            try:
                narration_request = engine.get_narration_request_by_request_id(
                    owner_user_id=owner_id,
                    party_id=party_id,
                    request_id=request_id,
                )
            except RPPartyNotFound as exc:
                raise HTTPException(status_code=404, detail="party not found") from exc
            except LookupError:
                return {
                    "party_id": party_id,
                    "request_id": request_id,
                    "status": "unknown",
                    "turn": None,
                    "request": None,
                }
            turn = next(
                (
                    item
                    for item in engine.list_turns(
                        owner_user_id=owner_id, party_id=party_id
                    )
                    if item.id == narration_request.turn_id
                ),
                None,
            )
            return {
                "party_id": party_id,
                "request_id": request_id,
                "status": (
                    "completed"
                    if narration_request.status == "succeeded"
                    else narration_request.status
                ),
                "error": narration_request.last_error,
                "turn": rp_turn_payload(turn) if turn is not None else None,
                "request": rp_request_payload(narration_request),
            }
        try:
            party_store.get_party(party_id, owner_user_id=owner_user_id(request))
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(request))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        turn = party_state_store.get_turn_by_request_id(request_id)
        status = party_state_store.get_turn_request(request_id)
        if turn:
            return {
                "party_id": party_id,
                "request_id": request_id,
                "status": "completed",
                "turn": turn,
                "request": status,
            }
        if status:
            return {
                "party_id": party_id,
                "request_id": request_id,
                "status": status.get("status"),
                "error": status.get("error"),
                "turn": None,
                "request": status,
            }
        return {
            "party_id": party_id,
            "request_id": request_id,
            "status": "unknown",
            "turn": None,
            "request": None,
        }

    @app.get("/api/parties/{party_id}/memory")
    def get_party_memory(request: Request, party_id: str, limit: int = 5) -> dict[str, Any]:
        if rebuilt_rp_request(request):
            try:
                memory = require_rp_engine().latest_story_memory(
                    owner_user_id=rp_owner_user_id(request), party_id=party_id
                )
            except RPPartyNotFound as exc:
                raise HTTPException(status_code=404, detail="party not found") from exc
            return {
                "party_id": party_id,
                "story_memory": (
                    {
                        "id": memory.id,
                        "revision": memory.revision,
                        "base_snapshot_id": memory.base_snapshot_id,
                        "update_id": memory.update_id,
                        "snapshot": memory.snapshot.model_dump(mode="json"),
                        "safe_coverage": memory.snapshot.safe_coverage,
                        "created_at": memory.created_at,
                    }
                    if memory is not None
                    else None
                ),
            }
        try:
            party = party_store.get_party(party_id, owner_user_id=owner_user_id(request))
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(request))
            party_settings = runtime_settings_for_party(party)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        summarizer = MemorySummarizer(party_settings, party_state_store)
        chapters = party_state_store.memory_chapters(limit=limit)
        legacy_summaries = party_state_store.memory_summaries(limit=limit)
        payload = {
            "party_id": party_id,
            "memory": party_state_store.latest_memory_coverage(),
            "summaries": chapters or legacy_summaries,
            "legacy_summaries": legacy_summaries,
            "chapters": chapters,
            "stats": summarizer.stats(),
        }
        if party.scenario_type == "rp":
            story_updater = RPStoryMemoryUpdater(party_settings, party_state_store)
            payload["story_memory"] = story_updater.prompt_snapshot()
            payload["story_memory_stats"] = story_updater.stats()
            if int(party.rp_contract_revision or 0) >= 9:
                payload["player_corrections"] = RPGMService(
                    party_settings,
                    party_state_store,
                ).active_corrections()
        return payload

    @app.get("/api/parties/{party_id}/service-jobs")
    def get_party_service_jobs(request: Request, party_id: str, limit: int = 20) -> dict[str, Any]:
        if rebuilt_rp_request(request):
            engine = require_rp_engine()
            owner_id = rp_owner_user_id(request)
            try:
                service_jobs = engine.list_service_jobs(
                    owner_user_id=owner_id, party_id=party_id
                )
                administrator_jobs = engine.list_administrator_jobs(
                    owner_user_id=owner_id, party_id=party_id
                )
            except RPPartyNotFound as exc:
                raise HTTPException(status_code=404, detail="party not found") from exc
            bounded_limit = min(max(int(limit), 1), 100)
            jobs = [
                *(rp_job_payload(job) for job in service_jobs),
                *(rp_job_payload(job) for job in administrator_jobs),
            ]
            jobs.sort(key=lambda item: (item["created_at"], item["id"]))
            return {"party_id": party_id, "jobs": jobs[-bounded_limit:]}
        try:
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(request))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"party_id": party_id, "jobs": party_state_store.service_jobs(limit=min(max(limit, 1), 100))}

    @app.get("/api/parties/{party_id}/lore-cards")
    def get_party_lore_cards(request: Request, party_id: str, include_archived: bool = False) -> dict[str, Any]:
        if rebuilt_rp_request(request):
            try:
                party = require_rp_engine().get_party(
                    owner_user_id=rp_owner_user_id(request), party_id=party_id
                )
                derived = require_rp_engine().derived_context(
                    owner_user_id=rp_owner_user_id(request), party_id=party_id
                )
            except RPPartyNotFound as exc:
                raise HTTPException(status_code=404, detail="party not found") from exc
            cards = [
                {**card, "origin": "world"}
                for card in party.world_snapshot.seed_lore_cards
            ]
            cards.extend(
                {
                    "id": card.id,
                    "kind": card.kind,
                    "origin": card.origin,
                    "title": card.title,
                    "content": card.content,
                    "keywords": list(card.keywords),
                    "source_turn_id": card.source_turn_id,
                    "source_version": card.source_version,
                    "evidence_span_ids": list(card.evidence_span_ids),
                    "enabled": card.enabled,
                    "created_at": card.created_at,
                }
                for card in derived.runtime_lore_cards
            )
            return {"party_id": party_id, "cards": cards}
        try:
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(request))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"party_id": party_id, "cards": party_state_store.lore_cards(include_archived=include_archived)}

    @app.post("/api/parties/{party_id}/lore-cards/draft")
    async def draft_party_lore_card(
        request: Request,
        party_id: str,
        draft_request: PartyLoreCardDraftRequest,
    ) -> dict[str, Any]:
        try:
            party = party_store.get_party(party_id, owner_user_id=owner_user_id(request))
            ensure_party_playable(party)
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(request))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if party.scenario_type != "rp" or int(party.rp_contract_revision or 0) < 8:
            raise HTTPException(status_code=400, detail="Lore Card drafting requires an RP revision-8 party")

        source_ids = draft_request.source_turn_ids
        source_id_set = set(source_ids)
        selected_turns = [
            turn
            for turn in eligible_rp_turns(
                party_state_store.turns_for_memory(include_noncanonical_fallback=False)
            )
            if int(turn["id"]) in source_id_set
        ]
        selected_ids = [int(turn["id"]) for turn in selected_turns]
        if set(selected_ids) != source_id_set:
            missing = sorted(source_id_set - set(selected_ids))
            raise HTTPException(
                status_code=400,
                detail=f"source_turn_ids must reference complete playable turns; invalid: {missing}",
            )

        source_units = [
            {
                "turn_id": int(turn["id"]),
                "messages": [
                    {"role": role, "content": content}
                    for role, content in rp_turn_messages(turn)
                ],
            }
            for turn in selected_turns
        ]
        payload = lore_card_draft_payload(source_units)
        exact_prompt = service_prompt_text(payload)
        if len(exact_prompt) > LORE_CARD_DRAFT_INPUT_MAX_CHARS:
            raise HTTPException(
                status_code=400,
                detail=f"selected turns exceed the {LORE_CARD_DRAFT_INPUT_MAX_CHARS}-character Lore Card draft input limit",
            )
        request_id = f"lore-card-draft:{uuid.uuid4().hex}"
        try:
            completion = await ServiceModelClient(settings).complete(
                role="lore_card_draft",
                provider="openrouter",
                model=LORE_CARD_DRAFT_MODEL,
                party_id=party_id,
                turn_id=max(selected_ids),
                request_id=request_id,
                party_turn=int(selected_turns[-1].get("party_turn") or 0),
                attempt=1,
                prompt=exact_prompt,
                payload=payload,
            )
            choice = completion.data.get("choices", [{}])[0]
            if isinstance(choice, dict) and choice.get("finish_reason") == "length":
                raise ValueError("Lore Card draft response was truncated by the output limit")
            draft = PartyLoreCardDraft.model_validate(json.loads(response_text(completion.data)))
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise HTTPException(status_code=502, detail=f"Lore Card draft failed: {exc}") from exc
        return {
            "party_id": party_id,
            "request_id": request_id,
            "draft": {
                **draft.model_dump(mode="json"),
                "source_turn_ids": selected_ids,
            },
        }

    @app.post("/api/parties/{party_id}/lore-cards")
    def create_party_lore_card(request: Request, party_id: str, card: PartyLoreCardCreate) -> dict[str, Any]:
        try:
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(request))
            created = party_state_store.create_lore_card(**card.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        party_state_store.audit(
            "lore_card_created",
            {
                "card_id": created["id"],
                "title": created["title"],
                "source_turn_ids": created["source_turn_ids"],
                "confirmed_by_player": True,
            },
        )
        return {"party_id": party_id, "card": created}

    @app.patch("/api/parties/{party_id}/lore-cards/{card_id}")
    def update_party_lore_card(
        request: Request,
        party_id: str,
        card_id: int,
        card: PartyLoreCardUpdate,
    ) -> dict[str, Any]:
        try:
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(request))
            updated = party_state_store.update_lore_card(card_id, card.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        party_state_store.audit("lore_card_updated", {"card_id": updated["id"], "archived": updated["archived"]})
        return {"party_id": party_id, "card": updated}

    @app.get("/api/parties/{party_id}/checkpoints")
    def get_party_checkpoints(
        request: Request,
        party_id: str,
        limit: int = 50,
        include_state: bool = False,
    ) -> dict[str, Any]:
        try:
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(request))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "party_id": party_id,
            "checkpoints": party_state_store.memory_checkpoints(
                limit=min(max(limit, 1), 100),
                include_state=include_state,
            ),
        }

    @app.post("/api/parties/{party_id}/checkpoints")
    def create_party_checkpoint(request: Request, party_id: str, checkpoint: PartyCheckpointCreate) -> dict[str, Any]:
        try:
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(request))
            created = party_state_store.create_memory_checkpoint(checkpoint.label)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        party_state_store.audit(
            "memory_checkpoint_created",
            {"checkpoint_id": created["id"], "through_turn_id": created["through_turn_id"]},
        )
        return {"party_id": party_id, "checkpoint": created}

    @app.get("/api/parties/{party_id}/branches")
    def get_party_branches(request: Request, party_id: str, limit: int = 100) -> dict[str, Any]:
        try:
            branches = party_store.list_party_branches(
                party_id,
                owner_user_id=owner_user_id(request),
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"party_id": party_id, "branches": branches}

    @app.post("/api/parties/{party_id}/branches")
    def create_party_branch(request: Request, party_id: str, payload: PartyBranchCreate) -> dict[str, Any]:
        try:
            branch = party_store.create_party_branch(
                party_id=party_id,
                checkpoint_id=payload.checkpoint_id,
                label=payload.label,
                owner_user_id=owner_user_id(request),
                rp_contract_revision=payload.rp_contract_revision,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"party_id": party_id, "branch": branch}

    @app.get("/api/parties/{party_id}/branches/{branch_id}")
    def get_party_branch(request: Request, party_id: str, branch_id: str, limit: int = 200) -> dict[str, Any]:
        owner_id = owner_user_id(request)
        try:
            party = party_store.get_party(party_id, owner_user_id=owner_id)
            branch = party_store.get_party_branch(party_id, branch_id, owner_user_id=owner_id)
            branch_store = party_store.store_for_branch(party_id, branch_id, owner_user_id=owner_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        branch_state = branch_store.get_state()
        return {
            "party": party.model_dump(mode="json"),
            "branch": branch,
            "state": branch_state,
            "turns": branch_store.turn_history(limit=min(max(limit, 1), 500)),
            "state_versions": branch_store.history(limit=min(max(limit, 1), 500)),
            "characters": party_character_sheets(branch_state),
        }

    @app.post("/api/parties/{party_id}/memory/summarize")
    async def summarize_party_memory(
        http_request: Request,
        party_id: str,
        request: PartyMemorySummarizeRequest = PartyMemorySummarizeRequest(),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        try:
            party = party_store.get_party(party_id, owner_user_id=owner_user_id(http_request))
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(http_request))
            party_settings = runtime_settings_for_party(party)
            story_result = None
            if party.scenario_type == "rp":
                story_result = await RPStoryMemoryUpdater(party_settings, party_state_store).update(
                    authorization,
                    force=request.force,
                    fail_open=False,
                )
            result = await MemorySummarizer(party_settings, party_state_store).summarize(
                authorization,
                force=request.force,
                fail_open=False,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            raise HTTPException(status_code=502, detail=f"Narrative provider HTTP {status}") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload = {"party_id": party_id, **result}
        if story_result is not None:
            payload["story_generated"] = story_result["generated"]
            payload["story_memory"] = story_result["story_memory"]
            payload["story_memory_stats"] = story_result["stats"]
            payload["generated"] = bool(result.get("generated") or story_result.get("generated"))
            if story_result.get("generated"):
                payload["reason"] = "generated"
        return payload

    @app.delete("/api/parties/{party_id}/memory/latest")
    def delete_party_memory_latest(request: Request, party_id: str) -> dict[str, Any]:
        try:
            party = party_store.get_party(party_id, owner_user_id=owner_user_id(request))
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(request))
            party_settings = runtime_settings_for_party(party)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        deleted = party_state_store.delete_latest_memory_coverage()
        return {
            "party_id": party_id,
            "deleted": deleted is not None,
            "deleted_memory": deleted,
            "memory": party_state_store.latest_memory_coverage(),
            "stats": MemorySummarizer(party_settings, party_state_store).stats(),
        }

    @app.get("/api/parties/{party_id}/context")
    def get_party_context(
        request: Request,
        party_id: str,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            owner_id = owner_user_id(request)
            party = party_store.get_party(party_id, owner_user_id=owner_id)
            if branch_id is not None:
                party_state_store = party_store.store_for_branch(
                    party_id,
                    branch_id,
                    owner_user_id=owner_id,
                )
                party_settings = runtime_settings_for_branch(party, branch_id)
            else:
                party_state_store = party_store.store_for_party(
                    party_id,
                    owner_user_id=owner_id,
                )
                party_settings = runtime_settings_for_party(party)
            model_profile = party.model_profile or party_store.get_model_profile(party.model_profile_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        payload = {
            "party_id": party_id,
            "context": estimate_party_context(party_state_store, party_settings, model_profile),
        }
        if branch_id is not None:
            payload["branch_id"] = branch_id
        return payload

    @app.get("/api/parties/{party_id}/characters")
    def get_party_characters(request: Request, party_id: str) -> dict[str, Any]:
        try:
            party = party_store.get_party(party_id, owner_user_id=owner_user_id(request))
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(request))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "party_id": party_id,
            "state_campaign_id": party.state_campaign_id,
            "characters": party_character_sheets(party_state_store.get_state()),
        }

    @app.get("/api/parties/{party_id}/supervisor")
    def get_party_supervisor(request: Request, party_id: str) -> dict[str, Any]:
        if rebuilt_rp_request(request):
            engine = require_rp_engine()
            owner_id = rp_owner_user_id(request)
            try:
                party = engine.get_party(owner_user_id=owner_id, party_id=party_id)
                narration_requests = engine.list_narration_requests(
                    owner_user_id=owner_id, party_id=party_id
                )
                service_jobs = engine.list_service_jobs(
                    owner_user_id=owner_id, party_id=party_id
                )
                administrator_jobs = engine.list_administrator_jobs(
                    owner_user_id=owner_id, party_id=party_id
                )
            except RPPartyNotFound as exc:
                raise HTTPException(status_code=404, detail="party not found") from exc
            return {
                "party_id": party_id,
                "roles": {
                    "narrator": rp_role_status(
                        role="narrator",
                        enabled=settings.rp_narrator_enabled,
                        provider=party.narrator_provider,
                        model=party.narrator_model,
                        work=narration_requests,
                    ),
                    "atomic_service": rp_role_status(
                        role="atomic_service",
                        enabled=settings.rp_atomic_service_enabled,
                        provider=str(atomic_choice["provider"]),
                        model=str(atomic_choice["model"]),
                        work=service_jobs,
                    ),
                    "administrator": rp_role_status(
                        role="administrator",
                        enabled=settings.rp_administrator_enabled,
                        provider=str(administrator_choice["provider"]),
                        model=str(administrator_choice["model"]),
                        work=administrator_jobs,
                    ),
                },
            }
        try:
            party = party_store.get_party(party_id, owner_user_id=owner_user_id(request))
            party_state_store = party_store.store_for_party(
                party_id,
                owner_user_id=owner_user_id(request),
            )
            party_settings = runtime_settings_for_party(party)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            contract = rp_supervisor_contract_for_party(party)
            if contract is None:
                return {"party_id": party_id, "enabled": False}
            return {
                "party_id": party_id,
                **RPSupervisorService(
                    party_settings,
                    party_state_store,
                    contract,
                ).status_payload(),
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/parties/{party_id}/administrator/proposals")
    def list_party_administrator_proposals(
        request: Request, party_id: str
    ) -> dict[str, Any]:
        if not rebuilt_rp_request(request):
            raise HTTPException(
                status_code=404, detail="Administrator proposals are unavailable"
            )
        try:
            proposals = require_rp_engine().list_administrator_proposals(
                owner_user_id=rp_owner_user_id(request), party_id=party_id
            )
        except RPPartyNotFound as exc:
            raise HTTPException(status_code=404, detail="party not found") from exc
        return {
            "party_id": party_id,
            "proposals": [rp_proposal_payload(item) for item in proposals],
        }

    @app.post(
        "/api/parties/{party_id}/administrator/proposals/{proposal_id}/decision"
    )
    def decide_party_administrator_proposal(
        request: Request,
        party_id: str,
        proposal_id: int,
        payload: RPAdministratorProposalDecision,
    ) -> dict[str, Any]:
        if not rebuilt_rp_request(request):
            raise HTTPException(
                status_code=404, detail="Administrator proposals are unavailable"
            )
        try:
            proposal = require_rp_engine().decide_administrator_proposal(
                owner_user_id=rp_owner_user_id(request),
                party_id=party_id,
                proposal_id=proposal_id,
                decision=payload.decision,
            )
            party = require_rp_engine().get_party(
                owner_user_id=rp_owner_user_id(request), party_id=party_id
            )
        except RPPartyNotFound as exc:
            raise HTTPException(status_code=404, detail="party not found") from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="proposal not found") from exc
        except RPAdministratorProposalConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "party_id": party_id,
            "state_version": party.current_version,
            "proposal": rp_proposal_payload(proposal),
        }

    @app.post("/api/parties/{party_id}/characters/edit")
    def party_character_edit(http_request: Request, party_id: str, request: PartyCharacterStateEditRequest) -> dict[str, Any]:
        try:
            party = party_store.get_party(party_id, owner_user_id=owner_user_id(http_request))
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(http_request))
            party_settings = runtime_settings_for_party(party)
            scene_state_enabled = (
                party.scenario_type == "rp" and party_settings.rp_contract_revision == 7
            )
            patch = character_state_patch(party_state_store.get_state(), request)
            party_state_store.create_patch_proposal(patch)
            candidate = party_state_store.preview_patch(
                patch,
                scene_state_enabled=scene_state_enabled,
            )
            if request.confirm:
                state = party_state_store.apply_pending_patch(
                    patch.check_id or "latest",
                    reason="party_character_edit_confirm",
                    scene_state_enabled=scene_state_enabled,
                )
                return {"party_id": party_id, "applied": True, "proposal_id": patch.check_id, "state": state}
        except (PatchError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "party_id": party_id,
            "applied": False,
            "proposal_id": patch.check_id,
            "proposal": patch.model_dump(mode="json"),
            "candidate": candidate,
        }

    @app.post("/api/parties/{party_id}/characters/generate")
    async def party_character_generate(
        http_request: Request,
        party_id: str,
        request: PartyCharacterStateEditRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        request_id = f"character_generate_{uuid.uuid4().hex[:12]}"
        try:
            party = party_store.get_party(party_id, owner_user_id=owner_user_id(http_request))
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(http_request))
            party_settings = runtime_settings_for_party(party)
            generated = await generate_character_edit(party_settings, party_state_store, request, authorization, request_id)
            patch = character_state_patch(party_state_store.get_state(), generated)
            state = party_state_store.apply_state_patch(
                patch,
                reason=f"party_character_generate:{request_id}",
                scene_state_enabled=(
                    party.scenario_type == "rp" and party_settings.rp_contract_revision == 7
                ),
            )
            character_id = stable_character_id(generated.character_id or generated.name or "")
            party_state_store.audit(
                "party_character_generate",
                {
                    "request_id": request_id,
                    "character_id": character_id,
                    "model": service_model_settings(party_settings).intent_model,
                    "service_model": True,
                },
                request_id,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            raise HTTPException(status_code=502, detail=f"Narrative provider HTTP {status}") from exc
        except httpx.TimeoutException as exc:
            raise HTTPException(status_code=502, detail="Narrative provider timed out") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except (PatchError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "party_id": party_id,
            "applied": True,
            "request_id": request_id,
            "character_id": character_id,
            "generated": generated.model_dump(mode="json"),
            "patch": patch.model_dump(mode="json"),
            "state": state,
        }

    @app.post("/api/parties/{party_id}/prompt/preview")
    def preview_party_prompt(
        http_request: Request,
        party_id: str,
        request: PartyPromptPreviewRequest,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            owner_id = owner_user_id(http_request)
            party = party_store.get_party(party_id, owner_user_id=owner_id)
            if branch_id is not None:
                party_state_store = party_store.store_for_branch(
                    party_id,
                    branch_id,
                    owner_user_id=owner_id,
                )
                party_settings = runtime_settings_for_branch(party, branch_id)
            else:
                party_state_store = party_store.store_for_party(
                    party_id,
                    owner_user_id=owner_id,
                )
                party_settings = runtime_settings_for_party(party)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            inspector = PromptInspector(party_settings, party_state_store)
            inspector.relationship_model = relationship_model_for_party(party)
            inspector.scene_contract = scene_contract_for_party(party)
            world_clock_contract = world_clock_contract_for_party(
                party,
                effective_revision=party_settings.rp_contract_revision,
            )
            if world_clock_contract is not None:
                inspector.world_clock = WorldClockService(
                    party_settings,
                    party_state_store,
                    world_clock_contract,
                )
            supervisor_contract = rp_supervisor_contract_for_party(party)
            if supervisor_contract is not None:
                inspector.rp_supervisor = RPSupervisorService(
                    party_settings,
                    party_state_store,
                    supervisor_contract,
                )
            preview = inspector.preview(request.content, source=request.source)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload = {"party_id": party_id, "preview": preview}
        if branch_id is not None:
            payload["branch_id"] = branch_id
        return payload

    @app.post("/api/parties/{party_id}/start")
    async def start_party(
        http_request: Request,
        party_id: str,
        request: PartyStartRequest | RPPartyStartRequest = PartyStartRequest(),
        authorization: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
    ) -> dict[str, Any]:
        if rebuilt_rp_request(http_request):
            if request.model_fields_set.intersection({"temperature", "max_tokens"}):
                raise HTTPException(
                    status_code=422,
                    detail="rebuilt RP start accepts only idempotency_key",
                )
            engine = require_rp_engine()
            owner_id = rp_owner_user_id(http_request)
            idempotency_key = request.idempotency_key or f"party-start:{party_id}"
            try:
                party = engine.get_party(owner_user_id=owner_id, party_id=party_id)
                ensure_rebuilt_narrator_binding(party)
                try:
                    existing_request = engine.get_narration_request(
                        owner_user_id=owner_id,
                        party_id=party_id,
                        idempotency_key=idempotency_key,
                    )
                except LookupError:
                    existing_request = None
                if party.current_version != 0 and existing_request is None:
                    raise RPPartyVersionConflict("party already has committed history")
                request_id = (
                    existing_request.request_id
                    if existing_request is not None and x_request_id is None
                    else (x_request_id or idempotency_key)
                )
                if (
                    not settings.rp_narrator_enabled
                    and (
                        existing_request is None
                        or existing_request.status != "succeeded"
                    )
                ):
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "code": "rp_narrator_disabled",
                            "retryable": True,
                            "request_id": request_id,
                            "idempotency_key": idempotency_key,
                        },
                    )
                turn = await rp_narrator_service_for(
                    party, request_id
                ).narrate_opening(
                    owner_user_id=owner_id,
                    party_id=party_id,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                )
                current_party = engine.get_party(
                    owner_user_id=owner_id, party_id=party_id
                )
            except RPPartyNotFound as exc:
                raise HTTPException(status_code=404, detail="party not found") from exc
            except (RPIdempotencyConflict, RPPartyVersionConflict) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except RPNarratorUnavailable as exc:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "code": "rp_narrator_unavailable",
                        "message": str(exc),
                        "retryable": True,
                        "request_id": request_id,
                        "idempotency_key": idempotency_key,
                    },
                ) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {
                "party_id": party_id,
                "started": existing_request is None,
                "already_started": existing_request is not None,
                "state_version": current_party.current_version,
                "message": {"role": "assistant", "content": turn.narrator_text},
                "turn": rp_turn_payload(turn),
            }
        try:
            party = party_store.get_party(
                party_id,
                owner_user_id=owner_user_id(http_request),
                allow_retired_read=True,
            )
            ensure_party_playable(party)
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(http_request))
            party_settings = runtime_settings_for_party(party)
            party_settings = replace(
                party_settings,
                model_attempt_timeout_seconds=party_settings.party_start_model_attempt_timeout_seconds,
            )
            model_profile = party.model_profile or party_store.get_model_profile(party.model_profile_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        idempotency_key = request.idempotency_key or f"party-start:{party_id}"
        request_id = x_request_id or f"req_{uuid.uuid4().hex}"
        existing = party_state_store.get_turn_by_idempotency(idempotency_key)
        if existing:
            message = existing.get("choices", [{}])[0].get("message", {"role": "assistant", "content": ""})
            return {
                "party_id": party_id,
                "started": False,
                "already_started": True,
                "reason": "idempotency_key_exists",
                "state_version": party_state_store.current_version(),
                "message": message,
                "raw": existing,
            }

        existing_turns = party_state_store.turn_history(limit=1)
        if existing_turns:
            return {
                "party_id": party_id,
                "started": False,
                "already_started": True,
                "reason": "history_exists",
                "state_version": party_state_store.current_version(),
                "latest_turn": existing_turns[-1],
            }

        try:
            request_status = party_state_store.begin_turn_request(idempotency_key, request_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        request_id = str(request_status.get("request_id") or request_id)
        if not request_status.get("acquired"):
            if request_status.get("status") == "completed" and request_status.get("response"):
                response = request_status["response"]
                message = response.get("choices", [{}])[0].get("message", {"role": "assistant", "content": ""})
                return {
                    "party_id": party_id,
                    "started": False,
                    "already_started": True,
                    "reason": "request_completed",
                    "state_version": party_state_store.current_version(),
                    "message": message,
                    "raw": response,
                }
            if request_status.get("status") == "running":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "status": "running",
                        "request_id": request_status.get("request_id") or request_id,
                        "idempotency_key": idempotency_key,
                        "message": "request is already running",
                    },
                )

        adjudicator = Adjudicator(
            party_settings,
            party_state_store,
            relationship_model=relationship_model_for_party(party),
            scene_contract=scene_contract_for_party(party),
            world_clock_contract=world_clock_contract_for_party(
                party,
                effective_revision=party_settings.rp_contract_revision,
            ),
            rp_supervisor_contract=rp_supervisor_contract_for_party(party),
        )
        narrative = adjudicator.narrative
        expected_party_turn = int(party_state_store.get_state().get("meta", {}).get("turn", 0)) + 1
        adjudicator.record_trace_event(
            request_id=request_id,
            phase_key="player_input",
            alignment_key="player_input",
            lane="main",
            event_type="player_input",
            status="completed",
            payload={
                "input": {
                    "content": AUTO_START_HISTORY_MESSAGE,
                    "source": "system_auto_start",
                }
            },
            party_turn=expected_party_turn,
        )

        def trace_start_failure(exc: Exception) -> None:
            adjudicator.record_trace_event(
                request_id=request_id,
                phase_key="request_failed",
                alignment_key="request_terminal",
                lane="main",
                event_type="request_failed",
                status="failed",
                payload={"error": {"type": type(exc).__name__, "message": str(exc)[:1000]}},
                party_turn=expected_party_turn,
            )

        try:
            state = party_state_store.get_state()
            revision_seven = (
                party.scenario_type == "rp"
                and party_settings.rp_contract_revision >= 7
            )
            revision_eight = (
                party.scenario_type == "rp"
                and party_settings.rp_contract_revision >= 8
            )
            scene_bundle_revision = (
                party.scenario_type == "rp"
                and party_settings.rp_contract_revision == 7
            )
            expected_state_version = int(state.get("meta", {}).get("state_version") or 0)
            start_patch = party_start_state_patch(
                state,
                party_id,
                party.worldpack_id,
                party.scenario_type,
            )
            narrative_state = party_start_narrative_state(state, start_patch)
            prompt = party_start_prompt(party_store, party)
            opening_repair_prompt = (
                worldpack_prompt_text(
                    party,
                    "opening_scene",
                    effective_revision=party_settings.rp_contract_revision,
                )
                if party_settings.rp_contract_revision >= 11
                else None
            )
            chat_request = ChatCompletionRequest(
                model=model_profile.model,
                messages=[ChatMessage(role="user", content=prompt)],
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                stream=False,
            )
            apply_party_narrator_settings(
                chat_request,
                model_profile.provider,
                model_profile.model,
                party.narrator_settings,
            )
            start_outcome = party_start_outcome(
                party_id,
                party.scenario_type,
                party_settings.rp_contract_revision,
            )
            if scene_bundle_revision:
                start_outcome.scene_allowance = SceneAllowance.model_validate(
                    build_scene_transition_allowance(
                        state,
                        AUTO_START_HISTORY_MESSAGE,
                        character_aliases=normalized_aliases(
                            adjudicator.relationship_model or {}
                        ),
                        authored_stable_affiliations=adjudicator.authored_stable_affiliations(),
                    )
                )
                start_outcome.authoritative_block += (
                    "\n<SCENE_TRANSITION_ALLOWANCE>\n"
                    + json.dumps(
                        start_outcome.scene_allowance.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n</SCENE_TRANSITION_ALLOWANCE>"
                )
            memory_summary = (
                None
                if party_settings.rp_contract_revision >= 8
                else party_state_store.memory_for_prompt(
                    party_settings.party_memory_prompt_max_chars
                )
            )
            rp_story_memory = (
                adjudicator.rp_story_memory.prompt_snapshot()
                if adjudicator.rp_story_memory is not None
                else None
            )
            world_clock_projection = (
                adjudicator.world_clock.prompt_projection(narrative_state)
                if adjudicator.world_clock is not None
                else None
            )
            world_events = (
                str(world_clock_projection["block"])
                if world_clock_projection is not None
                else None
            )
            supervisor_advisory = (
                adjudicator.rp_supervisor.prompt_advisory()
                if adjudicator.rp_supervisor is not None
                else None
            )
            prompt_messages = narrative.narrative_messages(
                chat_request,
                narrative_state,
                start_outcome,
                repair_instruction=None,
                memory_summary=memory_summary,
                rp_story_memory=rp_story_memory,
                world_events=world_events,
                supervisor_advisory=supervisor_advisory,
            )
            opening_prompt_assembly = (
                prompt_assembly_diagnostics(
                    prompt_messages,
                    story_memory_covered_through_turn_id=(
                        story_memory_safe_coverage(rp_story_memory)
                        if party_settings.rp_contract_revision >= 8
                        else int(rp_story_memory.get("to_turn_id") or 0)
                        if rp_story_memory
                        else 0
                    ),
                    raw_tail_turn_ids=[],
                    rp_contract_revision=party_settings.rp_contract_revision,
                )
                if revision_seven
                else None
            )
            adjudicator.record_trace_event(
                request_id=request_id,
                phase_key="gateway_assembly",
                alignment_key="gateway_assembly",
                lane="main",
                event_type="gateway_assembly",
                status="completed",
                payload={
                    "capture_status": "complete",
                    "input": {"messages": prompt_messages},
                    "details": {
                        "message_count": len(prompt_messages),
                        "assembly_trace": adjudicator.prompt_assembly_trace(prompt_messages, prompt),
                    },
                },
                party_turn=expected_party_turn,
            )
            repaired = False
            fallback_reason: str | None = None
            transport_status = "ok"
            fallback_noncanonical = False
            opening_prompt_cache_response: dict[str, Any] | None = None
            scene_result: SceneMaterialization | None = None
            scene_before = (
                initial_scene_state(state, adjudicator.authored_stable_affiliations())
                if scene_bundle_revision
                else None
            )
            if revision_seven:
                current_state_version = int(party_state_store.current_version() or 0)
                if current_state_version != expected_state_version:
                    party_state_store.audit(
                        "state_version_conflict_pre_provider",
                        {
                            "request_id": request_id,
                            "expected_state_version": expected_state_version,
                            "current_state_version": current_state_version,
                            "opening_scene": True,
                        },
                        request_id,
                    )
                    raise StateVersionConflict(
                        "state version changed during opening assembly: "
                        f"expected {expected_state_version}, current {current_state_version}"
                    )
            try:
                raw = await narrative.complete(
                    chat_request,
                    narrative_state,
                    start_outcome,
                    authorization,
                    memory_summary=memory_summary,
                    rp_story_memory=rp_story_memory,
                    request_id=request_id,
                    world_events=world_events,
                    supervisor_advisory=supervisor_advisory,
                )
                opening_prompt_cache_response = raw
            except (
                httpx.HTTPStatusError,
                httpx.TimeoutException,
                ProviderRateLimitError,
                httpx.RequestError,
                RuntimeError,
            ) as exc:
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code if exc.response is not None else "unknown"
                    fallback_reason = f"http_{status}"
                    transport_status = "provider_error"
                elif isinstance(exc, httpx.TimeoutException):
                    fallback_reason = "timeout"
                    transport_status = "provider_timeout"
                elif isinstance(exc, ProviderRateLimitError):
                    fallback_reason = "rate_limited"
                    transport_status = "provider_error"
                elif isinstance(exc, httpx.RequestError):
                    fallback_reason = "network_error"
                    transport_status = "provider_error"
                else:
                    fallback_reason = "runtime_error"
                    transport_status = "provider_error"
                revision_seven_transport = revision_seven and isinstance(
                    exc,
                    (
                        httpx.HTTPStatusError,
                        httpx.TimeoutException,
                        ProviderRateLimitError,
                        httpx.RequestError,
                    ),
                )
                if not revision_seven_transport:
                    raise
                text = safe_fallback(
                    start_outcome,
                    narrative_state,
                    "",
                    party.worldpack_id,
                    party.scenario_type,
                )
                raw = adjudicator.provider_fallback_response(
                    start_outcome,
                    text,
                    fallback_reason,
                    request_id,
                    audit=False,
                )
                fallback_noncanonical = True
            if scene_bundle_revision and not fallback_noncanonical:
                scene_result = materialize_scene_bundle(
                    raw,
                    state,
                    latest_user_message=AUTO_START_HISTORY_MESSAGE,
                    party_turn=expected_party_turn,
                    authoritative_outcome={
                        **start_outcome.model_dump(mode="json"),
                        "scene_allowance": (
                            start_outcome.scene_allowance.model_dump(mode="json")
                            if start_outcome.scene_allowance is not None
                            else None
                        ),
                    },
                )
                if scene_result.valid:
                    raw = Adjudicator.with_narrative_text(raw, scene_result.text)
            response = adjudicator.normalize_response(raw, model_profile.model)
            text = scene_result.text if scene_result is not None else response_text(response)
            if scene_result is not None and scene_result.valid:
                response = adjudicator.normalize_response(
                    Adjudicator.with_narrative_text(response, text),
                    model_profile.model,
                )
            if (
                party.scenario_type == "rp"
                and not text.strip()
                and not (scene_result is not None and not scene_result.valid)
            ):
                party_state_store.audit(
                    "llm_invalid_response",
                    {
                        "request_id": request_id,
                        "model": model_profile.model,
                        "reason": "empty_response",
                    },
                    request_id,
                )
                raise RuntimeError("Narrative provider returned an invalid response")
            response = Adjudicator.with_narrative_text(response, text)
            validation = (
                None
                if party_settings.rp_contract_revision < 3
                else adjudicator.validator.validate(
                    text,
                    start_outcome,
                    narrative_state,
                    campaign_id=party.worldpack_id,
                    scenario_type=party.scenario_type,
                )
            )
            if validation is not None:
                initial_violations = [
                    *validation.violations,
                    *(scene_result.violations if scene_result else []),
                ]
                adjudicator.record_trace_event(
                    request_id=request_id,
                    phase_key="validation:initial",
                    alignment_key="validation",
                    lane="main",
                    event_type="validation",
                    status="completed" if not initial_violations else "failed",
                    payload={
                        "input": {"response": text},
                        "output": {
                            "valid": not initial_violations,
                            "violations": initial_violations,
                        },
                        "metadata": {"repair": False, "opening_scene": True},
                    },
                    party_turn=expected_party_turn,
                )
            repair_attempts = 1 if revision_seven else party_settings.max_repair_attempts
            if (
                validation is not None
                and (
                    not validation.valid
                    or (scene_result is not None and not scene_result.valid)
                )
                and repair_attempts > 0
            ):
                repaired = True
                repair_instruction = validation.repair_instruction
                if scene_result is not None and not scene_result.valid:
                    repair_instruction = " ".join(
                        [repair_instruction, scene_result.repair_instruction]
                    ).strip()
                if revision_seven:
                    current_state_version = int(party_state_store.current_version() or 0)
                    if current_state_version != expected_state_version:
                        party_state_store.audit(
                            "state_version_conflict_pre_provider",
                            {
                                "request_id": request_id,
                                "expected_state_version": expected_state_version,
                                "current_state_version": current_state_version,
                                "opening_scene": True,
                                "repair": True,
                            },
                            request_id,
                        )
                        raise StateVersionConflict(
                            "state version changed before opening repair: "
                            f"expected {expected_state_version}, current {current_state_version}"
                        )
                raw = await narrative.complete(
                    chat_request,
                    narrative_state,
                    start_outcome,
                    authorization,
                    repair_instruction,
                    failed_response_text=text,
                    memory_summary=memory_summary,
                    rp_story_memory=rp_story_memory,
                    request_id=request_id,
                    world_events=world_events,
                    supervisor_advisory=supervisor_advisory,
                    opening_prompt=opening_repair_prompt,
                )
                if scene_bundle_revision:
                    scene_result = materialize_scene_bundle(
                        raw,
                        state,
                        latest_user_message=AUTO_START_HISTORY_MESSAGE,
                        party_turn=expected_party_turn,
                        authoritative_outcome={
                            **start_outcome.model_dump(mode="json"),
                            "scene_allowance": (
                                start_outcome.scene_allowance.model_dump(mode="json")
                                if start_outcome.scene_allowance is not None
                                else None
                            ),
                        },
                    )
                    if scene_result.valid:
                        raw = Adjudicator.with_narrative_text(raw, scene_result.text)
                response = adjudicator.normalize_response(raw, model_profile.model)
                text = scene_result.text if scene_result is not None else response_text(response)
                response = Adjudicator.with_narrative_text(response, text)
                validation = adjudicator.validator.validate(
                    text,
                    start_outcome,
                    narrative_state,
                    campaign_id=party.worldpack_id,
                    scenario_type=party.scenario_type,
                )
                repair_violations = [
                    *validation.violations,
                    *(scene_result.violations if scene_result else []),
                ]
                adjudicator.record_trace_event(
                    request_id=request_id,
                    phase_key="validation:repair",
                    alignment_key="validation",
                    lane="main",
                    event_type="validation",
                    status="completed" if not repair_violations else "failed",
                    payload={
                        "input": {"response": text},
                        "output": {
                            "valid": not repair_violations,
                            "violations": repair_violations,
                        },
                        "metadata": {"repair": True, "opening_scene": True},
                    },
                    party_turn=expected_party_turn,
                )
            if scene_result is not None and not scene_result.valid:
                raise SceneContinuityError("; ".join(scene_result.violations))
            if validation is not None and not validation.valid:
                fallback_reason = fallback_reason or "validation_failed"
                transport_status = "invalid_response"
                party_state_store.audit(
                    "party_start_validation_failed",
                    {
                        "request_id": request_id,
                        "model": model_profile.model,
                        "violations": validation.violations,
                    },
                    request_id,
                )
                raise RuntimeError("LLM response failed narrative validation")
            final_validation = (
                None
                if party_settings.rp_contract_revision < 3
                else adjudicator.validator.validate(
                    text,
                    start_outcome,
                    narrative_state,
                    campaign_id=party.worldpack_id,
                    scenario_type=party.scenario_type,
                )
            )
            final_violations = (
                final_validation.violations if final_validation is not None else []
            )
            adjudicator.record_trace_event(
                request_id=request_id,
                phase_key="validation:final",
                alignment_key="validation",
                lane="main",
                event_type="validation",
                status="completed" if not final_violations else "failed",
                payload={
                    "input": {"response": text},
                    "output": {
                        "valid": (
                            final_validation.valid
                            if final_validation is not None
                            else None
                        ),
                        "violations": final_violations,
                        "reason": (
                            None if final_validation is not None else "not_applicable"
                        ),
                    },
                    "metadata": {"repair": repaired, "opening_scene": True},
                },
                party_turn=expected_party_turn,
            )

            turn_metadata = {
                "schema_version": "rp-gateway.turn.v1",
                "turn_kind": "opening_scene",
                "scenario_type": party.scenario_type,
                "rp_contract_version": getattr(party, "rp_contract_version", "rp-core.v1"),
                "rp_contract_revision": int(getattr(party, "rp_contract_revision", 0) or 0),
                "worldpack_id": party.worldpack_id,
                "state_campaign_id": party_state_store.campaign_id,
                "narrative_provider": party_settings.llm_provider,
                "narrative_model": model_profile.model,
                "generated_by": "human",
                "validator_valid": final_validation.valid if final_validation is not None else None,
                "repaired": repaired,
                "fallback": fallback_reason is not None,
                "fallback_reason": fallback_reason,
                "transport_status": transport_status,
                "llm_calls": 2 if repaired else 1,
                "outcome": start_outcome.model_dump(mode="json"),
            }
            if opening_prompt_assembly is not None:
                turn_metadata["prompt_assembly"] = opening_prompt_assembly
            if world_clock_projection is not None and not fallback_noncanonical:
                turn_metadata["world_clock_events"] = dict(
                    world_clock_projection["metadata"]
                )
            if revision_eight:
                turn_metadata.update(
                    prompt_cache_observability(
                        opening_prompt_cache_response or response,
                        prompt_messages,
                        history_units=party_settings.effective_rp_raw_history_window_turns,
                    )
                )
            if revision_seven:
                commit_patch = (
                    StatePatch(
                        turn=expected_party_turn,
                        check_id=f"party_start:{party_id}",
                        source="rp-gateway-opening",
                        patch=[],
                    )
                    if fallback_noncanonical
                    else start_patch
                    or StatePatch(
                        turn=expected_party_turn,
                        check_id=f"party_start:{party_id}",
                        source="rp-gateway-opening",
                        patch=[],
                    )
                )
                turn_metadata["story_memory_canonical"] = not fallback_noncanonical
                if scene_bundle_revision:
                    scene_after = (
                        fallback_scene_state(
                            state,
                            adjudicator.authored_stable_affiliations(),
                        )
                        if fallback_noncanonical
                        else scene_result.scene_state
                        if scene_result is not None
                        else None
                    )
                    if scene_after is None:
                        raise SceneContinuityError("opening has no scene projection")
                    if not fallback_noncanonical:
                        commit_patch.patch.extend(
                            adjudicator.scene_legacy_operations(
                                state,
                                scene_result,
                                expected_party_turn,
                            )
                        )
                    commit_patch.patch.append(
                        PatchOperation(
                            op="replace" if "scene_state" in state else "add",
                            path="/scene_state",
                            value=scene_after,
                            reason="Commits the deterministic opening scene projection.",
                            turn=expected_party_turn,
                        )
                    )
                    turn_metadata.update(
                        {
                            "scene_claims": scene_result.claims if scene_result is not None else None,
                            "applied_scene_delta": (
                                scene_result.applied_operations if scene_result is not None else []
                            ),
                            "dropped_scene_delta": (
                                scene_result.dropped_operations if scene_result is not None else []
                            ),
                            "scene_state_before": scene_before,
                            "scene_state_after": scene_after,
                            "scene_state_stale": bool(scene_after.get("stale")),
                        }
                    )
                atomic_audits: list[tuple[str, dict[str, Any]]] = []
                if (
                    scene_bundle_revision
                    and scene_result is not None
                    and scene_result.dropped_operations
                ):
                    atomic_audits.append(
                        (
                            "scene_delta_operations_dropped",
                            {
                                "request_id": request_id,
                                "dropped_scene_delta": scene_result.dropped_operations,
                            },
                        )
                    )
                if fallback_noncanonical:
                    atomic_audits.append(
                        (
                            "llm_safe_fallback",
                            {
                                "request_id": request_id,
                                "check_id": start_outcome.check_id,
                                "model": model_profile.model,
                                "reason": fallback_reason,
                                "story_memory_canonical": False,
                            },
                        )
                    )
                atomic_audits.append(
                    (
                        "party_start_complete",
                        {
                            "request_id": request_id,
                            "model": model_profile.model,
                            "validator_valid": (
                                final_validation.valid
                                if final_validation is not None
                                else None
                            ),
                            "fallback_reason": fallback_reason,
                        },
                    )
                )
                state, turn_id = party_state_store.commit_turn(
                    commit_patch,
                    reason=f"party_start:{request_id}",
                    idempotency_key=idempotency_key,
                    request_id=request_id,
                    player_message=AUTO_START_HISTORY_MESSAGE,
                    narrative_response=text,
                    response_json=response,
                    expected_state_version=expected_state_version,
                    prompt_messages=prompt_messages,
                    metadata=turn_metadata,
                    consumed_world_clock_event_ids=(
                        list(world_clock_projection["event_ids"])
                        if world_clock_projection is not None and not fallback_noncanonical
                        else []
                    ),
                    party_turn=expected_party_turn,
                    audit_events=atomic_audits,
                    excluded_from_memory=fallback_noncanonical,
                )
                state_version = int(state["meta"]["state_version"])
            else:
                if start_patch:
                    state = party_state_store.apply_state_patch(start_patch, reason=f"party_start:{request_id}")
                state_version = party_state_store.current_version() or int(state.get("meta", {}).get("state_version") or 1)
                turn_id = party_state_store.record_turn(
                    idempotency_key,
                    request_id,
                    AUTO_START_HISTORY_MESSAGE,
                    text,
                    response,
                    state_version,
                    prompt_messages,
                    turn_metadata,
                    party_turn=int(state["meta"]["turn"]),
                )
            try:
                adjudicator.record_trace_event(
                    request_id=request_id,
                    phase_key="turn_commit",
                    alignment_key="turn_commit",
                    lane="main",
                    event_type="turn_commit",
                    status="completed",
                    payload={
                        "output": {
                            "turn_id": turn_id,
                            "state_version": state_version,
                            "party_turn": int(state["meta"]["turn"]),
                        }
                    },
                    party_turn=int(state["meta"]["turn"]),
                    turn_id=turn_id,
                )
            except Exception:  # noqa: BLE001 - revision-7 opening is already committed
                if not revision_seven:
                    raise
                logger.exception("party_start_commit_trace_failed request_id=%s", request_id)
            if not revision_seven:
                party_state_store.complete_turn_request(idempotency_key, response)
            if not revision_seven:
                party_state_store.audit(
                    "party_start_complete",
                    {
                        "request_id": request_id,
                        "turn_id": turn_id,
                        "model": model_profile.model,
                        "validator_valid": final_validation.valid if final_validation is not None else None,
                        "fallback_reason": fallback_reason,
                    },
                    request_id,
                )
        except PermissionError as exc:
            party_state_store.fail_turn_request(idempotency_key, f"{type(exc).__name__}: {exc}")
            trace_start_failure(exc)
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except httpx.TimeoutException as exc:
            party_state_store.fail_turn_request(idempotency_key, f"{type(exc).__name__}: {exc}")
            trace_start_failure(exc)
            raise HTTPException(
                status_code=504,
                detail="Narrative provider exceeded the party-start deadline",
            ) from exc
        except httpx.HTTPStatusError as exc:
            party_state_store.fail_turn_request(idempotency_key, f"{type(exc).__name__}: {exc}")
            trace_start_failure(exc)
            status = exc.response.status_code if exc.response is not None else "unknown"
            raise HTTPException(status_code=502, detail=f"Narrative provider HTTP {status}") from exc
        except ProviderRateLimitError as exc:
            party_state_store.fail_turn_request(idempotency_key, f"{type(exc).__name__}: {exc}")
            trace_start_failure(exc)
            party_state_store.audit("party_start_rate_limited", {"request_id": request_id, **exc.details}, request_id)
            raise HTTPException(status_code=429, detail=exc.public_detail()) from exc
        except httpx.RequestError as exc:
            party_state_store.fail_turn_request(idempotency_key, f"{type(exc).__name__}: {exc}")
            trace_start_failure(exc)
            raise HTTPException(status_code=502, detail="Narrative provider request failed") from exc
        except RuntimeError as exc:
            party_state_store.fail_turn_request(idempotency_key, f"{type(exc).__name__}: {exc}")
            trace_start_failure(exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ValueError as exc:
            party_state_store.fail_turn_request(idempotency_key, f"{type(exc).__name__}: {exc}")
            trace_start_failure(exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            party_state_store.fail_turn_request(idempotency_key, f"{type(exc).__name__}: {exc}")
            trace_start_failure(exc)
            raise

        return {
            **response,
            "party_id": party_id,
            "started": True,
            "already_started": False,
            "state_version": state_version,
            "message": response.get("choices", [{}])[0].get("message", {"role": "assistant", "content": ""}),
            "raw": response,
        }

    @app.post("/api/parties/{party_id}/messages")
    async def party_message(
        http_request: Request,
        party_id: str,
        request: RPPartyMessageRequest | PartyMessageRequest,
        authorization: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
    ) -> dict[str, Any]:
        if rebuilt_rp_request(http_request):
            if not isinstance(request, RPPartyMessageRequest):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "rebuilt RP messages require content, idempotency_key, "
                        "and expected_version"
                    ),
                )
            engine = require_rp_engine()
            owner_id = rp_owner_user_id(http_request)
            idempotency_key = request.idempotency_key
            try:
                party = engine.get_party(owner_user_id=owner_id, party_id=party_id)
                ensure_rebuilt_narrator_binding(party)
                try:
                    existing_request = engine.get_narration_request(
                        owner_user_id=owner_id,
                        party_id=party_id,
                        idempotency_key=idempotency_key,
                    )
                except LookupError:
                    existing_request = None
                request_id = (
                    existing_request.request_id
                    if existing_request is not None and x_request_id is None
                    else (x_request_id or idempotency_key)
                )
                if (
                    not settings.rp_narrator_enabled
                    and (
                        existing_request is None
                        or existing_request.status != "succeeded"
                    )
                ):
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "code": "rp_narrator_disabled",
                            "retryable": True,
                            "request_id": request_id,
                            "idempotency_key": idempotency_key,
                            "player_text": request.content,
                        },
                    )
                turn = await rp_narrator_service_for(
                    party, request_id
                ).narrate_turn(
                    owner_user_id=owner_id,
                    party_id=party_id,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    expected_version=request.expected_version,
                    player_text=request.content,
                )
                current_party = engine.get_party(
                    owner_user_id=owner_id, party_id=party_id
                )
            except RPPartyNotFound as exc:
                raise HTTPException(status_code=404, detail="party not found") from exc
            except (RPIdempotencyConflict, RPPartyVersionConflict) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except RPNarratorUnavailable as exc:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "code": "rp_narrator_unavailable",
                        "message": str(exc),
                        "retryable": True,
                        "request_id": request_id,
                        "idempotency_key": idempotency_key,
                        "player_text": request.content,
                    },
                ) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {
                "party_id": party_id,
                "state_version": current_party.current_version,
                "message": {"role": "assistant", "content": turn.narrator_text},
                "turn": rp_turn_payload(turn),
            }
        try:
            party = party_store.get_party(
                party_id,
                owner_user_id=owner_user_id(http_request),
                allow_retired_read=True,
            )
            ensure_party_playable(party)
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(http_request))
            party_settings = runtime_settings_for_party(party)
            gm_service = RPGMService(party_settings, party_state_store)
            request_id = x_request_id or request.idempotency_key or f"req_{uuid.uuid4().hex}"
            if gm_service.enabled:
                if request.story_memory_corrections:
                    raise ValueError(
                        "Revision-9 story-memory corrections must use the GM channel"
                    )
                channel = request.channel
                intent: dict[str, Any] | None = None
                if channel == "auto":
                    intent = await gm_service.classify(request.content, request_id=request_id)
                    if intent["label"] == "uncertain":
                        return {
                            "party_id": party_id,
                            "status": "route_required",
                            "state_version": party_state_store.current_version(),
                            "routing": {
                                "reason": intent.get("reason") or "uncertain",
                                "options": ["gm", "scene"],
                                "labels": {"gm": "Мастеру", "scene": "В сцену"},
                            },
                        }
                    channel = "gm" if intent["label"] == "correction" else "scene"
                if channel == "gm":
                    draft = await gm_service.draft(
                        request.content,
                        request_id=request_id,
                        target_hint=request.gm_target_slot,
                    )
                    return {
                        "party_id": party_id,
                        "status": "gm_draft",
                        "request_id": request_id,
                        "state_version": party_state_store.current_version(),
                        "gm_patch_draft": draft.model_dump(mode="json"),
                    }
            model_profile = party.model_profile or party_store.get_model_profile(party.model_profile_id)
            chat_request = party_chat_request(
                party_state_store,
                model_profile.model,
                request,
                party_settings,
                provider=model_profile.provider,
                narrator_settings=party.narrator_settings,
            )
            response = await Adjudicator(
                party_settings,
                party_state_store,
                relationship_model=relationship_model_for_party(party),
                scene_contract=scene_contract_for_party(party),
                world_clock_contract=world_clock_contract_for_party(
                    party,
                    effective_revision=party_settings.rp_contract_revision,
                ),
                rp_supervisor_contract=rp_supervisor_contract_for_party(party),
            ).handle_chat(
                chat_request,
                authorization,
                request.idempotency_key,
                request_id,
                allow_gateway_fallback=(
                    party_settings.rp_contract_revision >= 7
                ),
                story_memory_corrections=[
                    correction.model_dump(mode="json", exclude_none=True)
                    for correction in request.story_memory_corrections
                ],
            )
        except RequestAlreadyRunning as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "status": "running",
                    "request_id": exc.request_id,
                    "idempotency_key": exc.idempotency_key,
                    "message": "request is already running",
                },
            ) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ProviderRateLimitError as exc:
            raise HTTPException(status_code=429, detail=exc.public_detail()) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        message = response.get("choices", [{}])[0].get("message", {"role": "assistant", "content": ""})
        return {
            **response,
            "party_id": party_id,
            "state_version": party_state_store.current_version(),
            "message": message,
            "raw": response,
        }

    @app.post("/api/parties/{party_id}/gm-corrections/decide")
    async def decide_party_gm_correction(
        http_request: Request,
        party_id: str,
        decision: PartyGMCorrectionDecision,
        x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
    ) -> dict[str, Any]:
        try:
            party = party_store.get_party(party_id, owner_user_id=owner_user_id(http_request))
            ensure_party_playable(party)
            party_state_store = party_store.store_for_party(
                party_id,
                owner_user_id=owner_user_id(http_request),
            )
            party_settings = runtime_settings_for_party(party)
            gm_service = RPGMService(party_settings, party_state_store)
            if not gm_service.enabled:
                raise ValueError("GM corrections require an RP revision-9 party")

            async def ensure_absorption_job(
                correction_artifact: dict[str, Any],
                correction_request_id: str,
            ) -> None:
                if correction_artifact.get("target_kind") not in {"memory", "raw"}:
                    return
                party_state_store.enqueue_service_job(
                    "rp_story_memory",
                    correction_request_id,
                    2,
                    request_scoped=True,
                )
                adjudicator = Adjudicator(
                    party_settings,
                    party_state_store,
                    relationship_model=relationship_model_for_party(party),
                    scene_contract=scene_contract_for_party(party),
                    world_clock_contract=world_clock_contract_for_party(
                        party,
                        effective_revision=party_settings.rp_contract_revision,
                    ),
                    rp_supervisor_contract=rp_supervisor_contract_for_party(party),
                )
                if party_settings.post_turn_helpers_inline and party_settings.app_env == "test":
                    await adjudicator.drain_service_jobs(
                        authorization=None,
                        wait_for_retries=False,
                    )
                else:
                    adjudicator.schedule_service_jobs()

            if decision.decision == "reject":
                return {
                    "party_id": party_id,
                    "status": "rejected",
                    "state_version": party_state_store.current_version(),
                    "gm_patch_draft": decision.proposal.model_dump(mode="json"),
                }

            request_id = (
                x_request_id
                or decision.idempotency_key
                or f"gm-confirm:{uuid.uuid4().hex}"
            )
            idempotency_key = decision.idempotency_key or request_id
            request_status = party_state_store.begin_turn_request(idempotency_key, request_id)
            if not request_status.get("acquired"):
                if request_status.get("status") == "completed" and request_status.get("response"):
                    completed_response = request_status["response"]
                    completed_artifact = completed_response.get("gm_correction")
                    if isinstance(completed_artifact, dict):
                        await ensure_absorption_job(
                            completed_artifact,
                            str(completed_response.get("request_id") or request_id),
                        )
                    return completed_response
                raise RequestAlreadyRunning(
                    str(request_status.get("request_id") or request_id),
                    idempotency_key,
                )
            try:
                gm_service.validate_confirmed_proposal(decision.proposal)
            except Exception:
                party_state_store.fail_turn_request(
                    idempotency_key,
                    "GM correction validation failed",
                )
                raise

            party_turn = int(party_state_store.get_state().get("meta", {}).get("turn") or 0)
            artifact = gm_service.player_correction_artifact(
                decision.proposal,
                party_turn=party_turn,
            )
            after_text = decision.proposal.after or "утверждение отозвано"
            confirmation = (
                "Исправление подтверждено вне сцены.\n"
                f"Было: {decision.proposal.before}\n"
                f"Стало: {after_text}"
            )
            next_state_version = int(decision.proposal.base_state_version) + 1
            response = {
                "id": f"gm-correction-{decision.proposal.proposal_id}",
                "object": "rp.gm_correction",
                "created": int(time.time()),
                "model": "gateway-deterministic",
                "status": "confirmed",
                "party_id": party_id,
                "request_id": request_id,
                "state_version": next_state_version,
                "message": {"role": "assistant", "content": confirmation},
                "gm_correction": artifact,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": confirmation},
                        "finish_reason": "gateway_confirmation",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
            story_corrections = (
                [artifact["story_memory_correction"]]
                if isinstance(artifact.get("story_memory_correction"), dict)
                else []
            )
            metadata = {
                "schema_version": "rp-gateway.turn.v1",
                "turn_kind": "gm_correction",
                "scenario_type": party_settings.scenario_type,
                "rp_contract_version": party_settings.rp_contract_version,
                "rp_contract_revision": party_settings.rp_contract_revision,
                "worldpack_id": party_settings.campaign_id,
                "state_campaign_id": party_state_store.campaign_id,
                "generated_by": "human",
                "transport_status": "gateway_confirmation",
                "llm_calls": 0,
                "player_correction": artifact,
            }
            if story_corrections:
                metadata["story_memory_corrections"] = story_corrections
            rule_replacement = None
            if decision.proposal.target_kind == "absolute_rule":
                rule_replacement = {
                    "id": decision.proposal.target_id,
                    "before": decision.proposal.before,
                    "after": decision.proposal.after,
                    "forbidden_claims": decision.proposal.forbidden_claims,
                }
            try:
                party_state_store.commit_gm_correction(
                    reason=f"player_gm_correction:{decision.proposal.proposal_id}",
                    idempotency_key=idempotency_key,
                    request_id=request_id,
                    player_message=(
                        decision.proposal.after
                        or f"Отозвать: {decision.proposal.before}"
                    ),
                    response_json=response,
                    metadata=metadata,
                    expected_state_version=decision.proposal.base_state_version,
                    rule_replacement=rule_replacement,
                    audit_events=[
                        (
                            "player_gm_correction_confirmed",
                            {
                                "correction_id": decision.proposal.proposal_id,
                                "target_kind": decision.proposal.target_kind,
                                "target_slot": decision.proposal.target_slot,
                                "party_turn": party_turn,
                                "narrator_called": False,
                            },
                        )
                    ],
                )
            except Exception:
                party_state_store.fail_turn_request(idempotency_key, "GM correction commit failed")
                raise
            await ensure_absorption_job(artifact, request_id)
            return response
        except RequestAlreadyRunning as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "status": "running",
                    "request_id": exc.request_id,
                    "idempotency_key": exc.idempotency_key,
                    "message": "request is already running",
                },
            ) from exc
        except StateVersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/admin/autotests/models")
    def admin_autotest_models(request: Request) -> dict[str, Any]:
        require_admin(request)
        if settings.rp_rebuild_enabled:
            raise HTTPException(
                status_code=410,
                detail="legacy autotests are unavailable after rebuilt cutover",
            )
        party_store.settings = settings_with_provider_key(settings)
        profiles = party_store.list_autotest_model_profiles()
        return {"model_profiles": [profile.model_dump(mode="json") for profile in profiles]}

    @app.get("/api/admin/autotests")
    def admin_list_autotests(
        request: Request,
        source_party_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        admin = require_admin(request)
        if settings.rp_rebuild_enabled:
            raise HTTPException(
                status_code=410,
                detail="legacy autotests are unavailable after rebuilt cutover",
            )
        if source_party_id:
            try:
                party_store.get_party(
                    source_party_id, owner_user_id=admin.id if admin else None
                )
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "runs": party_store.list_autotest_runs(
                limit=limit, source_party_id=source_party_id
            )
        }

    @app.post("/api/admin/autotests")
    async def admin_create_autotest(
        http_request: Request,
        payload: AutoTestCreate,
    ) -> dict[str, Any]:
        admin = require_admin(http_request)
        if settings.rp_rebuild_enabled:
            raise HTTPException(
                status_code=410,
                detail="legacy autotests are unavailable after rebuilt cutover",
            )
        owner_id = admin.id if admin else None
        try:
            source_party = party_store.get_party(payload.source_party_id, owner_user_id=owner_id)
            supported_profiles = {profile.id: profile for profile in party_store.list_autotest_model_profiles()}
            player_profile = supported_profiles.get(payload.player_model_profile_id)
            if player_profile is None:
                raise ValueError("auto-player model must be an available OpenRouter or Local Gemma profile")
            source_store = party_store.store_for_party(source_party.id, owner_user_id=owner_id)
            if source_store.has_running_turn_request():
                raise ValueError("wait for the current party turn to finish before creating an auto-test branch")
            label = f"Автотест · {time.strftime('%Y-%m-%d %H:%M:%S')} · {payload.turn_count} ходов"
            checkpoint = source_store.create_memory_checkpoint(label)
            branch = party_store.create_party_branch(
                party_id=source_party.id,
                checkpoint_id=int(checkpoint["id"]),
                label=label,
                branch_type="autotest",
                owner_user_id=owner_id,
                rp_contract_revision=payload.rp_contract_revision,
            )
            source_store.audit(
                "autotest_branch_created",
                {"branch_id": branch["id"], "checkpoint_id": checkpoint["id"], "requested_turns": payload.turn_count},
            )
            run = party_store.create_autotest_run(
                owner_user_id=owner_id,
                source_party_id=source_party.id,
                branch_id=branch["id"],
                checkpoint_id=int(checkpoint["id"]),
                player_model_profile_id=player_profile.id,
                player_prompt=payload.player_prompt,
                requested_turns=payload.turn_count,
            )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        schedule_autotest(run["id"])
        checkpoint_summary = {key: value for key, value in checkpoint.items() if key != "state"}
        return {"run": run, "branch": branch, "checkpoint": checkpoint_summary}

    @app.post("/api/admin/autotests/{run_id}/stop")
    def admin_stop_autotest(request: Request, run_id: str) -> dict[str, Any]:
        require_admin(request)
        if settings.rp_rebuild_enabled:
            raise HTTPException(
                status_code=410,
                detail="legacy autotests are unavailable after rebuilt cutover",
            )
        try:
            run = party_store.request_autotest_stop(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"run": run}

    @app.post("/api/parties/{party_id}/checks")
    async def party_check(
        http_request: Request,
        party_id: str,
        request: PartyCheckRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        try:
            party = party_store.get_party(party_id, owner_user_id=owner_user_id(http_request))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if party.scenario_type != "rp":
            raise HTTPException(status_code=400, detail="The compatibility check endpoint is available only for RP parties")
        command = check_command(request)
        return await party_message(
            http_request,
            party_id,
            PartyMessageRequest(content=command),
            authorization=authorization,
            x_request_id=None,
        )

    @app.post("/api/parties/{party_id}/world/instruct")
    async def party_world_instruct(
        http_request: Request,
        party_id: str,
        request: WorldInstructionRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        try:
            party = party_store.get_party(party_id, owner_user_id=owner_user_id(http_request))
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(http_request))
            party_settings = runtime_settings_for_party(party)
            world = Adjudicator(party_settings, party_state_store).world
            draft = await world.draft_instruction(request.instruction, authorization, use_llm=request.use_llm)
            party_state_store.create_patch_proposal(draft.patch)
            scene_state_enabled = (
                party.scenario_type == "rp" and party_settings.rp_contract_revision == 7
            )
            candidate = party_state_store.preview_patch(
                draft.patch,
                scene_state_enabled=scene_state_enabled,
            )
            if request.confirm:
                state = party_state_store.apply_pending_patch(
                    draft.proposal_id,
                    reason="party_world_instruction_confirm",
                    scene_state_enabled=scene_state_enabled,
                )
                return {"party_id": party_id, "applied": True, "proposal": draft.model_dump(mode="json"), "state": state}
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except (PatchError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "party_id": party_id,
            "applied": False,
            "proposal": draft.model_dump(mode="json"),
            "candidate": candidate,
        }

    @app.get("/api/parties/{party_id}/world/proposals")
    def party_world_proposals(request: Request, party_id: str, limit: int = 10) -> dict[str, Any]:
        try:
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(request))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"party_id": party_id, "proposals": party_state_store.pending_patches(limit=limit)}

    @app.post("/api/parties/{party_id}/world/apply")
    def party_world_apply(http_request: Request, party_id: str, request: WorldApplyRequest) -> dict[str, Any]:
        try:
            party = party_store.get_party(
                party_id,
                owner_user_id=owner_user_id(http_request),
            )
            party_settings = runtime_settings_for_party(party)
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(http_request))
            patch = party_state_store.get_pending_patch(request.proposal_id)
            if not request.confirm:
                return {"party_id": party_id, "applied": False, "would_apply": patch.model_dump(mode="json")}
            state = party_state_store.apply_pending_patch(
                request.proposal_id,
                reason="party_world_instruction_apply",
                scene_state_enabled=(
                    party.scenario_type == "rp" and party_settings.rp_contract_revision == 7
                ),
            )
        except (PatchError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"party_id": party_id, "applied": True, "state": state}

    @app.post("/api/parties/{party_id}/world/discard")
    def party_world_discard(http_request: Request, party_id: str, request: WorldApplyRequest) -> dict[str, Any]:
        try:
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(http_request))
            proposal_id = party_state_store.discard_pending_patch(request.proposal_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"party_id": party_id, "discarded": True, "proposal_id": proposal_id}

    @app.post("/api/parties/{party_id}/rollback")
    async def party_rollback(party_id: str, request: Request) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if request.headers.get("content-length") not in {None, "0"}:
            body = await request.json()
        target_version = body.get("target_version")
        try:
            party = party_store.get_party(
                party_id,
                owner_user_id=owner_user_id(request),
            )
            party_settings = runtime_settings_for_party(party)
            party_state_store = party_store.store_for_party(party_id, owner_user_id=owner_user_id(request))
            state = party_state_store.rollback(
                int(target_version) if target_version is not None else None,
                scene_state_enabled=(
                    party.scenario_type == "rp" and party_settings.rp_contract_revision == 7
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"party_id": party_id, "rolled_back": True, "state": state}

    @app.post("/api/state/patch/preview")
    def preview_patch(request: Request, envelope: PatchEnvelope) -> dict[str, Any]:
        require_admin(request)
        try:
            candidate = store.preview_patch(
                envelope.patch,
                scene_state_enabled=(
                    settings.scenario_type == "rp" and settings.rp_contract_revision == 7
                ),
            )
        except (PatchError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"candidate": candidate, "would_apply": envelope.patch.model_dump(mode="json")}

    @app.post("/api/state/patch/apply")
    def apply_patch(request: Request, envelope: PatchEnvelope) -> dict[str, Any]:
        require_admin(request)
        try:
            if not envelope.confirm:
                return preview_patch(request, envelope)
            state = store.apply_state_patch(
                envelope.patch,
                reason="api_patch_apply",
                scene_state_enabled=(
                    settings.scenario_type == "rp" and settings.rp_contract_revision == 7
                ),
            )
        except (PatchError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"applied": True, "state": state}

    @app.get("/api/world/proposals")
    def world_proposals(request: Request, limit: int = 10) -> dict[str, Any]:
        require_admin(request)
        return {"campaign_id": settings.campaign_id, "proposals": store.pending_patches(limit=limit)}

    @app.post("/api/world/instruct")
    async def world_instruct(
        http_request: Request,
        request: WorldInstructionRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_admin(http_request)
        try:
            draft = await Adjudicator(settings_with_provider_key(settings), store).world.draft_instruction(
                request.instruction,
                authorization,
                use_llm=request.use_llm,
            )
            store.create_patch_proposal(draft.patch)
            scene_state_enabled = (
                settings.scenario_type == "rp" and settings.rp_contract_revision == 7
            )
            candidate = store.preview_patch(
                draft.patch,
                scene_state_enabled=scene_state_enabled,
            )
            if request.confirm:
                state = store.apply_pending_patch(
                    draft.proposal_id,
                    reason="world_instruction_api_confirm",
                    scene_state_enabled=scene_state_enabled,
                )
                return {"applied": True, "proposal": draft.model_dump(mode="json"), "state": state}
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except (PatchError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "applied": False,
            "proposal": draft.model_dump(mode="json"),
            "candidate": candidate,
            "apply_command": f"/world apply {draft.proposal_id}",
            "discard_command": f"/world discard {draft.proposal_id}",
        }

    @app.post("/api/world/apply")
    def world_apply(http_request: Request, request: WorldApplyRequest) -> dict[str, Any]:
        require_admin(http_request)
        try:
            patch = store.get_pending_patch(request.proposal_id)
            if not request.confirm:
                return {"applied": False, "would_apply": patch.model_dump(mode="json")}
            state = store.apply_pending_patch(
                request.proposal_id,
                reason="world_instruction_api_apply",
                scene_state_enabled=(
                    settings.scenario_type == "rp" and settings.rp_contract_revision == 7
                ),
            )
        except (PatchError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"applied": True, "state": state}

    @app.post("/api/turn/rollback")
    async def rollback(request: Request) -> dict[str, Any]:
        require_admin(request)
        body: dict[str, Any] = {}
        if request.headers.get("content-length") not in {None, "0"}:
            body = await request.json()
        target_version = body.get("target_version")
        try:
            state = store.rollback(
                int(target_version) if target_version is not None else None,
                scene_state_enabled=(
                    settings.scenario_type == "rp" and settings.rp_contract_revision == 7
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"rolled_back": True, "state": state}

    @app.post("/v1/chat/completions")
    async def chat_completions(
        request: ChatCompletionRequest,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
    ):
        if settings.rp_rebuild_enabled:
            raise HTTPException(
                status_code=410,
                detail="legacy RP chat completion route is unavailable after rebuilt cutover",
            )
        request_id = x_request_id or f"req_{uuid.uuid4().hex}"
        try:
            response = await Adjudicator(settings_with_provider_key(settings), store).handle_chat(request, authorization, idempotency_key, request_id)
        except RequestAlreadyRunning as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "status": "running",
                    "request_id": exc.request_id,
                    "idempotency_key": exc.idempotency_key,
                    "message": "request is already running",
                },
            ) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if request.stream:
            return StreamingResponse(stream_openai_response(response), media_type="text/event-stream")
        return JSONResponse(response)

    return app


def settings_for_party(
    settings: Settings,
    party: Any,
    *,
    effective_revision: int | None = None,
) -> Settings:
    if settings.app_env != "test" and (
        settings.scenario_type != "rp" or getattr(party, "scenario_type", None) != "rp"
    ):
        raise ValueError("RP gateway accepts only scenario_type=rp")
    model_profile = party.model_profile
    revision = (
        int(effective_revision)
        if effective_revision is not None
        else int(getattr(party, "rp_contract_revision", 0) or 0)
    )
    party_cache_id = (
        getattr(party, "id", "")
        or getattr(party, "state_campaign_id", "")
        or party.worldpack_id
    )
    prompt_values = {
        "scenario_type": getattr(party, "scenario_type", "rp"),
        "rp_contract_version": getattr(party, "rp_contract_version", "rp-core.v1"),
        "rp_contract_revision": revision,
        "campaign_id": party.worldpack_id,
        "world_system_prompt": worldpack_prompt_text(party, "gm_system", effective_revision=revision),
        "world_authors_note": worldpack_prompt_text(party, "authors_note", effective_revision=revision),
        "prompt_cache_session_id": f"rp-party:{party_cache_id}",
    }
    if model_profile is None:
        return replace(settings, **prompt_values)
    configured = settings_for_model_profile(settings, model_profile, f"rp-party:{party_cache_id}")
    return replace(
        configured,
        **prompt_values,
        model_attempt_timeout_seconds=settings.model_attempt_timeout_seconds,
    )


def settings_for_model_profile(settings: Settings, model_profile: Any, cache_session_id: str) -> Settings:
    provider = normalize_provider(model_profile.provider)
    if provider not in {"local", "gemini", "openrouter"}:
        raise ValueError(f"model profile provider is retired or unsupported: {provider}")
    fallback_models = settings.openrouter_fallback_models if provider == "openrouter" else ()
    return replace(
        settings,
        llm_provider=provider,
        llm_api_base=model_profile.base_url,
        narrative_model=model_profile.model,
        intent_model=model_profile.model,
        validator_model=model_profile.model,
        llm_fallback_models=fallback_models,
        llm_disabled_models=(),
        model_attempt_timeout_seconds=(
            settings.local_llm_timeout_seconds if provider == "local" else settings.model_attempt_timeout_seconds
        ),
        prompt_cache_session_id=cache_session_id,
        party_context_limit_tokens=min(
            model_context_limit_tokens(model_profile) or settings.party_context_max_tokens,
            settings.party_context_max_tokens,
        ),
    )


def worldpack_prompt_text(
    party: Any,
    file_key: str,
    *,
    effective_revision: int | None = None,
) -> str:
    revision = (
        int(effective_revision)
        if effective_revision is not None
        else int(getattr(party, "rp_contract_revision", 0) or 0)
    )
    if revision >= 11:
        materialization = PartyStore.validate_worldpack_materialization(
            getattr(party, "worldpack_materialization", None)
        )
        materialized_key = {
            "gm_system": "world_system_prompt",
            "authors_note": "world_authors_note",
            "opening_scene": "opening_prompt",
        }.get(file_key)
        if materialized_key is None:
            raise ValueError(f"revision-11 prompt has no materialized field for {file_key}")
        return str(materialization[materialized_key])
    world = getattr(party, "worldpack", None)
    if world is None or not isinstance(world.manifest, dict):
        return ""
    files = world.manifest.get("files")
    relative_path = files.get(file_key) if isinstance(files, dict) else None
    if not isinstance(relative_path, str) or not relative_path.strip():
        return ""
    root = Path(world.manifest_path).resolve().parent
    target = (root / relative_path).resolve()
    if target != root and root not in target.parents:
        return ""
    try:
        return target.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def relationship_model_for_party(party: Any) -> dict[str, Any] | None:
    if getattr(party, "scenario_type", None) != "rp":
        return None
    world = getattr(party, "worldpack", None)
    if world is None or not isinstance(world.manifest, dict):
        return None
    declaration = world.manifest.get("relationships")
    if declaration is None:
        return None
    if not isinstance(declaration, dict) or declaration.get("schema_version") != "rp-relationships.v2":
        raise ValueError("invalid WorldPack relationship declaration")
    relative_path = declaration.get("model")
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("WorldPack relationship model path is missing")
    root = Path(world.manifest_path).resolve().parent
    target = (root / relative_path).resolve()
    if target != root and root not in target.parents:
        raise ValueError("WorldPack relationship model path escapes the pack")
    try:
        model = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("cannot load WorldPack relationship model") from exc
    if not isinstance(model, dict) or model.get("schema_version") != "rp-relationships.v2":
        raise ValueError("invalid WorldPack relationship model")
    return model


def scene_contract_for_party(party: Any) -> dict[str, Any] | None:
    if (
        getattr(party, "scenario_type", None) != "rp"
        or int(getattr(party, "rp_contract_revision", 0) or 0) < 7
    ):
        return None
    world = getattr(party, "worldpack", None)
    manifest = getattr(world, "manifest", None)
    if not isinstance(manifest, dict):
        return None
    rp_contract = manifest.get("rp_contract")
    if not isinstance(rp_contract, dict):
        return None
    stable = rp_contract.get("stable_affiliations")
    if stable is None:
        return None
    if not isinstance(stable, dict) or len(stable) > 64:
        raise ValueError("invalid WorldPack rp_contract.stable_affiliations")
    normalized = {
        character_id: affiliation
        for character_id, affiliation in stable.items()
        if isinstance(character_id, str)
        and isinstance(affiliation, str)
        and 0 < len(character_id) <= 128
        and 0 < len(affiliation) <= 128
    }
    if len(normalized) != len(stable):
        raise ValueError("invalid WorldPack rp_contract.stable_affiliations")
    return {"stable_affiliations": normalized}


def world_clock_contract_for_party(
    party: Any,
    *,
    effective_revision: int | None = None,
) -> dict[str, Any] | None:
    revision = (
        int(effective_revision)
        if effective_revision is not None
        else int(getattr(party, "rp_contract_revision", 0) or 0)
    )
    if (
        getattr(party, "scenario_type", None) != "rp"
        or revision < 10
    ):
        return None
    world = getattr(party, "worldpack", None)
    manifest = getattr(world, "manifest", None)
    manifest_path = getattr(world, "manifest_path", None)
    if not isinstance(manifest, dict) or not manifest_path:
        return None
    return load_world_clock_contract(manifest_path, manifest)


def rp_supervisor_contract_for_party(party: Any) -> dict[str, Any] | None:
    if getattr(party, "scenario_type", None) != "rp":
        return None
    world = getattr(party, "worldpack", None)
    manifest = getattr(world, "manifest", None)
    manifest_path = getattr(world, "manifest_path", None)
    if not isinstance(manifest, dict) or not manifest_path:
        return None
    return load_rp_supervisor_contract(manifest_path, manifest)


def party_start_prompt(party_store: PartyStore, party: Any) -> str:
    world = party.worldpack or party_store.get_worldpack(party.worldpack_id)
    character = party.player_character or party_store.get_player_character(party.player_character_id)
    manifest = world.manifest if isinstance(world.manifest, dict) else {}
    revision = int(getattr(party, "rp_contract_revision", 0) or 0)
    materialization = (
        PartyStore.validate_worldpack_materialization(
            getattr(party, "worldpack_materialization", None)
        )
        if revision >= 11
        else None
    )
    opening_scene = (
        str(materialization["opening_prompt"])
        if materialization is not None
        else party_store.opening_scene_text(world)
    )
    premise = str(manifest.get("premise") or world.premise or manifest.get("prompt") or "").strip()
    player_role = (
        str(materialization["player_role"])
        if materialization is not None
        else str(manifest.get("player_role") or "").strip()
    )
    rendered_player_role = (
        player_role
        if materialization is not None
        else character.description or player_role or "active player character"
    )
    opening_block = opening_scene or (
        "No dedicated opening-scene file is available. Synthesize the first scene from the current state, "
        "world premise, and player character. End with a concrete player-facing choice."
    )
    mode_instruction = (
        "Write the first GM message for a roleplaying party in Russian. Establish a playable situation without "
        "rolling a check or resolving a player choice, and end with a concrete opening for player action."
    )
    blocks = [
            "START_PARTY_OPENING_SCENE",
            "This is an internal Light GUI auto-start request, not a player action.",
            f"Selected scenario type: {party.scenario_type}",
            mode_instruction,
            "Use second person where appropriate and preserve player agency.",
            "Do not expose service instructions, JSON, model policy, or the AUTO_START marker.",
            f"World title: {world.title}",
            f"World premise: {premise or 'use the current authoritative state'}",
            f"Player character: {character.name}",
            f"Player role: {rendered_player_role}",
            f"Opening scene source:\n{opening_block}",
        ]
    if materialization is not None and character.description:
        blocks.insert(-1, f"Player character description: {character.description}")
    return "\n\n".join(blocks)


def party_start_outcome(
    party_id: str,
    scenario_type: str = "rp",
    rp_contract_revision: int = 0,
) -> Outcome:
    result = "narrative_continuation" if rp_contract_revision >= 1 else "success"
    return Outcome(
        check_id=f"party_start:{party_id}",
        action_type=(
            "narrative"
            if scenario_type == "rp" and rp_contract_revision >= 1
            else "feasibility"
        ),
        actor="system",
        target="opening_scene",
        result=result,
        roll=0,
        difficulty=0,
        modifiers={},
        final_score=0,
        blocked_reasons=[],
        consequences=["Initial scene is introduced; no player decision has been resolved yet."],
        forbidden_reinterpretations=[
            "Do not treat the start request as a player action.",
            "Do not change state or grant resources through the opening narration.",
        ],
        authoritative_block=(
            "AUTHORITATIVE_OUTCOME: This is the start of a new party. Present the opening scene only. "
            "No mechanical check was rolled, and no player action has been resolved."
        ),
    )


def party_start_state_patch(
    state: dict[str, Any],
    party_id: str,
    worldpack_id: str | None = None,
    scenario_type: str = "rp",
) -> StatePatch | None:
    return None


def party_start_narrative_state(state: dict[str, Any], patch: StatePatch | None) -> dict[str, Any]:
    if not patch:
        return state
    cloned = copy.deepcopy(state)
    meta = cloned.setdefault("meta", {})
    meta["turn"] = max(int(meta.get("turn", 0) or 0), patch.turn)
    resources = cloned.setdefault("player", {}).setdefault("resources", {})
    for operation in patch.patch:
        prefix = "/player/resources/"
        if operation.path.startswith(prefix):
            resources[operation.path.removeprefix(prefix)] = operation.value
    return cloned


def lore_card_draft_payload(source_units: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "model": LORE_CARD_DRAFT_MODEL,
        "temperature": 0.1,
        "max_tokens": LORE_CARD_DRAFT_OUTPUT_MAX_TOKENS,
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "lore_card_draft",
                "strict": True,
                "schema": PartyLoreCardDraft.model_json_schema(),
            },
        },
        "messages": [
            {
                "role": "system",
                "content": (
                    "Создай одну компактную Lore Card только из приведённых завершённых игровых ходов. "
                    "Сохрани подтверждённые детали, полезные для будущего нарратива; не добавляй новый лор, "
                    "не превращай намерение игрока в свершившийся факт и не упоминай служебный процесс. "
                    "Верни JSON ровно с полями title, content, keywords. keywords — непустые точные триггеры; "
                    "для названного персонажа добавь встречающиеся или очевидные русские падежные формы имени."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({"source_turns": source_units}, ensure_ascii=False, separators=(",", ":")),
            },
        ],
    }


def party_chat_request(
    store: StateStore,
    model: str,
    request: PartyMessageRequest,
    settings: Settings,
    provider: str | None = None,
    narrator_settings: dict[str, Any] | None = None,
) -> ChatCompletionRequest:
    revision_seven_rp = settings.scenario_type == "rp" and settings.rp_contract_revision >= 7
    revision_eight_rp = settings.scenario_type == "rp" and settings.rp_contract_revision >= 8
    story_memory = store.effective_rp_story_memory() if revision_seven_rp else None
    memory = story_memory if revision_seven_rp else store.latest_memory_coverage()
    covered_through = (
        story_memory_safe_coverage(story_memory)
        if revision_eight_rp
        else int(memory["to_turn_id"])
        if memory
        else 0
    )
    all_turns = store.turns_for_memory(
        include_noncanonical_fallback=revision_seven_rp
    )
    if revision_seven_rp and not revision_eight_rp:
        all_turns = unresolved_noncanonical_fallback_turns(store.get_state(), all_turns)
    if revision_eight_rp:
        raw_turns = raw_history_window(
            all_turns,
            safe_coverage=covered_through,
            window_turns=settings.effective_rp_raw_history_window_turns,
        )
        overflow_turns: list[dict[str, Any]] = []
    else:
        turns = [turn for turn in all_turns if int(turn["id"]) > covered_through]
    if revision_seven_rp and not revision_eight_rp:
        turns = list(
            {
                int(turn["id"]): turn
                for turn in [
                    *[item for item in all_turns if item.get("noncanonical_safe_fallback")],
                    *turns,
                ]
            }.values()
        )
        turns.sort(key=lambda item: int(item["id"]))
        overflow_turns, raw_turns = [], turns
    elif not revision_eight_rp:
        current_message_tokens = estimate_tokens(request.content)
        history_budget = max(settings.effective_party_history_token_budget - current_message_tokens, 0)
        overflow_turns, raw_turns = split_turns_by_token_budget(turns, history_budget)
    messages: list[ChatMessage] = []
    if revision_seven_rp and not revision_eight_rp:
        messages.append(
            ChatMessage(
                role="system",
                content=scene_state_boundary_block(store.get_state()),
            )
        )
    lore_query = (
        recent_rp_scan_text(
            store.turns_for_memory(include_noncanonical_fallback=False),
            request.content,
        )
        if revision_eight_rp
        else request.content
    )
    lore_block = party_lore_cards_block(
        store.lore_cards_for_prompt(
            lore_query,
            limit=settings.party_lore_card_prompt_limit,
            max_chars=(
                min(settings.party_lore_card_prompt_max_chars, 4_000)
                if revision_eight_rp
                else settings.party_lore_card_prompt_max_chars
            ),
            title_keywords_only=revision_eight_rp,
            whole_match=revision_eight_rp,
        ),
        max_chars=4_000 if revision_eight_rp else None,
    )
    if lore_block:
        messages.append(ChatMessage(role="system", content=lore_block))
    if revision_eight_rp and settings.rp_contract_revision >= 9:
        corrections_block = RPGMService(settings, store).overlay_block()
        if corrections_block:
            messages.append(ChatMessage(role="system", content=corrections_block))
    fallback_block = uncompacted_archive_fallback_block(
        overflow_turns,
        settings.party_memory_fallback_max_chars,
    )
    if fallback_block:
        messages.append(ChatMessage(role="system", content=fallback_block))
    for turn in raw_turns:
        rendered = (
            rp_turn_messages(turn)
            if revision_eight_rp
            else [
                ("user", str(turn["player_message"])),
                ("assistant", str(turn["narrative_response"])),
            ]
        )
        messages.extend(ChatMessage(role=role, content=content) for role, content in rendered)
    if settings.party_memory_retrieval_enabled and not revision_eight_rp:
        retrieved = store.search_archived_turns(
            request.content,
            through_turn_id=covered_through,
            limit=settings.party_memory_retrieval_limit,
        )
        retrieval_block = archived_memory_retrieval_block(retrieved, settings.party_memory_retrieval_max_chars)
        if retrieval_block:
            messages.append(ChatMessage(role="system", content=retrieval_block))
    messages.append(ChatMessage(role="user", content=request.content))
    chat_request = ChatCompletionRequest(
        model=model,
        messages=messages,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        stream=False,
    )
    chat_request._raw_transcript_chars = sum(
        len(str(turn.get("player_message") or "")) + len(str(turn.get("narrative_response") or ""))
        for turn in all_turns
    )
    chat_request._latest_player_action = request.content
    if revision_seven_rp:
        chat_request._rp_story_memory_snapshot_id = int(story_memory["id"]) if story_memory else None
        chat_request._rp_story_memory_covered_through_turn_id = covered_through
    if revision_eight_rp:
        chat_request._rp_raw_history_turn_ids = [int(turn["id"]) for turn in raw_turns]
        chat_request._rp_raw_history_removable_units = removable_covered_history_units(
            raw_turns,
            safe_coverage=covered_through,
        )
    if provider and narrator_settings:
        apply_party_narrator_settings(chat_request, provider, model, narrator_settings)
    return chat_request


def apply_party_narrator_settings(
    request: ChatCompletionRequest,
    provider: str,
    model: str,
    narrator_settings: dict[str, Any] | None,
) -> ChatCompletionRequest:
    settings = validate_narrator_settings(provider, model, narrator_settings or {})
    if not settings:
        return request
    if request.temperature is None and "temperature" in settings:
        request.temperature = float(settings["temperature"])
    if request.max_tokens is None and "max_tokens" in settings:
        request.max_tokens = int(settings["max_tokens"])
    if "top_p" in settings:
        request.top_p = float(settings["top_p"])
    effort = settings.get("reasoning_effort")
    if effort == "none":
        request.reasoning = {"enabled": False}
    elif effort:
        request.reasoning = {"effort": effort, "exclude": True}
    request._narrator_settings_model = model
    return request


async def generate_character_edit(
    settings: Settings,
    store: StateStore,
    request: PartyCharacterStateEditRequest,
    authorization: str | None,
    request_id: str,
) -> PartyCharacterStateEditRequest:
    state = store.get_state()
    runtime = service_model_settings(settings)
    if runtime.llm_api_base.startswith("mock://"):
        return mock_generated_character_edit(settings, state, request)

    world = WorldInstructor(settings, store)
    payload: dict[str, Any] = {
        "model": runtime.intent_model,
        "temperature": 0.35,
        "max_tokens": 1200,
        "stream": False,
        "messages": [
            {"role": "system", "content": character_generation_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "input_fields": request.model_dump(mode="json", exclude_none=True),
                        "state_excerpt": character_generation_state_excerpt(state, request),
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    attempts = world.model_attempts(runtime.intent_model, runtime)
    service_client = ServiceModelClient(runtime)
    last_timeout: httpx.TimeoutException | None = None
    last_status: httpx.HTTPStatusError | None = None
    last_parse_error: ValueError | None = None
    for index, model in enumerate(attempts):
        payload["model"] = model
        started = time.perf_counter()
        logger.info(
            "character_llm_attempt_start request_id=%s model=%s attempt=%s/%s timeout_seconds=%s",
            request_id,
            model,
            index + 1,
            len(attempts),
            runtime.model_attempt_timeout_seconds,
        )
        try:
            completion = await service_client.complete(
                role="character_generation",
                provider=runtime.llm_provider,
                model=model,
                party_id=store.campaign_id,
                turn_id=int(state.get("meta", {}).get("turn", 0)) + 1,
                prompt=service_prompt_text(payload),
                payload=payload,
            )
        except httpx.TimeoutException as exc:
            last_timeout = exc
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.warning(
                "character_llm_attempt_timeout request_id=%s model=%s attempt=%s/%s elapsed_ms=%s fallback=%s",
                request_id,
                model,
                index + 1,
                len(attempts),
                elapsed_ms,
                index < len(attempts) - 1,
            )
            if index < len(attempts) - 1:
                continue
            raise
        except httpx.HTTPStatusError as exc:
            last_status = exc
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            status_code = exc.response.status_code
            logger.warning(
                "character_llm_attempt_http_error request_id=%s model=%s status=%s elapsed_ms=%s fallback=%s",
                request_id,
                model,
                status_code,
                elapsed_ms,
                index < len(attempts) - 1,
            )
            if index < len(attempts) - 1 and status_code in {400, 404, 408, 500, 502, 503, 504}:
                continue
            raise
        except RuntimeError:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.warning(
                "character_llm_attempt_rate_limited request_id=%s model=%s elapsed_ms=%s",
                request_id,
                model,
                elapsed_ms,
            )
            raise
        try:
            data = world.extract_json(response_text(completion.data))
        except ValueError as exc:
            last_parse_error = exc
            logger.warning(
                "character_llm_attempt_parse_error request_id=%s model=%s attempt=%s/%s fallback=%s error=%s",
                request_id,
                model,
                index + 1,
                len(attempts),
                index < len(attempts) - 1,
                exc,
            )
            if index < len(attempts) - 1:
                continue
            raise RuntimeError("LLM did not return character JSON") from exc
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "character_llm_attempt_success request_id=%s model=%s elapsed_ms=%s fallback_used=%s",
            request_id,
            model,
            elapsed_ms,
            index > 0 or model != runtime.intent_model,
        )
        return coerce_generated_character_edit(data, request, state)
    if last_status:
        raise last_status
    if last_timeout:
        raise last_timeout
    if last_parse_error:
        raise RuntimeError("LLM did not return character JSON") from last_parse_error
    raise RuntimeError(f"No model attempts configured for provider {runtime.llm_provider}")


def character_generation_prompt() -> str:
    return (
        "Generate exactly one structured tabletop-RP character edit for RP Gateway. "
        "Return only a JSON object, no markdown and no explanations. "
        "Allowed keys: target, character_id, name, status, location, current_goal, attitude_to_player, "
        "loyalty, trust, fear, knowledge, obligations, hard_constraints, secrets. "
        "knowledge, obligations, hard_constraints, and secrets may be arrays of short strings. "
        "trust must be an integer from -10 to 10; fear must be an integer from 0 to 10. "
        "Preserve every non-empty user-provided field exactly; only fill missing fields. "
        "Use the user's language for generated prose. Do not invent resolved plot outcomes, only character traits, "
        "social posture, knowledge, duties, and constraints that fit the current state."
    )


def character_generation_state_excerpt(state: dict[str, Any], request: PartyCharacterStateEditRequest) -> dict[str, Any]:
    character_id = stable_character_id(request.character_id or request.name or "")
    characters = state.get("characters", {}) if isinstance(state.get("characters"), dict) else {}
    return {
        "turn": state.get("meta", {}).get("turn"),
        "player": state.get("player", {}),
        "target_character_id": character_id,
        "existing_character": characters.get(character_id),
        "nearby_characters": [
            {"id": key, "name": value.get("name") if isinstance(value, dict) else key}
            for key, value in list(characters.items())[:20]
        ],
        "world_constraints": state.get("world_constraints", [])[:20],
        "timeline_tail": state.get("timeline", [])[-8:],
    }


def coerce_generated_character_edit(
    data: dict[str, Any],
    source: PartyCharacterStateEditRequest,
    state: dict[str, Any],
) -> PartyCharacterStateEditRequest:
    payload = data.get("character") if isinstance(data.get("character"), dict) else data
    normalized: dict[str, Any] = {
        "target": source.target,
        "character_id": normalize_optional_string(payload.get("character_id")),
        "name": normalize_optional_string(payload.get("name")),
        "status": normalize_optional_string(payload.get("status")),
        "location": normalize_optional_string(payload.get("location")),
        "current_goal": normalize_optional_string(payload.get("current_goal")),
        "attitude_to_player": normalize_optional_string(payload.get("attitude_to_player")),
        "loyalty": normalize_optional_string(payload.get("loyalty")),
        "trust": clamp_optional_int(payload.get("trust"), -10, 10),
        "fear": clamp_optional_int(payload.get("fear"), 0, 10),
        "knowledge": normalize_line_field(payload.get("knowledge")),
        "obligations": normalize_line_field(payload.get("obligations")),
        "hard_constraints": normalize_line_field(payload.get("hard_constraints")),
        "secrets": normalize_line_field(payload.get("secrets")),
        "confirm": True,
    }
    for field in [
        "character_id",
        "name",
        "status",
        "location",
        "current_goal",
        "attitude_to_player",
        "loyalty",
        "trust",
        "fear",
        "knowledge",
        "obligations",
        "hard_constraints",
        "secrets",
    ]:
        value = getattr(source, field)
        if value is not None and (not isinstance(value, str) or value.strip()):
            normalized[field] = value
    if source.target == "player":
        normalized["character_id"] = None
    elif not normalized.get("character_id") and normalized.get("name"):
        normalized["character_id"] = stable_character_id(str(normalized["name"]))
    if not normalized.get("location") and source.target == "player":
        normalized["location"] = normalize_optional_string(state.get("player", {}).get("location"))
    return PartyCharacterStateEditRequest.model_validate(normalized)


def mock_generated_character_edit(
    settings: Settings,
    state: dict[str, Any],
    source: PartyCharacterStateEditRequest,
) -> PartyCharacterStateEditRequest:
    mode = settings.llm_api_base.removeprefix("mock://")
    if mode == "timeout":
        raise httpx.TimeoutException("mock timeout")
    if mode == "http-503":
        request = httpx.Request("POST", "https://mock.provider.local/chat/completions")
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("mock provider unavailable", request=request, response=response)
    if mode == "rate-limit":
        raise RuntimeError(f"{settings.llm_provider} API returned 429 rate limit")
    name = source.name or source.character_id or ("Игрок" if source.target == "player" else "NPC")
    default_location = "unknown" if source.target == "npc" else state.get("player", {}).get("location") or "unknown"
    generated = {
        "target": source.target,
        "character_id": source.character_id or (stable_character_id(name) if source.target == "npc" else None),
        "name": name,
        "status": source.status or ("active" if source.target == "player" else "alive"),
        "location": source.location or default_location,
        "current_goal": source.current_goal or f"держать свою роль в сцене как {name}",
        "attitude_to_player": source.attitude_to_player or ("самоконтроль" if source.target == "player" else "нейтральное любопытство"),
        "loyalty": source.loyalty or "локальная рутина",
        "trust": source.trust if source.trust is not None else 0,
        "fear": source.fear if source.fear is not None else 0,
        "knowledge": source.knowledge or f"{name} замечает детали текущей сцены.",
        "obligations": source.obligations or f"{name} не нарушает явные ограничения мира.",
        "hard_constraints": source.hard_constraints or "",
        "secrets": source.secrets or "",
        "confirm": True,
    }
    return PartyCharacterStateEditRequest.model_validate(generated)


def normalize_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_line_field(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        lines = [str(item).strip(" -•\t") for item in value if str(item).strip(" -•\t")]
        return "\n".join(lines) or None
    return normalize_optional_string(value)


def clamp_optional_int(value: Any, lower: int, upper: int) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(lower, min(upper, number))


def character_state_patch(state: dict[str, Any], request: PartyCharacterStateEditRequest) -> StatePatch:
    turn = int(state.get("meta", {}).get("turn", 0)) + 1
    proposal_id = f"character-{uuid.uuid4().hex[:12]}"
    operations: list[PatchOperation] = []
    if request.target == "player":
        for field, value in [
            ("name", request.name),
            ("status", request.status),
            ("location", request.location),
            ("description", request.current_goal),
        ]:
            add_value_patch(operations, state, f"/player/{field}", value, turn, f"Updates player {field} from Light GUI character editor.")
        if request.knowledge is not None:
            add_value_patch(
                operations,
                state,
                "/player/known_world_facts",
                split_editor_lines(request.knowledge),
                turn,
                "Updates player known facts from Light GUI character editor.",
            )
        if request.obligations is not None:
            add_value_patch(
                operations,
                state,
                "/player/constraints",
                split_editor_lines(request.obligations),
                turn,
                "Updates player constraints from Light GUI character editor.",
            )
        participant = "player"
    else:
        character_id = stable_character_id(request.character_id or request.name or "")
        if not character_id:
            raise ValueError("character_id or name is required for NPC edits")
        characters = state.get("characters", {}) if isinstance(state.get("characters"), dict) else {}
        participant = character_id
        base_path = f"/characters/{pointer_escape(character_id)}"
        if character_id not in characters:
            operations.append(
                PatchOperation(
                    op="add",
                    path=base_path,
                    value={
                        "name": request.name or character_id,
                        "status": request.status or "alive",
                        "location": request.location or "unknown",
                        "attitude_to_player": request.attitude_to_player or "",
                        "trust": request.trust if request.trust is not None else 0,
                        "fear": request.fear if request.fear is not None else 0,
                        "loyalty": request.loyalty or "unknown",
                        "current_goal": request.current_goal or "",
                        "knowledge": split_editor_lines(request.knowledge),
                        "secrets": split_editor_lines(request.secrets),
                        "obligations": split_editor_lines(request.obligations),
                        "hard_constraints": split_editor_lines(request.hard_constraints),
                        "last_confirmed_update": turn,
                    },
                    reason="Creates NPC from Light GUI character editor.",
                    turn=turn,
                )
            )
        else:
            for field, value in [
                ("name", request.name),
                ("status", request.status),
                ("location", request.location),
                ("current_goal", request.current_goal),
                ("attitude_to_player", request.attitude_to_player),
                ("loyalty", request.loyalty),
                ("trust", request.trust),
                ("fear", request.fear),
            ]:
                add_value_patch(operations, state, f"{base_path}/{field}", value, turn, f"Updates NPC {field} from Light GUI character editor.")
            for field, value in [
                ("knowledge", split_editor_lines(request.knowledge)),
                ("obligations", split_editor_lines(request.obligations)),
                ("hard_constraints", split_editor_lines(request.hard_constraints)),
                ("secrets", split_editor_lines(request.secrets)),
            ]:
                if value:
                    add_value_patch(operations, state, f"{base_path}/{field}", value, turn, f"Updates NPC {field} from Light GUI character editor.")
            add_value_patch(operations, state, f"{base_path}/last_confirmed_update", turn, turn, "Marks NPC update turn.")
    if not operations:
        raise ValueError("no character fields to update")
    operations.append(
        PatchOperation(
            op="add",
            path="/timeline/-",
            value={"turn": turn, "event": f"Character editor updated {participant}.", "confirmed": True, "participants": [participant]},
            reason="Records Light GUI character edit.",
            turn=turn,
        )
    )
    return StatePatch(turn=turn, check_id=proposal_id, source="character-editor", patch=operations)


def add_value_patch(
    operations: list[PatchOperation],
    state: dict[str, Any],
    path: str,
    value: Any,
    turn: int,
    reason: str,
) -> None:
    if value is None:
        return
    if isinstance(value, str) and not value.strip():
        return
    operations.append(
        PatchOperation(
            op="replace" if path_exists(state, path) else "add",
            path=path,
            value=value.strip() if isinstance(value, str) else value,
            reason=reason,
            turn=turn,
        )
    )


def path_exists(document: Any, path: str) -> bool:
    current = document
    for part in [part.replace("~1", "/").replace("~0", "~") for part in path.strip("/").split("/")]:
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        return False
    return True


def split_editor_lines(value: str | None) -> list[str]:
    if value is None:
        return []
    return [line.strip(" -•\t") for line in value.splitlines() if line.strip(" -•\t")]


def stable_character_id(value: str) -> str:
    clean = re.sub(r"[^\w\-]+", "-", value.strip().lower(), flags=re.UNICODE).strip("-")
    return clean[:80]


def pointer_escape(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def check_command(request: PartyCheckRequest) -> str:
    parts = [f"/check {request.check_type}", f"skill={request.skill}", f"difficulty={request.difficulty}"]
    if request.target:
        parts.append(f"target={quote_token(request.target)}")
    if request.goal:
        parts.append(f"goal={quote_token(request.goal)}")
    return " ".join(parts)


def quote_token(value: str) -> str:
    if re_safe_token(value):
        return value
    return json.dumps(value, ensure_ascii=False)


def re_safe_token(value: str) -> bool:
    return bool(value) and all(char.isalnum() or char in {"_", "-", "."} for char in value)


async def stream_openai_response(response: dict[str, Any]):
    content = str(response.get("choices", [{}])[0].get("message", {}).get("content", ""))
    chunk = {
        "id": response.get("id"),
        "object": "chat.completion.chunk",
        "created": response.get("created"),
        "model": response.get("model"),
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    for part in split_stream(content):
        chunk["choices"] = [{"index": 0, "delta": {"content": part}, "finish_reason": None}]
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    chunk["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


def split_stream(content: str, size: int = 80) -> list[str]:
    if not content:
        return [""]
    return [content[index : index + size] for index in range(0, len(content), size)]
