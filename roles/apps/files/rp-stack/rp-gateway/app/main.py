"""FastAPI entrypoint for the single Decision 043 RP runtime."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.core.config import Settings, get_settings
from app.models.schemas import (
    HealthResponse,
    LoginRequest,
    ModelProfileSummary,
    PartyLoreCardCreate,
    PartyLoreCardDraft,
    PartyLoreCardDraftRequest,
    ProviderApiKeyCreate,
    ProviderApiKeyUpdate,
    RPAdministratorProposalDecision,
    RPPartyCreate,
    RPPartyMessageRequest,
    RPPartyStartRequest,
    RPPlayerCorrectionDecision,
    RPPlayerCorrectionDraftRequest,
    RPScenarioFreeCreate,
    UserCreate,
    UserDeleteRequest,
    UserPasswordUpdate,
    UserStatusUpdate,
)
from app.rp.content import (
    SUPPORTED_WORLD_ID,
    ScenarioPresetNotFound,
    WorldScenarioLoader,
    WorldSourceError,
)
from app.rp.mechanics import (
    RPAdministratorHandler,
    RPAtomicServiceHandler,
    player_correction_catalog,
    player_correction_catalog_hash,
)
from app.rp.narrator import RPNarratorService, RPNarratorUnavailable
from app.rp.provider import (
    RPAdministratorProvider,
    RPAtomicServiceProvider,
    RPNarratorProvider,
)
from app.rp.runner import RPRunner
from app.rp.turn_engine import (
    RPAdministratorProposalConflict,
    RPBackgroundJobConflict,
    RPIdempotencyConflict,
    RPPartyNotFound,
    RPPartyVersionConflict,
    RPPlayerCorrectionConflict,
    RPTurnEngine,
)
from app.services.auth_store import AuthStore, AuthUser
from app.services.provider_catalog import (
    NARRATOR_MODEL,
    NARRATOR_PROFILE_ID,
    narrator_profile,
    validate_narrator_settings,
)


logger = logging.getLogger(__name__)
RP_ANONYMOUS_OWNER = "anonymous"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    if settings.scenario_type != "rp":
        raise RuntimeError("RP gateway requires SCENARIO_TYPE=rp")
    if (
        settings.rp_atomic_service_enabled or settings.rp_administrator_enabled
    ) and not settings.local_llm_enabled:
        raise ValueError("enabled local RP roles require LOCAL_LLM_ENABLED=true")

    auth_store = AuthStore(settings)
    engine = RPTurnEngine(settings.rp_sqlite_path)
    atomic_model = RPAtomicServiceProvider(
        settings, provider="local", model=settings.local_llm_model_alias
    )
    administrator_model = RPAdministratorProvider(
        settings, provider="local", model=settings.local_llm_model_alias
    )
    runner = RPRunner(
        engine,
        RPAtomicServiceHandler(engine, atomic_model),
        RPAdministratorHandler(engine, administrator_model),
        service_enabled=settings.rp_atomic_service_enabled,
        administrator_enabled=settings.rp_administrator_enabled,
        poll_interval=settings.rp_runner_poll_interval_seconds,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        recovered = await runner.start()
        if any(recovered.values()):
            logger.warning("recovered_rp_work %s", recovered)
        try:
            yield
        finally:
            await runner.stop()

    app = FastAPI(title="RP Gateway", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.auth_store = auth_store
    app.state.rp_engine = engine
    app.state.rp_runner = runner
    profile = ModelProfileSummary.model_validate(narrator_profile(settings))

    def current_user(request: Request) -> AuthUser | None:
        if not settings.auth_enabled:
            return None
        user = getattr(request.state, "user", None)
        if not isinstance(user, AuthUser):
            raise HTTPException(status_code=401, detail="authentication required")
        return user

    def owner_id(request: Request) -> str:
        user = current_user(request)
        return user.id if user else RP_ANONYMOUS_OWNER

    def auth_owner_id(request: Request) -> str | None:
        value = owner_id(request)
        return None if value == RP_ANONYMOUS_OWNER else value

    def require_admin(request: Request) -> AuthUser | None:
        user = current_user(request)
        if settings.auth_enabled and (not user or not user.is_admin):
            raise HTTPException(status_code=403, detail="admin role required")
        return user

    def world_loader() -> WorldScenarioLoader:
        return WorldScenarioLoader(Path(settings.worldpacks_path) / SUPPORTED_WORLD_ID)

    def party_payload(party: Any) -> dict[str, Any]:
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

    def turn_payload(turn: Any) -> dict[str, Any]:
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

    def request_payload(item: Any) -> dict[str, Any]:
        return {
            "id": item.id,
            "party_id": item.party_id,
            "turn_kind": item.turn_kind,
            "request_id": item.request_id,
            "idempotency_key": item.idempotency_key,
            "expected_version": item.expected_version,
            "player_text": item.player_text,
            "status": item.status,
            "turn_id": item.turn_id,
            "last_error": item.last_error,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }

    def job_payload(job: Any) -> dict[str, Any]:
        value = {
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
            value["job_type"] = job.job_type
        else:
            value.update(
                {
                    "job_type": "administrator",
                    "window_start_version": job.window_start_version,
                    "window_end_version": job.window_end_version,
                    "evidence_versions": list(job.evidence_versions),
                    "window_hash": job.window_hash,
                }
            )
        return value

    def proposal_payload(item: Any) -> dict[str, Any]:
        return {
            "id": item.id,
            "party_id": item.party_id,
            "administrator_job_id": item.administrator_job_id,
            "kind": item.kind,
            "target_slot": item.target_slot,
            "before_text": item.before_text,
            "after_text": item.after_text,
            "base_party_version": item.base_party_version,
            "base_guidance_revision": item.base_guidance_revision,
            "evidence_versions": list(item.evidence_versions),
            "window_hash": item.window_hash,
            "status": item.status,
            "applied_party_version": item.applied_party_version,
            "created_at": item.created_at,
            "decided_at": item.decided_at,
        }

    def lore_payload(card: Any) -> dict[str, Any]:
        return {
            "id": card.id,
            "kind": card.kind,
            "origin": card.origin,
            "authoring_kind": card.authoring_kind,
            "title": card.title,
            "content": card.content,
            "keywords": list(card.keywords),
            "source_turn_id": card.source_turn_id,
            "source_version": card.source_version,
            "evidence_span_ids": list(card.evidence_span_ids),
            "enabled": card.enabled,
            "created_at": card.created_at,
        }

    def correction_payload(item: Any) -> dict[str, Any]:
        return {
            "id": item.id,
            "service_job_id": item.service_job_id,
            "base_party_version": item.base_party_version,
            "catalog_hash": item.catalog_hash,
            "target_slot": item.target_slot,
            "target_kind": item.target_kind,
            "action": item.action,
            "before": item.before_text,
            "after": item.after_text,
            "forbidden_claims": list(item.forbidden_claims),
            "status": item.status,
            "created_at": item.created_at,
            "decided_at": item.decided_at,
        }

    def settings_for_party(party: Any) -> Settings:
        key_owner = None if party.owner_user_id == RP_ANONYMOUS_OWNER else party.owner_user_id
        secret = auth_store.default_provider_secret(
            party.narrator_base_url,
            provider="openrouter",
            owner_user_id=key_owner,
            party_id=party.id,
            exact_base_url=True,
        ) or settings.openrouter_api_key
        if not secret:
            raise ValueError("Party narrator requires an exact OpenRouter key")
        return replace(
            settings,
            openrouter_api_base=party.narrator_base_url,
            openrouter_api_key=secret,
        )

    def ensure_narrator_binding(party: Any) -> None:
        if (
            party.narrator_profile_id != profile.id
            or party.narrator_provider != profile.provider
            or party.narrator_base_url.rstrip("/") != profile.base_url.rstrip("/")
            or party.narrator_model != profile.model
        ):
            raise HTTPException(status_code=409, detail="party narrator binding is retired")

    def narrator_service(party: Any, request_id: str) -> RPNarratorService:
        provider = RPNarratorProvider(
            settings_for_party(party),
            provider=party.narrator_provider,
            model=party.narrator_model,
            narrator_settings=party.narrator_settings,
            party_id=party.id,
            request_id=request_id,
        )
        return RPNarratorService(
            engine,
            provider,
            atomic_service_enabled=settings.rp_atomic_service_enabled,
            derived_wait_seconds=settings.rp_derived_wait_seconds,
            derived_poll_interval=settings.rp_runner_poll_interval_seconds,
        )

    async def await_player_operation(party_id: str, owner: str, job_id: int) -> Any:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(30.0, settings.rp_derived_wait_seconds)
        while True:
            job = engine.get_service_job(
                owner_user_id=owner, party_id=party_id, job_id=job_id
            )
            if job.status == "succeeded":
                return job
            if job.status == "failed":
                raise HTTPException(
                    status_code=502,
                    detail={
                        "code": "rp_player_operation_failed",
                        "job_id": job.id,
                        "message": job.last_error,
                    },
                )
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise HTTPException(
                    status_code=504,
                    detail={
                        "code": "rp_player_operation_pending",
                        "job_id": job.id,
                        "retryable": True,
                    },
                )
            await asyncio.sleep(min(settings.rp_runner_poll_interval_seconds, remaining))

    def role_status(
        role: str, enabled: bool, provider: str, model: str, work: tuple[Any, ...]
    ) -> dict[str, Any]:
        return {
            "role": role,
            "enabled": enabled,
            "kill_switch": not enabled,
            "provider": provider,
            "model": model,
            "status": work[-1].status if work else "idle",
            "success_count": sum(item.status == "succeeded" for item in work),
            "error_count": sum(item.status == "failed" for item in work),
            "last_error": next(
                (item.last_error for item in reversed(work) if item.last_error), None
            ),
        }

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if (
            settings.auth_enabled
            and request.url.path.startswith("/api/")
            and not request.url.path.startswith("/api/auth/")
        ):
            user = auth_store.user_for_session(
                request.cookies.get(settings.auth_session_cookie_name)
            )
            if user is None:
                return JSONResponse(
                    {"detail": "authentication required"}, status_code=401
                )
            request.state.user = user
        return await call_next(request)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        database = "ok"
        try:
            engine.list_parties(owner_user_id=RP_ANONYMOUS_OWNER)
            if not runner.running:
                database = "error"
        except Exception:  # noqa: BLE001 - health boundary
            database = "error"
        return HealthResponse(
            status="ok" if database == "ok" else "error",
            database=database,
            world_id=SUPPORTED_WORLD_ID,
        )

    @app.get("/api/auth/me")
    def auth_me(request: Request) -> dict[str, Any]:
        user = (
            auth_store.user_for_session(
                request.cookies.get(settings.auth_session_cookie_name)
            )
            if settings.auth_enabled
            else None
        )
        return {
            "auth_enabled": settings.auth_enabled,
            "authenticated": user is not None or not settings.auth_enabled,
            "user": user.public_dict() if user else None,
        }

    @app.post("/api/auth/login")
    def auth_login(payload: LoginRequest, response: Response) -> dict[str, Any]:
        if not settings.auth_enabled:
            return {"auth_enabled": False, "authenticated": True, "user": None}
        user = auth_store.authenticate(payload.username, payload.password)
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
        return {
            "auth_enabled": True,
            "authenticated": True,
            "user": user.public_dict(),
        }

    @app.post("/api/auth/logout")
    def auth_logout(request: Request, response: Response) -> dict[str, Any]:
        auth_store.delete_session(
            request.cookies.get(settings.auth_session_cookie_name)
        )
        response.delete_cookie(settings.auth_session_cookie_name)
        return {"logged_out": True}

    @app.get("/api/admin/users")
    def list_users(request: Request) -> dict[str, Any]:
        require_admin(request)
        return {
            "users": [
                {
                    **user.public_dict(),
                    "party_count": len(engine.list_parties(owner_user_id=user.id)),
                }
                for user in auth_store.list_users()
            ]
        }

    @app.post("/api/admin/users")
    def create_user(request: Request, payload: UserCreate) -> dict[str, Any]:
        require_admin(request)
        try:
            user = auth_store.create_user(
                payload.username, payload.password, payload.role
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"user": user.public_dict()}

    @app.patch("/api/admin/users/{user_id}/password")
    def set_user_password(
        request: Request, user_id: str, payload: UserPasswordUpdate
    ) -> dict[str, Any]:
        require_admin(request)
        try:
            user = auth_store.set_password(user_id, payload.password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"user": user.public_dict()}

    @app.patch("/api/admin/users/{user_id}/status")
    def set_user_status(
        request: Request, user_id: str, payload: UserStatusUpdate
    ) -> dict[str, Any]:
        require_admin(request)
        try:
            user = auth_store.set_user_status(user_id, payload.status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"user": user.public_dict()}

    @app.delete("/api/admin/users/{user_id}")
    def delete_user(
        request: Request,
        user_id: str,
        payload: UserDeleteRequest = UserDeleteRequest(),
    ) -> dict[str, Any]:
        admin = require_admin(request)
        if admin and admin.id == user_id:
            raise HTTPException(
                status_code=400, detail="cannot delete the current admin session user"
            )
        if engine.list_parties(owner_user_id=user_id):
            raise HTTPException(status_code=400, detail="user still owns RP parties")
        try:
            auth_store.delete_user(user_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "deleted": True,
            "user_id": user_id,
            "deleted_data": payload.delete_data,
        }

    @app.get("/api/worldpacks")
    def list_worldpacks(scenario_type: str | None = None) -> dict[str, Any]:
        if scenario_type not in {None, "rp"}:
            raise HTTPException(status_code=400, detail="scenario_type must be rp")
        try:
            loader = world_loader()
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
                            "id": item.id,
                            "title": item.title,
                            "player_role": item.player_role,
                            "style": item.style,
                            "format": item.format,
                            "difficulty": item.difficulty,
                            "detail_level": item.detail_level,
                        }
                        for item in presets
                    ],
                }
            ]
        }

    @app.get("/api/worldpacks/{world_id}")
    def get_worldpack(world_id: str) -> dict[str, Any]:
        if world_id != SUPPORTED_WORLD_ID:
            raise HTTPException(status_code=404, detail="worldpack not found")
        worldpack = list_worldpacks()["worldpacks"][0]
        try:
            preset = world_loader().materialize_preset(
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

    @app.get("/api/model-profiles")
    def list_model_profiles() -> dict[str, Any]:
        return {"model_profiles": [profile.model_dump(mode="json")]}

    @app.get("/api/parties")
    def list_parties(request: Request) -> dict[str, Any]:
        return {
            "parties": [
                party_payload(item)
                for item in engine.list_parties(owner_user_id=owner_id(request))
            ]
        }

    @app.post("/api/parties")
    def create_party(request: Request, payload: RPPartyCreate) -> dict[str, Any]:
        if payload.model_profile_id != NARRATOR_PROFILE_ID:
            raise HTTPException(
                status_code=400, detail="model profile is unavailable for RP"
            )
        try:
            loader = world_loader()
            world = loader.materialize_world()
            if isinstance(payload.scenario, RPScenarioFreeCreate):
                scenario = loader.materialize_free_scenario(
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
                    active_character_ids=tuple(payload.scenario.active_character_ids),
                    local_overrides=payload.scenario.local_overrides,
                )
            else:
                scenario = loader.materialize_preset(payload.scenario.preset_id)
            narrator_settings = validate_narrator_settings(
                "openrouter",
                NARRATOR_MODEL,
                payload.narrator_settings.model_dump(mode="json", exclude_none=True)
                if payload.narrator_settings
                else {},
            )
            party = engine.create_party(
                owner_user_id=owner_id(request),
                party_id=f"party_{uuid.uuid4().hex[:12]}",
                title=payload.title,
                world_snapshot=world,
                scenario_snapshot=scenario,
                narrator_profile_id=profile.id,
                narrator_provider=profile.provider,
                narrator_base_url=profile.base_url,
                narrator_model=profile.model,
                narrator_settings=narrator_settings,
            )
        except (ValueError, WorldSourceError, ScenarioPresetNotFound) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"party": party_payload(party)}

    @app.get("/api/parties/{party_id}")
    def get_party(request: Request, party_id: str) -> dict[str, Any]:
        try:
            party = engine.get_party(
                owner_user_id=owner_id(request), party_id=party_id
            )
        except RPPartyNotFound as exc:
            raise HTTPException(status_code=404, detail="party not found") from exc
        return {"party": party_payload(party)}

    @app.get("/api/parties/{party_id}/byok")
    def list_byok(request: Request, party_id: str) -> dict[str, Any]:
        try:
            engine.get_party(owner_user_id=owner_id(request), party_id=party_id)
        except RPPartyNotFound as exc:
            raise HTTPException(status_code=404, detail="party not found") from exc
        return {
            "party_id": party_id,
            "api_keys": [
                key.public_dict()
                for key in auth_store.list_provider_api_keys(
                    auth_owner_id(request), party_id
                )
            ],
        }

    @app.post("/api/parties/{party_id}/byok")
    def create_byok(
        request: Request, party_id: str, payload: ProviderApiKeyCreate
    ) -> dict[str, Any]:
        try:
            party = engine.get_party(
                owner_user_id=owner_id(request), party_id=party_id
            )
            requested_base = (
                payload.base_url or settings.openrouter_api_base
            ).rstrip("/")
            if requested_base != party.narrator_base_url.rstrip("/"):
                raise ValueError(
                    "BYOK base_url must match the Party narrator binding"
                )
            key = auth_store.create_provider_api_key(
                label=payload.label,
                secret_value=payload.api_key,
                provider="openrouter",
                base_url=party.narrator_base_url,
                is_default=payload.is_default,
                owner_user_id=auth_owner_id(request),
                party_id=party_id,
            )
        except RPPartyNotFound as exc:
            raise HTTPException(status_code=404, detail="party not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"party_id": party_id, "api_key": key.public_dict()}

    @app.patch("/api/parties/{party_id}/byok/{key_id}")
    def update_byok(
        request: Request,
        party_id: str,
        key_id: str,
        payload: ProviderApiKeyUpdate,
    ) -> dict[str, Any]:
        try:
            engine.get_party(owner_user_id=owner_id(request), party_id=party_id)
            key = auth_store.update_provider_api_key(
                key_id,
                label=payload.label,
                secret_value=payload.api_key,
                is_default=payload.is_default,
                owner_user_id=auth_owner_id(request),
                party_id=party_id,
            )
        except RPPartyNotFound as exc:
            raise HTTPException(status_code=404, detail="party not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"party_id": party_id, "api_key": key.public_dict()}

    @app.delete("/api/parties/{party_id}/byok/{key_id}")
    def delete_byok(request: Request, party_id: str, key_id: str) -> dict[str, Any]:
        try:
            engine.get_party(owner_user_id=owner_id(request), party_id=party_id)
            auth_store.delete_provider_api_key(
                key_id, auth_owner_id(request), party_id
            )
        except RPPartyNotFound as exc:
            raise HTTPException(status_code=404, detail="party not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"deleted": True, "party_id": party_id, "api_key_id": key_id}

    @app.get("/api/parties/{party_id}/history")
    def history(
        request: Request, party_id: str, limit: int = 50
    ) -> dict[str, Any]:
        try:
            turns = engine.list_turns(
                owner_user_id=owner_id(request), party_id=party_id
            )
        except RPPartyNotFound as exc:
            raise HTTPException(status_code=404, detail="party not found") from exc
        bounded = min(max(limit, 1), 200)
        return {
            "party_id": party_id,
            "turns": [turn_payload(item) for item in turns[-bounded:]],
        }

    @app.get("/api/parties/{party_id}/requests/{request_id}")
    def request_status(
        request: Request, party_id: str, request_id: str
    ) -> dict[str, Any]:
        owner = owner_id(request)
        try:
            item = engine.get_narration_request_by_request_id(
                owner_user_id=owner, party_id=party_id, request_id=request_id
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
                candidate
                for candidate in engine.list_turns(
                    owner_user_id=owner, party_id=party_id
                )
                if candidate.id == item.turn_id
            ),
            None,
        )
        return {
            "party_id": party_id,
            "request_id": request_id,
            "status": "completed" if item.status == "succeeded" else item.status,
            "error": item.last_error,
            "turn": turn_payload(turn) if turn else None,
            "request": request_payload(item),
        }

    @app.get("/api/parties/{party_id}/memory")
    def memory(request: Request, party_id: str) -> dict[str, Any]:
        try:
            item = engine.latest_story_memory(
                owner_user_id=owner_id(request), party_id=party_id
            )
        except RPPartyNotFound as exc:
            raise HTTPException(status_code=404, detail="party not found") from exc
        value = (
            None
            if item is None
            else {
                "id": item.id,
                "revision": item.revision,
                "base_snapshot_id": item.base_snapshot_id,
                "update_id": item.update_id,
                "snapshot": item.snapshot.model_dump(mode="json"),
                "safe_coverage": item.snapshot.safe_coverage,
                "created_at": item.created_at,
            }
        )
        return {"party_id": party_id, "story_memory": value}

    @app.get("/api/parties/{party_id}/service-jobs")
    def service_jobs(
        request: Request, party_id: str, limit: int = 20
    ) -> dict[str, Any]:
        owner = owner_id(request)
        try:
            jobs = [
                *(
                    job_payload(item)
                    for item in engine.list_service_jobs(
                        owner_user_id=owner, party_id=party_id
                    )
                ),
                *(
                    job_payload(item)
                    for item in engine.list_administrator_jobs(
                        owner_user_id=owner, party_id=party_id
                    )
                ),
            ]
        except RPPartyNotFound as exc:
            raise HTTPException(status_code=404, detail="party not found") from exc
        jobs.sort(key=lambda item: (item["created_at"], item["id"]))
        bounded = min(max(limit, 1), 100)
        return {"party_id": party_id, "jobs": jobs[-bounded:]}

    @app.get("/api/parties/{party_id}/lore-cards")
    def lore_cards(request: Request, party_id: str) -> dict[str, Any]:
        owner = owner_id(request)
        try:
            party = engine.get_party(owner_user_id=owner, party_id=party_id)
            derived = engine.derived_context(owner_user_id=owner, party_id=party_id)
        except RPPartyNotFound as exc:
            raise HTTPException(status_code=404, detail="party not found") from exc
        cards = [
            {**card, "origin": "world"}
            for bundle in party.world_snapshot.seed_lore_cards
            for card in (
                bundle.get("cards", [])
                if isinstance(bundle.get("cards"), list)
                else []
            )
            if isinstance(card, dict)
        ]
        cards.extend(
            {**card.model_dump(mode="json"), "origin": "scenario"}
            for card in party.scenario_snapshot.local_overrides.lore_cards
        )
        cards.extend(lore_payload(card) for card in derived.runtime_lore_cards)
        return {"party_id": party_id, "cards": cards}

    @app.post("/api/parties/{party_id}/lore-cards/draft")
    async def draft_lore(
        request: Request, party_id: str, payload: PartyLoreCardDraftRequest
    ) -> dict[str, Any]:
        if not settings.rp_atomic_service_enabled:
            raise HTTPException(status_code=503, detail="RP atomic service is disabled")
        owner = owner_id(request)
        source_turn_id = payload.source_turn_ids[0]
        try:
            party = engine.get_party(owner_user_id=owner, party_id=party_id)
            turn = next(
                (
                    item
                    for item in engine.list_turns(
                        owner_user_id=owner, party_id=party_id
                    )
                    if item.id == source_turn_id
                ),
                None,
            )
            if turn is None:
                raise ValueError(
                    "source_turn_ids must reference one complete committed turn"
                )
            job = engine.enqueue_player_operation(
                owner_user_id=owner,
                party_id=party.id,
                job_type="player_lore",
                source_turn_id=source_turn_id,
                expected_version=payload.expected_version,
                operation_id=f"player-lore:{payload.idempotency_key}",
                operation={
                    "expected_version": payload.expected_version,
                    "kind": payload.kind,
                    "source_turn_id": source_turn_id,
                },
            )
            job = await await_player_operation(party_id, owner, job.id)
        except RPPartyNotFound as exc:
            raise HTTPException(status_code=404, detail="party not found") from exc
        except (RPIdempotencyConflict, RPPartyVersionConflict) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = job.result or {}
        card = result.get("card")
        if result.get("result") == "no_candidate" or not isinstance(card, dict):
            return {
                "party_id": party_id,
                "job_id": job.id,
                "result": "no_candidate",
                "kind": payload.kind,
                "title": None,
                "content": None,
                "keywords": None,
                "evidence_span_ids": None,
                "source_turn_ids": [source_turn_id],
            }
        return {
            "party_id": party_id,
            "job_id": job.id,
            "result": "draft",
            "kind": card["kind"],
            "title": card["title"],
            "content": card["content"],
            "keywords": card["keywords"],
            "evidence_span_ids": card["evidence_span_ids"],
            "source_turn_ids": [source_turn_id],
        }

    @app.post("/api/parties/{party_id}/lore-cards")
    def confirm_lore(
        request: Request, party_id: str, payload: PartyLoreCardCreate
    ) -> dict[str, Any]:
        owner = owner_id(request)
        try:
            job = engine.get_service_job(
                owner_user_id=owner,
                party_id=party_id,
                job_id=payload.draft_job_id,
            )
            draft = (job.result or {}).get("card")
            if not isinstance(draft, dict):
                raise ValueError("Lore draft job has no confirmable card")
            if payload.source_turn_ids != [job.source_turn_id]:
                raise ValueError("Lore confirmation must keep the draft source turn")
            reviewed = PartyLoreCardDraft(
                title=payload.title,
                content=payload.content,
                keywords=payload.keywords,
            )
            card = engine.confirm_player_lore_card(
                owner_user_id=owner,
                party_id=party_id,
                service_job_id=payload.draft_job_id,
                expected_version=payload.expected_version,
                idempotency_key=payload.idempotency_key,
                authoring_kind=payload.kind,
                title=reviewed.title,
                content=reviewed.content,
                keywords=tuple(reviewed.keywords),
                enabled=payload.enabled,
            )
        except RPPartyNotFound as exc:
            raise HTTPException(status_code=404, detail="party not found") from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Lore draft not found") from exc
        except (RPIdempotencyConflict, RPPartyVersionConflict) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (RPBackgroundJobConflict, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"party_id": party_id, "card": lore_payload(card)}

    @app.get("/api/parties/{party_id}/player-corrections")
    def list_corrections(request: Request, party_id: str) -> dict[str, Any]:
        try:
            items = engine.list_player_corrections(
                owner_user_id=owner_id(request), party_id=party_id
            )
        except RPPartyNotFound as exc:
            raise HTTPException(status_code=404, detail="party not found") from exc
        return {
            "party_id": party_id,
            "proposals": [correction_payload(item) for item in items],
        }

    @app.post("/api/parties/{party_id}/player-corrections/draft")
    async def draft_correction(
        request: Request, party_id: str, payload: RPPlayerCorrectionDraftRequest
    ) -> dict[str, Any]:
        if not settings.rp_atomic_service_enabled:
            raise HTTPException(status_code=503, detail="RP atomic service is disabled")
        owner = owner_id(request)
        operation_id = f"player-correction:{payload.idempotency_key}"
        try:
            party = engine.get_party(owner_user_id=owner, party_id=party_id)
            existing = next(
                (
                    job
                    for job in engine.list_service_jobs(
                        owner_user_id=owner, party_id=party_id
                    )
                    if job.operation_id == operation_id
                ),
                None,
            )
            if existing:
                expected = {
                    "instruction": payload.instruction,
                    "raw_hint": payload.raw_hint,
                    "expected_version": payload.expected_version,
                }
                if existing.job_type != "player_correction" or any(
                    existing.operation.get(key) != value
                    for key, value in expected.items()
                ):
                    raise RPIdempotencyConflict(
                        "PlayerCorrection idempotency key owns different input"
                    )
                job = existing
            else:
                turns = engine.list_turns(owner_user_id=owner, party_id=party_id)
                if not turns:
                    raise ValueError(
                        "PlayerCorrection requires at least one committed turn"
                    )
                memory = engine.latest_story_memory(
                    owner_user_id=owner, party_id=party_id
                )
                catalog = player_correction_catalog(
                    party=party, turns=turns, memory=memory
                )
                if payload.raw_hint is not None and not any(
                    str(item["target_slot"]) == payload.raw_hint
                    or (
                        payload.raw_hint.count(":") == 1
                        and str(item["target_slot"]).startswith(
                            payload.raw_hint + ":"
                        )
                    )
                    for item in catalog
                ):
                    raise ValueError(
                        "raw_hint does not match the Party RAW catalog"
                    )
                job = engine.enqueue_player_operation(
                    owner_user_id=owner,
                    party_id=party_id,
                    job_type="player_correction",
                    source_turn_id=turns[-1].id,
                    expected_version=payload.expected_version,
                    operation_id=operation_id,
                    operation={
                        "expected_version": payload.expected_version,
                        "instruction": payload.instruction,
                        "raw_hint": payload.raw_hint,
                        "catalog_hash": player_correction_catalog_hash(catalog),
                        "catalog": list(catalog),
                    },
                )
            job = await await_player_operation(party_id, owner, job.id)
        except RPPartyNotFound as exc:
            raise HTTPException(status_code=404, detail="party not found") from exc
        except (RPIdempotencyConflict, RPPartyVersionConflict) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = job.result or {}
        return {
            "party_id": party_id,
            "job_id": job.id,
            "result": result.get("result"),
            "proposal": result.get("proposal"),
        }

    @app.post("/api/parties/{party_id}/player-corrections/{proposal_id}/decision")
    def decide_correction(
        request: Request,
        party_id: str,
        proposal_id: int,
        payload: RPPlayerCorrectionDecision,
    ) -> dict[str, Any]:
        owner = owner_id(request)
        try:
            party = engine.get_party(owner_user_id=owner, party_id=party_id)
            catalog = player_correction_catalog(
                party=party,
                turns=engine.list_turns(owner_user_id=owner, party_id=party_id),
                memory=engine.latest_story_memory(
                    owner_user_id=owner, party_id=party_id
                ),
            )
            proposal, overlay = engine.decide_player_correction(
                owner_user_id=owner,
                party_id=party_id,
                proposal_id=proposal_id,
                decision=payload.decision,
                expected_version=payload.expected_version,
                idempotency_key=payload.idempotency_key,
                catalog=catalog,
            )
        except RPPartyNotFound as exc:
            raise HTTPException(status_code=404, detail="party not found") from exc
        except LookupError as exc:
            raise HTTPException(
                status_code=404, detail="PlayerCorrection proposal not found"
            ) from exc
        except RPPlayerCorrectionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "party_id": party_id,
            "proposal": correction_payload(proposal),
            "overlay": (
                {
                    "revision": overlay.revision,
                    "applies_to_version": overlay.applies_to_version,
                }
                if overlay
                else None
            ),
        }

    @app.get("/api/parties/{party_id}/supervisor")
    def supervisor(request: Request, party_id: str) -> dict[str, Any]:
        owner = owner_id(request)
        try:
            party = engine.get_party(owner_user_id=owner, party_id=party_id)
            narrations = engine.list_narration_requests(
                owner_user_id=owner, party_id=party_id
            )
            service = engine.list_service_jobs(
                owner_user_id=owner, party_id=party_id
            )
            administrator = engine.list_administrator_jobs(
                owner_user_id=owner, party_id=party_id
            )
        except RPPartyNotFound as exc:
            raise HTTPException(status_code=404, detail="party not found") from exc
        return {
            "party_id": party_id,
            "roles": {
                "narrator": role_status(
                    "narrator",
                    settings.rp_narrator_enabled,
                    party.narrator_provider,
                    party.narrator_model,
                    narrations,
                ),
                "atomic_service": role_status(
                    "atomic_service",
                    settings.rp_atomic_service_enabled,
                    "local",
                    settings.local_llm_model_alias,
                    service,
                ),
                "administrator": role_status(
                    "administrator",
                    settings.rp_administrator_enabled,
                    "local",
                    settings.local_llm_model_alias,
                    administrator,
                ),
            },
        }

    @app.get("/api/parties/{party_id}/administrator/proposals")
    def list_admin_proposals(request: Request, party_id: str) -> dict[str, Any]:
        try:
            items = engine.list_administrator_proposals(
                owner_user_id=owner_id(request), party_id=party_id
            )
        except RPPartyNotFound as exc:
            raise HTTPException(status_code=404, detail="party not found") from exc
        return {
            "party_id": party_id,
            "proposals": [proposal_payload(item) for item in items],
        }

    @app.post("/api/parties/{party_id}/administrator/proposals/{proposal_id}/decision")
    def decide_admin_proposal(
        request: Request,
        party_id: str,
        proposal_id: int,
        payload: RPAdministratorProposalDecision,
    ) -> dict[str, Any]:
        owner = owner_id(request)
        try:
            proposal = engine.decide_administrator_proposal(
                owner_user_id=owner,
                party_id=party_id,
                proposal_id=proposal_id,
                decision=payload.decision,
            )
            party = engine.get_party(owner_user_id=owner, party_id=party_id)
        except RPPartyNotFound as exc:
            raise HTTPException(status_code=404, detail="party not found") from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="proposal not found") from exc
        except RPAdministratorProposalConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "party_id": party_id,
            "state_version": party.current_version,
            "proposal": proposal_payload(proposal),
        }

    @app.post("/api/parties/{party_id}/start")
    async def start_party(
        request: Request,
        party_id: str,
        payload: RPPartyStartRequest = RPPartyStartRequest(),
        x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
    ) -> dict[str, Any]:
        owner = owner_id(request)
        key = payload.idempotency_key or f"party-start:{party_id}"
        request_id = x_request_id or key
        existing = None
        try:
            party = engine.get_party(owner_user_id=owner, party_id=party_id)
            ensure_narrator_binding(party)
            try:
                existing = engine.get_narration_request(
                    owner_user_id=owner,
                    party_id=party_id,
                    idempotency_key=key,
                )
            except LookupError:
                pass
            if party.current_version != 0 and existing is None:
                raise RPPartyVersionConflict("party already has committed history")
            if existing is not None and x_request_id is None:
                request_id = existing.request_id
            if not settings.rp_narrator_enabled and (
                existing is None or existing.status != "succeeded"
            ):
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "rp_narrator_disabled",
                        "retryable": True,
                        "request_id": request_id,
                        "idempotency_key": key,
                    },
                )
            turn = await narrator_service(party, request_id).narrate_opening(
                owner_user_id=owner,
                party_id=party_id,
                request_id=request_id,
                idempotency_key=key,
            )
            current = engine.get_party(owner_user_id=owner, party_id=party_id)
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
                    "idempotency_key": key,
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "party_id": party_id,
            "started": existing is None,
            "already_started": existing is not None,
            "state_version": current.current_version,
            "message": {"role": "assistant", "content": turn.narrator_text},
            "turn": turn_payload(turn),
        }

    @app.post("/api/parties/{party_id}/messages")
    async def party_message(
        request: Request,
        party_id: str,
        payload: RPPartyMessageRequest,
        x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
    ) -> dict[str, Any]:
        owner = owner_id(request)
        request_id = x_request_id or payload.idempotency_key
        try:
            party = engine.get_party(owner_user_id=owner, party_id=party_id)
            ensure_narrator_binding(party)
            try:
                existing = engine.get_narration_request(
                    owner_user_id=owner,
                    party_id=party_id,
                    idempotency_key=payload.idempotency_key,
                )
            except LookupError:
                existing = None
            if existing is not None and x_request_id is None:
                request_id = existing.request_id
            if not settings.rp_narrator_enabled and (
                existing is None or existing.status != "succeeded"
            ):
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "rp_narrator_disabled",
                        "retryable": True,
                        "request_id": request_id,
                        "idempotency_key": payload.idempotency_key,
                        "player_text": payload.content,
                    },
                )
            turn = await narrator_service(party, request_id).narrate_turn(
                owner_user_id=owner,
                party_id=party_id,
                request_id=request_id,
                idempotency_key=payload.idempotency_key,
                expected_version=payload.expected_version,
                player_text=payload.content,
            )
            current = engine.get_party(owner_user_id=owner, party_id=party_id)
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
                    "idempotency_key": payload.idempotency_key,
                    "player_text": payload.content,
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "party_id": party_id,
            "state_version": current.current_version,
            "message": {"role": "assistant", "content": turn.narrator_text},
            "turn": turn_payload(turn),
        }

    return app
