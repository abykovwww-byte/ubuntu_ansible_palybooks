#!/usr/bin/env python3
"""Generate reviewable WorldPack Lore Card candidates outside runtime."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORLDPACKS_ROOT = ROOT / "roles" / "apps" / "files" / "rp-stack" / "worldpacks"
MODEL = "deepseek/deepseek-v4-pro"
MAX_PROMPT_CHARS = 8_000
MAX_SOURCE_CHARS = 6_400
SOURCE_KEYS = ("campaign_bible", "world_info", "characters")
CARD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["cards"],
    "properties": {
        "cards": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "title", "keywords", "content", "always_on", "enabled"],
                "properties": {
                    "key": {"type": "string"},
                    "title": {"type": "string"},
                    "keywords": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                    "content": {"type": "string"},
                    "always_on": {"type": "boolean"},
                    "enabled": {"type": "boolean"},
                },
            },
        }
    },
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Call the stack-managed OpenRouter model at authoring time and write candidate Lore Cards for human review."
        )
    )
    parser.add_argument("worldpack", help="WorldPack slug under roles/apps/files/rp-stack/worldpacks")
    parser.add_argument(
        "--output",
        default="lore-cards/generated.json",
        help="Output path inside the WorldPack (default: lore-cards/generated.json)",
    )
    parser.add_argument("--force", action="store_true", help="Replace an existing candidate file")
    return parser.parse_args()


def safe_worldpack(slug: str) -> tuple[Path, dict[str, Any]]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        raise ValueError("worldpack must be a lowercase ASCII slug")
    pack_root = (WORLDPACKS_ROOT / slug).resolve()
    if WORLDPACKS_ROOT.resolve() not in pack_root.parents:
        raise ValueError("worldpack path escapes the repository WorldPack root")
    manifest_path = pack_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return pack_root, manifest


def source_sections(pack_root: Path, manifest: dict[str, Any]) -> list[str]:
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    sections: list[str] = []
    for key in SOURCE_KEYS:
        relative = files.get(key)
        if not isinstance(relative, str) or not relative.strip():
            continue
        path = (pack_root / relative).resolve()
        if pack_root not in path.parents or not path.is_file():
            raise ValueError(f"unsafe or missing source file for {key}")
        text = path.read_text(encoding="utf-8").strip()
        chunks = re.split(r"(?m)(?=^##?\s+)", text)
        for chunk in chunks:
            chunk = chunk.strip()
            while chunk:
                sections.append(f"SOURCE {relative}\n{chunk[:MAX_SOURCE_CHARS]}")
                chunk = chunk[MAX_SOURCE_CHARS:].strip()
    if not sections:
        raise ValueError("WorldPack has no campaign_bible, world_info, or characters source text")
    return sections


def source_batches(sections: list[str]) -> list[str]:
    batches: list[str] = []
    current = ""
    for section in sections:
        trial = f"{current}\n\n{section}".strip()
        if current and len(trial) > MAX_SOURCE_CHARS:
            batches.append(current)
            current = section
        else:
            current = trial
    if current:
        batches.append(current)
    return batches


def completion_payload(source: str) -> dict[str, Any]:
    return {
        "model": MODEL,
        "temperature": 0.1,
        "max_tokens": 4_000,
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "worldpack_lore_cards", "strict": True, "schema": CARD_SCHEMA},
        },
        "messages": [
            {
                "role": "system",
                "content": (
                    "Нарежь только предоставленный авторский текст на компактные независимые RP Lore Cards. "
                    "Не добавляй фактов. Для NPC используй key=npc:<id из заголовка>, canonical title, все русские "
                    "падежные aliases в keywords, private goal, hard boundaries и скрытые факты в content. "
                    "NPC cards всегда always_on=false. Для мест, улик, правил и сюжетных давлений используй стабильные "
                    "lowercase ASCII keys. Каждая карточка должна иметь непустые точные keywords."
                ),
            },
            {"role": "user", "content": source},
        ],
    }


def call_openrouter(payload: dict[str, Any], api_key: str, api_base: str) -> list[dict[str, Any]]:
    exact_messages = json.dumps(payload["messages"], ensure_ascii=False, separators=(",", ":"))
    if len(exact_messages) > MAX_PROMPT_CHARS:
        raise ValueError("authoring prompt exceeds 8000 characters")
    request = urllib.request.Request(
        f"{api_base.rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - explicit OpenRouter endpoint
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"OpenRouter returned HTTP {exc.code}") from exc
    choice = body.get("choices", [{}])[0]
    if choice.get("finish_reason") == "length":
        raise RuntimeError("OpenRouter truncated the authoring response")
    result = json.loads(choice.get("message", {}).get("content", ""))
    cards = result.get("cards") if isinstance(result, dict) else None
    if not isinstance(cards, list) or not cards:
        raise ValueError("authoring response contains no cards")
    return cards


def validate_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in cards:
        if not isinstance(card, dict):
            raise ValueError("authoring response contains a non-object card")
        key = str(card.get("key") or "").strip()
        title = str(card.get("title") or "").strip()
        content = str(card.get("content") or "").strip()
        keywords = list(
            dict.fromkeys(str(value).strip() for value in card.get("keywords", []) if str(value).strip())
        )
        if not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,127}", key) or key in seen:
            raise ValueError(f"invalid or duplicate generated key: {key}")
        if not title or not content or not keywords:
            raise ValueError(f"generated card is incomplete: {key}")
        if not isinstance(card.get("always_on", False), bool) or not isinstance(card.get("enabled", True), bool):
            raise ValueError(f"generated card has invalid flags: {key}")
        if key.startswith("npc:") and card.get("always_on") is not False:
            raise ValueError(f"generated NPC card must use always_on=false: {key}")
        seen.add(key)
        normalized.append(
            {
                "key": key,
                "title": title,
                "keywords": keywords,
                "content": content,
                "always_on": bool(card.get("always_on", False)),
                "enabled": bool(card.get("enabled", True)),
            }
        )
    return normalized


def main() -> int:
    args = arguments()
    api_key = os.getenv("SERVICE_OPENROUTER_API_KEY", "").strip()
    if not api_key:
        print("SERVICE_OPENROUTER_API_KEY is required", file=sys.stderr)
        return 2
    pack_root, manifest = safe_worldpack(args.worldpack)
    output_path = (pack_root / args.output).resolve()
    if pack_root not in output_path.parents or output_path.suffix.lower() != ".json":
        raise ValueError("output must be a JSON file inside the WorldPack")
    if output_path.exists() and not args.force:
        raise FileExistsError(f"refusing to replace {output_path}; pass --force after human review")
    cards: list[dict[str, Any]] = []
    api_base = os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
    for batch in source_batches(source_sections(pack_root, manifest)):
        cards.extend(call_openrouter(completion_payload(batch), api_key, api_base))
    payload = {
        "schema_version": "rp-gateway.worldpack-lore-cards.v1",
        "cards": validate_cards(cards),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(payload['cards'])} candidate cards to {output_path}")
    print("Review every card against the source before committing it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
