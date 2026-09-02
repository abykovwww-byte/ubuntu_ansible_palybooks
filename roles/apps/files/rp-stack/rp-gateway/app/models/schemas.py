"""Closed HTTP models for the Decision 043 RP runtime."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.rp.content import ScenarioLocalOverrides


WorldChoiceId = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$"),
]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelProfileSummary(_ClosedModel):
    id: str
    title: str
    provider: Literal["openrouter"]
    base_url: str = Field(exclude=True, repr=False)
    model: str
    params: dict[str, Any] = Field(default_factory=dict)
    api_key_source: str
    description: str = ""
    rp_fit: str = ""
    context_window: str = ""
    tags: list[str] = Field(default_factory=list)
    source: str = "static"
    availability: str = ""
    is_free: bool = False
    pricing_prompt: str = ""
    pricing_completion: str = ""
    pricing_input_cache_read: str = ""
    pricing_input_cache_write: str = ""
    pricing_input_cache_write_1h: str = ""
    rp_specialized: bool = False


class NarratorSettings(_ClosedModel):
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] | None = None
    max_tokens: Literal[1024, 2048, 4096, 8192, 16384] | None = None


class RPScenarioPresetCreate(_ClosedModel):
    source: Literal["preset"]
    preset_id: WorldChoiceId


class RPScenarioFreeCreate(_ClosedModel):
    source: Literal["free"]
    scenario_id: WorldChoiceId
    title: str = Field(min_length=1, max_length=160)
    player_role: str = Field(min_length=1, max_length=4000)
    style: str = Field(min_length=1, max_length=4000)
    format: str = Field(min_length=1, max_length=160)
    difficulty: str | None = Field(default=None, max_length=160)
    detail_level: str = Field(min_length=1, max_length=160)
    narrator_system: str = Field(min_length=1, max_length=12000)
    narrator_note: str = Field(min_length=1, max_length=12000)
    opening: str = Field(min_length=1, max_length=12000)
    initial_state: dict[str, Any]
    active_character_ids: list[WorldChoiceId] = Field(min_length=1, max_length=100)
    local_overrides: ScenarioLocalOverrides = Field(default_factory=ScenarioLocalOverrides)


RPScenarioCreate = Annotated[
    RPScenarioPresetCreate | RPScenarioFreeCreate,
    Field(discriminator="source"),
]


class RPPartyCreate(_ClosedModel):
    title: str = Field(min_length=1, max_length=160)
    world_id: Literal["day-watch-moscow-v2"]
    scenario: RPScenarioCreate
    model_profile_id: str = Field(min_length=1, max_length=160)
    narrator_settings: NarratorSettings | None = None


class RPPartyStartRequest(_ClosedModel):
    idempotency_key: str | None = Field(default=None, max_length=200)


class RPPartyMessageRequest(_ClosedModel):
    content: str = Field(min_length=1, max_length=12000)
    idempotency_key: str = Field(min_length=1, max_length=200)
    expected_version: int = Field(ge=1)


class PartyLoreCardDraftRequest(_ClosedModel):
    source_turn_ids: list[int] = Field(min_length=1, max_length=1)
    kind: Literal["character", "event", "location"]
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("source_turn_ids")
    @classmethod
    def validate_source_turn_ids(cls, value: list[int]) -> list[int]:
        if value[0] <= 0:
            raise ValueError("source_turn_ids must contain a positive turn ID")
        return value


class PartyLoreCardDraft(_ClosedModel):
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=12000)
    keywords: list[str] = Field(min_length=1, max_length=40)

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, value: list[str]) -> list[str]:
        normalized = list(
            dict.fromkeys(str(keyword).strip() for keyword in value if str(keyword).strip())
        )
        if not normalized:
            raise ValueError("keywords must contain at least one non-empty trigger")
        return normalized


class PartyLoreCardCreate(PartyLoreCardDraft):
    source_turn_ids: list[int] = Field(min_length=1, max_length=1)
    kind: Literal["character", "event", "location"]
    draft_job_id: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    always_on: Literal[False] = False
    enabled: bool = True


class RPPlayerCorrectionDraftRequest(_ClosedModel):
    instruction: str = Field(min_length=1, max_length=4000)
    raw_hint: str | None = Field(
        default=None,
        max_length=280,
        pattern=r"^raw:[1-9][0-9]*(?::[a-f0-9]{20})?$",
    )
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)


class RPPlayerCorrectionDecision(_ClosedModel):
    decision: Literal["accept", "reject"]
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)


class RPAdministratorProposalDecision(_ClosedModel):
    decision: Literal["accept", "reject"]


class HealthResponse(_ClosedModel):
    status: str
    database: str
    world_id: Literal["day-watch-moscow-v2"]


class LoginRequest(_ClosedModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class UserCreate(_ClosedModel):
    username: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=6, max_length=200)
    role: Literal["admin", "user"] = "user"


class UserPasswordUpdate(_ClosedModel):
    password: str = Field(min_length=6, max_length=200)


class UserStatusUpdate(_ClosedModel):
    status: Literal["active", "disabled"]


class UserDeleteRequest(_ClosedModel):
    delete_data: Literal[False] = False


class ProviderApiKeyCreate(_ClosedModel):
    label: str = Field(default="OpenRouter key", min_length=1, max_length=120)
    api_key: str = Field(min_length=1, max_length=400)
    provider: Literal["openrouter"] = "openrouter"
    base_url: str | None = Field(default=None, max_length=300)
    is_default: bool = True


class ProviderApiKeyUpdate(_ClosedModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    api_key: str | None = Field(default=None, min_length=1, max_length=400)
    is_default: bool | None = None
