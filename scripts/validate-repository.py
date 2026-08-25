#!/usr/bin/env python3
"""Validate repository-level Codex, Wiki, JSON, and plugin contracts."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IGNORED_PARTS = {".git", ".venv", "graphify-out", "node_modules", "__pycache__"}
LINK_RE = re.compile(r"(?<!!)\[[^]]*]\(([^)]+)\)")
SSH_COMMAND_RE = re.compile(
    r"(?i)(?:^|[\\/\s])ssh(?:\.exe)?\s+(?:-[A-Za-z]|[A-Za-z0-9._-]+@[A-Za-z0-9])"
)
RP_CONTRACT_DECLARATION_RE = re.compile(
    r'"rp_contract"\s*:\s*\{\s*"schema_version"\s*:\s*"rp-core\.v2"\s*,'
    r'\s*"revision"\s*:\s*([0-9]+)'
)


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def validate_json(errors: list[str]) -> None:
    for path in ROOT.rglob("*.json"):
        if any(part in IGNORED_PARTS for part in path.parts):
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - aggregate every invalid file
            fail(errors, f"invalid JSON: {path.relative_to(ROOT)}: {exc}")


def validate_worldpack_lore_cards(errors: list[str]) -> None:
    worldpacks_root = ROOT / "roles" / "apps" / "files" / "rp-stack" / "worldpacks"
    for manifest_path in sorted(worldpacks_root.glob("*/manifest.json")):
        pack_root = manifest_path.parent.resolve()
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        files = manifest.get("files") if isinstance(manifest, dict) else None
        relative_dir = files.get("lore_cards") if isinstance(files, dict) else None
        conventional_dir = pack_root / "lore-cards"
        if relative_dir is None:
            if conventional_dir.exists():
                fail(errors, f"undeclared WorldPack lore-cards directory: {manifest_path.parent.relative_to(ROOT)}")
            continue
        if not isinstance(relative_dir, str) or not relative_dir.strip():
            fail(errors, f"invalid WorldPack lore_cards path: {manifest_path.relative_to(ROOT)}")
            continue
        cards_dir = (pack_root / relative_dir).resolve()
        if pack_root not in cards_dir.parents or not cards_dir.is_dir():
            fail(errors, f"missing or unsafe WorldPack lore_cards directory: {manifest_path.relative_to(ROOT)}")
            continue
        card_paths = sorted(cards_dir.glob("*.json"))
        if not card_paths:
            fail(errors, f"WorldPack lore_cards directory contains no JSON: {cards_dir.relative_to(ROOT)}")
            continue

        cards_by_key: dict[str, dict[str, object]] = {}
        for card_path in card_paths:
            try:
                payload = json.loads(card_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("schema_version") != "rp-gateway.worldpack-lore-cards.v1":
                fail(errors, f"invalid WorldPack lore-card schema: {card_path.relative_to(ROOT)}")
                continue
            raw_cards = payload.get("cards")
            if not isinstance(raw_cards, list) or not raw_cards:
                fail(errors, f"WorldPack lore-card file has no cards: {card_path.relative_to(ROOT)}")
                continue
            for index, card in enumerate(raw_cards, start=1):
                label = f"{card_path.relative_to(ROOT)}:{index}"
                if not isinstance(card, dict):
                    fail(errors, f"WorldPack lore card is not an object: {label}")
                    continue
                key = str(card.get("key") or "").strip()
                if not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,127}", key):
                    fail(errors, f"WorldPack lore card has invalid key: {label}")
                    continue
                if key in cards_by_key:
                    fail(errors, f"duplicate WorldPack lore card key: {key}")
                    continue
                title = str(card.get("title") or "").strip()
                content = str(card.get("content") or "").strip()
                raw_keywords = card.get("keywords")
                keywords = (
                    [str(keyword).strip() for keyword in raw_keywords if str(keyword).strip()]
                    if isinstance(raw_keywords, list)
                    else []
                )
                if not title or len(title) > 160 or not content or len(content) > 12_000:
                    fail(errors, f"WorldPack lore card has invalid title/content: {label}")
                if not keywords or len(keywords) > 40:
                    fail(errors, f"WorldPack lore card keywords must be non-empty: {label}")
                if not isinstance(card.get("always_on", False), bool) or not isinstance(card.get("enabled", True), bool):
                    fail(errors, f"WorldPack lore card has invalid flags: {label}")
                if key.startswith("npc:") and card.get("always_on", False) is not False:
                    fail(errors, f"WorldPack NPC lore card must use always_on=false: {label}")
                cards_by_key[key] = card

        pack_id = str(manifest.get("id") or manifest_path.parent.name)
        if pack_id == "merchant-sviatoslav" and len(cards_by_key) < 15:
            fail(errors, "merchant-sviatoslav must contain at least 15 authored Lore Cards")

        relationship_decl = manifest.get("relationships") if isinstance(manifest, dict) else None
        relationship_path = relationship_decl.get("model") if isinstance(relationship_decl, dict) else None
        if not isinstance(relationship_path, str) or not relationship_path.strip():
            continue
        try:
            relationship_model = json.loads((pack_root / relationship_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        characters = relationship_model.get("characters") if isinstance(relationship_model, dict) else None
        if not isinstance(characters, dict):
            continue
        for character_id, declaration in characters.items():
            key = f"npc:{character_id}"
            card = cards_by_key.get(key)
            if card is None:
                fail(errors, f"WorldPack lore cards missing NPC card: {pack_id}:{key}")
                continue
            aliases = declaration.get("aliases") if isinstance(declaration, dict) else None
            expected_aliases = {
                str(alias).strip().casefold()
                for alias in aliases or []
                if str(alias).strip()
            }
            keywords = {
                str(keyword).strip().casefold()
                for keyword in card.get("keywords", [])
                if str(keyword).strip()
            }
            if not expected_aliases or not expected_aliases.issubset(keywords):
                fail(errors, f"WorldPack NPC lore card lacks declared aliases: {pack_id}:{key}")
            canonical_name = str(next(iter(aliases), "")).strip() if isinstance(aliases, list) else ""
            if canonical_name and str(card.get("title") or "").strip() != canonical_name:
                fail(errors, f"WorldPack NPC lore card title is not the canonical name: {pack_id}:{key}")


def validate_world_clocks(errors: list[str]) -> None:
    worldpacks_root = ROOT / "roles" / "apps" / "files" / "rp-stack" / "worldpacks"
    stable_id = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}")
    duration = re.compile(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?"
    )

    def duration_seconds(value: object) -> int | None:
        match = duration.fullmatch(str(value or ""))
        if (
            match is None
            or not any(match.groupdict().values())
            or (
                "T" in str(value or "")
                and not any(match.group(name) for name in ("hours", "minutes", "seconds"))
            )
        ):
            return None
        days = int(match.group("days") or 0)
        hours = int(match.group("hours") or 0)
        minutes = int(match.group("minutes") or 0)
        seconds = int(match.group("seconds") or 0)
        if hours >= 24 or minutes >= 60 or seconds >= 60:
            return None
        return (((days * 24) + hours) * 60 + minutes) * 60 + seconds

    def valid_date(value: object) -> bool:
        text = str(value or "")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None

    for manifest_path in sorted(worldpacks_root.glob("*/manifest.json")):
        pack_root = manifest_path.parent.resolve()
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        files = manifest.get("files") if isinstance(manifest, dict) else None
        relative_path = files.get("world_clock") if isinstance(files, dict) else None
        conventional_path = pack_root / "world-clock.json"
        rp_contract = manifest.get("rp_contract") if isinstance(manifest, dict) else None
        raw_revision = rp_contract.get("revision") if isinstance(rp_contract, dict) else 0
        revision = int(raw_revision) if isinstance(raw_revision, int) else 0
        pack_id = str(manifest.get("id") or manifest_path.parent.name)
        if relative_path is None:
            if conventional_path.exists():
                fail(errors, f"undeclared WorldPack world-clock.json: {manifest_path.parent.relative_to(ROOT)}")
            if revision >= 10:
                fail(errors, f"revision-10 WorldPack must declare world_clock: {manifest_path.relative_to(ROOT)}")
            continue
        label = str(manifest_path.relative_to(ROOT))
        if not isinstance(relative_path, str) or not relative_path.strip():
            fail(errors, f"invalid WorldPack world_clock path: {label}")
            continue
        clock_path = (pack_root / relative_path).resolve()
        if (clock_path != pack_root and pack_root not in clock_path.parents) or not clock_path.is_file():
            fail(errors, f"missing or unsafe WorldPack world_clock file: {label}")
            continue
        if revision < 10:
            fail(errors, f"WorldPack world_clock requires rp_contract revision >= 10: {label}")
        if pack_id != "merchant-sviatoslav":
            fail(errors, f"only merchant-sviatoslav may declare candidate revision 10: {label}")
        try:
            clock = json.loads(clock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        required_clock_keys = {"schema_version", "initial_date", "step_unit", "max_step", "markers", "events"}
        if not isinstance(clock, dict) or set(clock) != required_clock_keys:
            fail(errors, f"invalid WorldPack world clock envelope: {clock_path.relative_to(ROOT)}")
            continue
        max_step_seconds = duration_seconds(clock.get("max_step"))
        if (
            clock.get("schema_version") != "rp-gateway.world-clock.v1"
            or clock.get("step_unit") != "iso8601_duration"
            or not valid_date(clock.get("initial_date"))
            or max_step_seconds is None
            or not 0 < max_step_seconds <= 31 * 24 * 60 * 60
        ):
            fail(errors, f"invalid WorldPack world clock header: {clock_path.relative_to(ROOT)}")

        raw_markers = clock.get("markers")
        raw_events = clock.get("events")
        if not isinstance(raw_markers, list) or len(raw_markers) > 64:
            fail(errors, f"invalid WorldPack world clock markers: {clock_path.relative_to(ROOT)}")
            raw_markers = []
        if not isinstance(raw_events, list) or not raw_events or len(raw_events) > 128:
            fail(errors, f"invalid WorldPack world clock events: {clock_path.relative_to(ROOT)}")
            raw_events = []
        marker_ids: set[str] = set()
        for marker in raw_markers:
            if not isinstance(marker, dict) or not {"id", "label"}.issubset(marker) or set(marker) - {"id", "label", "predicate"}:
                fail(errors, f"invalid WorldPack world clock marker: {clock_path.relative_to(ROOT)}")
                continue
            marker_id = str(marker.get("id") or "")
            marker_label = str(marker.get("label") or "").strip()
            if (
                not stable_id.fullmatch(marker_id)
                or marker_id in marker_ids
                or not marker_label
                or len(marker_label) > 160
            ):
                fail(errors, f"invalid or duplicate WorldPack world clock marker id: {marker_id}")
                continue
            marker_ids.add(marker_id)
            predicate = marker.get("predicate")
            if predicate is not None and (
                not isinstance(predicate, dict)
                or set(predicate) != {"type", "path", "value"}
                or predicate.get("type") != "state_equals"
                or not str(predicate.get("path") or "").startswith(
                    (
                        "/player/resources/",
                        "/characters/",
                        "/factions/",
                        "/resources/",
                        "/active_threads/",
                        "/completed_threads/",
                        "/world_constraints/",
                    )
                )
                or isinstance(predicate.get("value"), (dict, list))
            ):
                fail(errors, f"invalid WorldPack world clock marker predicate: {marker_id}")

        lore_card_keys: set[str] = set()
        lore_dir = files.get("lore_cards") if isinstance(files, dict) else None
        if isinstance(lore_dir, str):
            for card_path in sorted((pack_root / lore_dir).glob("*.json")):
                try:
                    card_payload = json.loads(card_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                for card in card_payload.get("cards", []) if isinstance(card_payload, dict) else []:
                    if isinstance(card, dict) and stable_id.fullmatch(str(card.get("key") or "")):
                        lore_card_keys.add(str(card["key"]))

        event_ids: set[str] = set()
        world_fact_ids: set[str] = set()
        dependencies: dict[str, str] = {}
        total_fact_chars = 0
        for event in raw_events:
            required_event_keys = {"id", "condition", "summary", "superseded_by", "consequences"}
            if not isinstance(event, dict) or set(event) != required_event_keys:
                fail(errors, f"invalid WorldPack world clock event shape: {clock_path.relative_to(ROOT)}")
                continue
            event_id = str(event.get("id") or "")
            if not stable_id.fullmatch(event_id) or event_id in event_ids:
                fail(errors, f"invalid or duplicate WorldPack world clock event id: {event_id}")
                continue
            event_ids.add(event_id)
            summary = str(event.get("summary") or "").strip()
            if not summary or len(summary) > 240:
                fail(errors, f"invalid WorldPack world clock event summary: {event_id}")
            superseded_by = event.get("superseded_by")
            if (
                not isinstance(superseded_by, list)
                or not superseded_by
                or len(superseded_by) > 8
                or len(set(superseded_by)) != len(superseded_by)
                or any(marker_id not in marker_ids for marker_id in superseded_by)
            ):
                fail(errors, f"WorldPack world clock event lacks a valid supersession path: {event_id}")
            condition = event.get("condition")
            condition_type = condition.get("type") if isinstance(condition, dict) else None
            if condition_type == "date_gte":
                if set(condition) != {"type", "date"} or not valid_date(condition.get("date")):
                    fail(errors, f"invalid WorldPack date_gte condition: {event_id}")
            elif condition_type == "after_event":
                if set(condition) != {"type", "event_id"}:
                    fail(errors, f"invalid WorldPack after_event condition: {event_id}")
                else:
                    dependencies[event_id] = str(condition.get("event_id") or "")
            elif condition_type == "after_confirmed":
                if set(condition) != {"type", "marker_id"} or condition.get("marker_id") not in marker_ids:
                    fail(errors, f"invalid WorldPack after_confirmed condition: {event_id}")
            else:
                fail(errors, f"unsupported WorldPack world clock condition: {event_id}")
            consequences = event.get("consequences")
            if not isinstance(consequences, list) or not consequences or len(consequences) > 8:
                fail(errors, f"invalid WorldPack world clock consequences: {event_id}")
                continue
            for consequence in consequences:
                consequence_type = consequence.get("type") if isinstance(consequence, dict) else None
                if consequence_type == "world_fact":
                    if set(consequence) != {"type", "id", "text"}:
                        fail(errors, f"invalid WorldPack world_fact consequence: {event_id}")
                        continue
                    fact_id = str(consequence.get("id") or "")
                    text = str(consequence.get("text") or "").strip()
                    if (
                        not stable_id.fullmatch(fact_id)
                        or fact_id in world_fact_ids
                        or not text
                        or len(text) > 180
                    ):
                        fail(errors, f"invalid WorldPack world_fact consequence: {event_id}")
                    world_fact_ids.add(fact_id)
                    total_fact_chars += len(text)
                elif consequence_type == "lore_card":
                    if (
                        set(consequence) != {"type", "key", "enabled"}
                        or consequence.get("key") not in lore_card_keys
                        or not isinstance(consequence.get("enabled"), bool)
                    ):
                        fail(errors, f"invalid WorldPack lore_card consequence: {event_id}")
                else:
                    fail(errors, f"unsupported WorldPack world clock consequence: {event_id}")
        if total_fact_chars > 400:
            fail(errors, f"WorldPack world clock facts exceed 400 characters: {clock_path.relative_to(ROOT)}")
        for event_id, dependency in dependencies.items():
            if dependency not in event_ids or dependency == event_id:
                fail(errors, f"invalid WorldPack after_event reference: {event_id} -> {dependency}")
        for event_id in dependencies:
            seen: set[str] = set()
            current = event_id
            while current in dependencies:
                if current in seen:
                    fail(errors, f"WorldPack world clock after_event cycle: {event_id}")
                    break
                seen.add(current)
                current = dependencies[current]

        if pack_id == "merchant-sviatoslav":
            if len(event_ids) < 4:
                fail(errors, "merchant-sviatoslav world clock must contain at least four events")
            if not any("vyatichi" in event_id or "вятич" in str(event.get("summary") or "").casefold() for event_id, event in ((str(item.get("id") or ""), item) for item in raw_events if isinstance(item, dict))):
                fail(errors, "merchant-sviatoslav world clock must include the Vyatichi campaign")


def validate_wiki(errors: list[str]) -> None:
    wiki_root = ROOT / "docs" / "wiki"
    readme = wiki_root / "README.md"
    if not readme.is_file():
        fail(errors, "missing docs/wiki/README.md")
        return

    for path in wiki_root.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        fence_count = sum(1 for line in text.splitlines() if line.lstrip().startswith("```"))
        if fence_count % 2:
            fail(errors, f"unbalanced Markdown fence: {path.relative_to(ROOT)}")

        for match in LINK_RE.finditer(text):
            raw_target = match.group(1).strip().split(maxsplit=1)[0].strip("<>")
            if not raw_target or raw_target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target_path = raw_target.split("#", 1)[0]
            if not target_path:
                continue
            resolved = (path.parent / target_path).resolve()
            try:
                resolved.relative_to(ROOT.resolve())
            except ValueError:
                fail(errors, f"Wiki link escapes repository: {path.relative_to(ROOT)} -> {raw_target}")
                continue
            if not resolved.exists():
                fail(errors, f"broken Wiki link: {path.relative_to(ROOT)} -> {raw_target}")


def validate_agents(errors: list[str]) -> None:
    required = [
        ROOT / "AGENTS.md",
        ROOT / "roles" / "apps" / "files" / "rp-stack" / "AGENTS.md",
        ROOT / "roles" / "apps" / "files" / "rp-stack" / "rp-gateway" / "AGENTS.md",
        ROOT / "roles" / "apps" / "files" / "rp-stack" / "worldpacks" / "AGENTS.md",
        ROOT / ".codex" / "config.toml",
        ROOT / ".codex" / "hooks.json",
        ROOT / "docs" / "repository-work-standard.md",
        ROOT / "scripts" / "sync-codex-skills.ps1",
    ]
    for path in required:
        if not path.is_file():
            fail(errors, f"missing project policy file: {path.relative_to(ROOT)}")

    merge_policy_files = [
        ROOT / "AGENTS.md",
        ROOT / "codex-skills" / "abykovserv-iac-deploy" / "SKILL.md",
        ROOT / "codex-skills" / "abykovserv-iac-deploy" / "references" / "deployment-map.md",
        ROOT / "codex-skills" / "rp-world-pack-builder" / "SKILL.md",
        ROOT / "codex-skills" / "rp-world-pack-builder" / "references" / "world-pack-contract.md",
        ROOT / "codex-skills" / "training-world-pack-builder" / "SKILL.md",
        ROOT / "docs" / "repository-work-standard.md",
        ROOT / "docs" / "server-setup-notes.md",
        ROOT / "docs" / "use-framework.md",
        ROOT / "docs" / "wiki" / "09-operations-and-repository.md",
        ROOT / "docs" / "wiki" / "README.md",
        ROOT / "plugins" / "rp-stack-devkit" / "skills" / "rp-stack-devkit" / "SKILL.md",
        ROOT / "roles" / "apps" / "files" / "rp-stack" / "README.md",
        ROOT / "roles" / "apps" / "files" / "rp-stack" / "docs" / "decisions" / "018-separate-training-and-rp-gateways.md",
        ROOT / "roles" / "apps" / "files" / "rp-stack" / "docs" / "decisions" / "019-retire-legacy-awareness-gateway-path.md",
        ROOT / "roles" / "apps" / "files" / "rp-stack" / "docs" / "decisions" / "020-rp-relationship-pressure-layer.md",
        ROOT / "roles" / "apps" / "files" / "rp-stack" / "docs" / "plans" / "011-local-gemma4-vulkan.md",
        ROOT / "roles" / "apps" / "files" / "rp-stack" / "docs" / "plans" / "014-interactive-training-site-artifacts.md",
        ROOT / "roles" / "apps" / "files" / "rp-stack" / "docs" / "plans" / "015-training-scenario-interaction-capabilities.md",
    ]
    merge_policy_markers = {
        "non-draft pull request": re.compile(r"(?i)non-draft (?:pull request|PR)"),
        "green CI": re.compile(r"(?i)(?:green|зел[её]н\w*)[- ]CI|CI is green"),
        "merge": re.compile(r"(?i)(?:merge|мерж)"),
        "main": re.compile(r"(?i)\bmain\b"),
    }
    for path in merge_policy_files:
        if not path.is_file():
            fail(errors, f"missing merge-policy file: {path.relative_to(ROOT)}")
            continue
        text = path.read_text(encoding="utf-8")
        missing = [name for name, pattern in merge_policy_markers.items() if not pattern.search(text)]
        if missing:
            fail(
                errors,
                f"merge policy missing from {path.relative_to(ROOT)}: {', '.join(missing)}",
            )

    direct_main_push = re.compile(
        r"(?i)(?:git push origin main|push(?: changes)? (?:to|in|в) "
        r"`?(?:GitHub )?(?:origin/)?main`?|push origin/main|commit \+ push.{0,80}GitHub main)"
    )
    prohibition = re.compile(r"(?i)(?:do not|must not|never|prohibit|запрещ)")
    stale_pushed_stop = re.compile(
        r"(?i)(?:stop(?:s|ped)? (?:after|at) .{0,20}pushed|"
        r"automation stops at .{0,20}pushed|останавливается.{0,40}pushed)"
    )
    result = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        fail(errors, "cannot enumerate Markdown files for merge-policy validation")
        return
    for relative_text in result.stdout.splitlines():
        path = ROOT / relative_text
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if direct_main_push.search(line) and not prohibition.search(line):
                fail(errors, f"direct push to main instruction: {relative_text}:{line_number}")
            if stale_pushed_stop.search(line):
                fail(errors, f"sudo boundary stops at pushed instead of merged: {relative_text}:{line_number}")


def validate_plugin(errors: list[str]) -> None:
    plugin_root = ROOT / "plugins" / "rp-stack-devkit"
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    marketplace_path = ROOT / ".agents" / "plugins" / "marketplace.json"
    required_files = [
        manifest_path,
        plugin_root / ".mcp.json",
        plugin_root / "skills" / "rp-stack-devkit" / "SKILL.md",
        plugin_root / "scripts" / "mcp-server.ps1",
        plugin_root / "scripts" / "rp-stack-ops.ps1",
        plugin_root / "hooks" / "hooks.json",
        marketplace_path,
    ]
    for path in required_files:
        if not path.is_file():
            fail(errors, f"missing plugin file: {path.relative_to(ROOT)}")
    if errors and not manifest_path.is_file():
        return

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for field in ("name", "version", "description", "author", "interface"):
        if field not in manifest:
            fail(errors, f"plugin manifest missing field: {field}")
    if manifest.get("name") != "rp-stack-devkit":
        fail(errors, "plugin manifest name must be rp-stack-devkit")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", str(manifest.get("version", ""))):
        fail(errors, "plugin version must be strict semver")
    if manifest.get("mcpServers") != "./.mcp.json":
        fail(errors, "plugin must declare ./.mcp.json")

    if marketplace_path.is_file():
        marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
        entry = next(
            (item for item in marketplace.get("plugins", []) if item.get("name") == "rp-stack-devkit"),
            None,
        )
        if entry is None:
            fail(errors, "marketplace does not list rp-stack-devkit")
        else:
            policy = entry.get("policy", {})
            if not policy.get("installation") or not policy.get("authentication"):
                fail(errors, "marketplace plugin policy is incomplete")
            if entry.get("source", {}).get("path") != "./plugins/rp-stack-devkit":
                fail(errors, "marketplace plugin source path is not canonical")


def tracked_files(errors: list[str]) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        fail(errors, "cannot enumerate tracked files with git ls-files")
        return []
    return [ROOT / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def markdown_command_fragments(text: str) -> list[str]:
    fragments: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            fragments.append(line)
        fragments.extend(re.findall(r"`([^`]+)`", line))
    return fragments


def validate_environment_contracts(errors: list[str]) -> None:
    inventory = ROOT / "inventories" / "local" / "group_vars" / "server.yml"
    production_env_template = ROOT / "roles" / "apps" / "templates" / "rp-stack.env.j2"
    env_templates = (
        production_env_template,
        ROOT / "roles" / "apps" / "templates" / "rp-stack.env.example.j2",
    )
    retention_variable = "rp_stack_gateway_service_call_log_retention_days: 0"
    retention_mapping = (
        "SERVICE_CALL_LOG_RETENTION_DAYS="
        "{{ rp_stack_gateway_service_call_log_retention_days | default(0) }}"
    )
    if (
        not inventory.is_file()
        or retention_variable not in inventory.read_text(encoding="utf-8")
    ):
        fail(errors, "RP Stack inventory must default service-call log retention to unlimited (0)")
    for path in env_templates:
        if not path.is_file() or retention_mapping not in path.read_text(encoding="utf-8"):
            fail(
                errors,
                f"RP Stack env template does not render service-call log retention: {path.relative_to(ROOT)}",
            )

    observed_revision_assignments = (
        re.findall(
            r"(?m)^rp_stack_gateway_rp_contract_observed_revision:\s*([0-9]+)\s*(?:#.*)?$",
            inventory.read_text(encoding="utf-8"),
        )
        if inventory.is_file()
        else []
    )
    observed_revision_mapping = (
        "RP_CONTRACT_OBSERVED_REVISION="
        "{{ rp_stack_gateway_rp_contract_observed_revision }}"
    )
    if observed_revision_assignments != ["8"]:
        fail(errors, "RP Stack inventory must set rp-core.v2 revision 8 observed exactly once")
    if (
        not production_env_template.is_file()
        or observed_revision_mapping not in production_env_template.read_text(encoding="utf-8")
    ):
        fail(errors, "RP Stack production env must render the explicit observed RP revision")

    canary_wrapper = ROOT / "scripts" / "run-rp-stack-evals.ps1"
    canary_markers = (
        "[ValidateRange(0, 10)]",
        "[Nullable[int]]$RpContractRevision = $null",
        "if ($null -ne $RpContractRevision)",
        '$arguments += @("--rp-contract-revision", [string]$RpContractRevision)',
    )
    if not canary_wrapper.is_file():
        fail(errors, "missing RP Stack eval wrapper")
    else:
        canary_source = canary_wrapper.read_text(encoding="utf-8-sig")
        if any(marker not in canary_source for marker in canary_markers):
            fail(errors, "RP Stack provider canary must forward explicit candidate revision 0..10")

    canary_runner = ROOT / "roles" / "apps" / "files" / "rp-stack" / "evals" / "run_evals.py"
    if not canary_runner.is_file():
        fail(errors, "missing RP Stack eval runner")
    elif 'choices=range(0, 11)' not in canary_runner.read_text(encoding="utf-8"):
        fail(errors, "RP Stack provider canary evaluator must accept candidate revision 0..10")

    marketplace = Path(".agents/plugins/marketplace.json")
    old_profile = b"C:" + b"\\Users\\" + b"albykov"
    old_plugin_path = b".agents/plugins/" + b"rp-stack-devkit/"
    tracked = tracked_files(errors)
    for path in tracked:
        if not path.is_file():
            continue
        data = path.read_bytes()
        relative = path.relative_to(ROOT)
        if old_profile in data:
            fail(errors, f"tracked file contains obsolete profile path: {relative}")
        if relative != marketplace and old_plugin_path in data:
            fail(errors, f"tracked file contains obsolete devkit path: {relative}")

    project_policy = ROOT / ".codex" / "hooks" / "rp_stack_policy.ps1"
    plugin_policy = ROOT / "plugins" / "rp-stack-devkit" / "hooks" / "rp_stack_policy.ps1"
    if project_policy.is_file() and plugin_policy.is_file() and project_policy.read_bytes() != plugin_policy.read_bytes():
        fail(errors, "project and plugin rp_stack_policy.ps1 copies differ")

    for base in (ROOT / "codex-skills", ROOT / "plugins"):
        for path in base.rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            for fragment in markdown_command_fragments(text):
                if SSH_COMMAND_RE.search(fragment) and "-i" not in fragment and "keyless-example" not in fragment:
                    fail(errors, f"SSH command lacks explicit -i: {path.relative_to(ROOT)}: {fragment.strip()}")

    ignore_path = ROOT / ".graphifyignore"
    required_ignores = {".tools/", "tmp/", "codex-worktrees/", "graphify-out/"}
    if not ignore_path.is_file():
        fail(errors, "missing .graphifyignore")
    else:
        actual_ignores = {
            line.strip().replace("\\", "/")
            for line in ignore_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        for required in sorted(required_ignores - actual_ignores):
            fail(errors, f".graphifyignore missing required entry: {required}")


def validate_rp_world_pack_builder_contract(errors: list[str]) -> None:
    inventory = ROOT / "inventories" / "local" / "group_vars" / "server.yml"
    if not inventory.is_file():
        return
    observed_revisions = re.findall(
        r"(?m)^rp_stack_gateway_rp_contract_observed_revision:\s*([0-9]+)\s*(?:#.*)?$",
        inventory.read_text(encoding="utf-8"),
    )
    if len(observed_revisions) != 1:
        return
    expected_revision = int(observed_revisions[0])
    contract_files = (
        ROOT / "codex-skills" / "rp-world-pack-builder" / "SKILL.md",
        ROOT / "codex-skills" / "rp-world-pack-builder" / "references" / "world-pack-contract.md",
    )
    combined = ""
    for path in contract_files:
        if not path.is_file():
            fail(errors, f"missing RP WorldPack builder contract: {path.relative_to(ROOT)}")
            continue
        text = path.read_text(encoding="utf-8")
        combined += "\n" + text
        declared_revisions = [
            int(value) for value in RP_CONTRACT_DECLARATION_RE.findall(text)
        ]
        if not declared_revisions or any(
            revision != expected_revision for revision in declared_revisions
        ):
            fail(
                errors,
                "RP WorldPack builder contract revision does not match observed "
                f"revision {expected_revision}: {path.relative_to(ROOT)}",
            )

    required_markers = (
        "PROMPT_AUTHORITY_HIERARCHY",
        "stable_affiliations",
        "scene_claims",
        "scene_delta",
        "story_memory_canonical=false",
        "rp-gateway.worldpack-lore-cards.v1",
        "rp-gateway.world-clock.v1",
    )
    for marker in required_markers:
        if marker not in combined:
            fail(errors, f"RP WorldPack builder contract missing compatibility marker: {marker}")
    if not re.search(r"(?i)force[- ]refresh", combined):
        fail(errors, "RP WorldPack builder contract missing revision-7 compatibility force-refresh rule")


def validate_adr_registry(errors: list[str]) -> None:
    validator = ROOT / "scripts" / "validate-adr-registry.py"
    if not validator.is_file():
        fail(errors, "missing scripts/validate-adr-registry.py")
        return
    result = subprocess.run(
        [sys.executable, str(validator), "--root", str(ROOT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip()
        fail(errors, f"ADR registry validation failed: {detail}")


def main() -> int:
    errors: list[str] = []
    validate_json(errors)
    validate_worldpack_lore_cards(errors)
    validate_world_clocks(errors)
    validate_wiki(errors)
    validate_agents(errors)
    validate_plugin(errors)
    validate_environment_contracts(errors)
    validate_rp_world_pack_builder_contract(errors)
    validate_adr_registry(errors)
    if errors:
        print("Repository validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Repository contracts valid: JSON, WorldPack Lore Cards/world clocks, Wiki, AGENTS, plugin, environment, RP builder, SSH, policy, and Graphify guards.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
