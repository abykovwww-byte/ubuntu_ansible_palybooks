#!/usr/bin/env python3
"""Validate Decision 022 readiness registries and the frozen acceptance oracle.

The validator intentionally uses only the Python standard library.  The YAML
files covered here use a deliberately small, frozen subset of YAML; accepting
more syntax would make this repository policy depend on an optional parser.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_ROOT = Path(__file__).resolve().parents[1]
DECISIONS_REL = Path("roles/apps/files/rp-stack/docs/decisions")
REGISTRY_REL = DECISIONS_REL / "registry"
ACCEPTANCE_REL = Path("roles/apps/files/rp-stack/evals/acceptance")
MANIFEST_REL = ACCEPTANCE_REL / "manifest.yml"

READINESS_LEVELS = ("каркас", "подключено", "наблюдается", "держится")
ASSERTION_KINDS = ("state_change", "prompt_presence", "absence", "projection")
REQUIRED_REGISTRY_FIELDS = (
    "requirement",
    "level",
    "store",
    "assertion",
    "expectation",
    "probe_command",
    "expected",
    "waiver",
)
REQUIRED_THRESHOLDS = (
    "event_precision",
    "event_recall",
    "character_id_accuracy",
    "empty_scene_false_positive_rate",
    "positive_trust_recall",
    "correction_retention",
)

# These are policy surfaces, not every prose document in the repository.  ADRs
# themselves contain examples of forbidden wording and therefore are excluded.
POLICY_SURFACE_GLOBS = (
    "AGENTS.md",
    "roles/apps/files/rp-stack/AGENTS.md",
    "roles/apps/files/rp-stack/rp-gateway/AGENTS.md",
    "roles/apps/files/rp-stack/worldpacks/AGENTS.md",
    "roles/apps/files/rp-stack/docs/wiki/*.md",
    "roles/apps/files/rp-stack/docs/decisions/registry/*.yml",
    "plugins/rp-stack-devkit/README.md",
    "plugins/rp-stack-devkit/skills/rp-stack-devkit/SKILL.md",
    "codex-skills/abykovserv-iac-deploy/SKILL.md",
    "codex-skills/rp-stack-wiki/SKILL.md",
)

MECHANISM_PREFIXES = (
    "roles/apps/files/rp-stack/rp-gateway/app/",
    "roles/apps/files/rp-stack/worldpacks/",
)
ORACLE_PREFIX = f"{ACCEPTANCE_REL.as_posix()}/corpus/"
ORACLE_FILES = {MANIFEST_REL.as_posix()}

BARE_READINESS_RE = re.compile(
    r"(?iu)(?:implementation\s+readiness|готовност(?:ь|и)|статус\s+готовности)"
    r"\s*(?::|—|-)\s*(?:реализовано|работает|готово|сделано)\b"
)
PARTY_ID_RE = re.compile(r"\bparty_[0-9a-f]{10,}\b", re.IGNORECASE)
EXPECTED_PROBE_RE = re.compile(
    r"^(?:chain passes through (?:state_change|prompt_presence|absence|projection)|"
    r"chain fails at (?:state_change|prompt_presence|absence|projection)|"
    r"chain reaches scene_considers for [1-9][0-9]* turns)$"
)


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    path: str | None = None
    warning: bool = False

    def render(self) -> str:
        severity = "WARN" if self.warning else "ERROR"
        location = f" {self.path}:" if self.path else ""
        return f"{severity}[{self.code}]{location} {self.message}"


def _strip_comment(value: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if char in {'"', "'"}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            continue
        if char == "#" and quote is None:
            return value[:index].rstrip()
    return value.rstrip()


def _parse_scalar(raw: str) -> object:
    value = _strip_comment(raw).strip()
    if not value:
        raise ValueError("missing scalar value")
    if value in {"null", "~"}:
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    if value[0:1] in {'"', "'"}:
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise ValueError(f"invalid quoted scalar: {value}") from exc
    if re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    if re.fullmatch(r"-?(?:[0-9]+\.[0-9]*|[0-9]*\.[0-9]+)", value):
        return float(value)
    return value


def _split_key_value(text: str) -> tuple[str, object]:
    if ":" not in text:
        raise ValueError("expected key: value")
    key, raw_value = text.split(":", 1)
    key = key.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise ValueError(f"invalid key: {key!r}")
    return key, _parse_scalar(raw_value)


def parse_registry(path: Path) -> tuple[list[dict[str, object]], list[Diagnostic]]:
    diagnostics: list[Diagnostic] = []
    entries: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    relative = path.as_posix()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        try:
            if raw_line.startswith("- "):
                if current is not None:
                    entries.append(current)
                current = {}
                key, value = _split_key_value(raw_line[2:])
            elif raw_line.startswith("  ") and current is not None:
                key, value = _split_key_value(raw_line.strip())
            else:
                raise ValueError("registry must be a top-level YAML sequence")
            if key in current:
                raise ValueError(f"duplicate key: {key}")
            current[key] = value
        except ValueError as exc:
            diagnostics.append(
                Diagnostic("REGISTRY_YAML_INVALID", str(exc), f"{relative}:{line_number}")
            )
    if current is not None:
        entries.append(current)
    if not entries:
        diagnostics.append(Diagnostic("REGISTRY_EMPTY", "registry has no requirements", relative))
    return entries, diagnostics


def _decision_status(root: Path, number: str) -> tuple[bool | None, list[Diagnostic]]:
    matches = sorted((root / DECISIONS_REL).glob(f"{number}-*.md"))
    if not matches:
        return None, [
            Diagnostic(
                "ADR_SOURCE_MISSING",
                "registry exists but the corresponding Decision source is not tracked; status is not inferred",
                f"{REGISTRY_REL.as_posix()}/{number}.yml",
                warning=True,
            )
        ]
    if len(matches) > 1:
        return None, [
            Diagnostic(
                "ADR_SOURCE_AMBIGUOUS",
                "multiple Decision source files match this registry",
                f"{REGISTRY_REL.as_posix()}/{number}.yml",
            )
        ]
    text = matches[0].read_text(encoding="utf-8")
    match = re.search(r"(?ms)^## Status\s*$\n(.*?)(?=^## |\Z)", text)
    if not match:
        return False, [
            Diagnostic(
                "ADR_STATUS_MISSING",
                "Decision source has no Status section",
                matches[0].relative_to(root).as_posix(),
            )
        ]
    return bool(re.search(r"(?i)\bAccepted\b", match.group(1))), []


def validate_registry_entry(
    entry: dict[str, object], *, accepted: bool | None, path: str, index: int
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    label = f"{path}#{index}"
    missing = [field for field in REQUIRED_REGISTRY_FIELDS if field not in entry]
    extra = sorted(set(entry) - set(REQUIRED_REGISTRY_FIELDS))
    if missing:
        diagnostics.append(
            Diagnostic("REGISTRY_FIELD_MISSING", f"missing fields: {', '.join(missing)}", label)
        )
    if extra:
        diagnostics.append(
            Diagnostic("REGISTRY_FIELD_UNKNOWN", f"unknown fields: {', '.join(extra)}", label)
        )
    if missing:
        return diagnostics

    for field in ("requirement", "store", "expectation"):
        if not isinstance(entry[field], str) or not str(entry[field]).strip():
            diagnostics.append(
                Diagnostic("REGISTRY_VALUE_EMPTY", f"{field} must be a non-empty string", label)
            )

    level = entry["level"]
    if level not in READINESS_LEVELS:
        diagnostics.append(
            Diagnostic(
                "READINESS_LEVEL_INVALID",
                f"level must be exactly one of: {' | '.join(READINESS_LEVELS)}",
                label,
            )
        )
        return diagnostics
    assertion = entry["assertion"]
    if assertion not in ASSERTION_KINDS:
        diagnostics.append(
            Diagnostic(
                "ASSERTION_KIND_INVALID",
                f"assertion must be exactly one of: {' | '.join(ASSERTION_KINDS)}",
                label,
            )
        )

    waiver = entry["waiver"]
    if waiver is not None and (not isinstance(waiver, str) or not waiver.strip()):
        diagnostics.append(Diagnostic("WAIVER_INVALID", "waiver must be null or non-empty text", label))
    if accepted is True and level == "каркас" and waiver is None:
        diagnostics.append(
            Diagnostic(
                "ADR_ACCEPTED_SCAFFOLD_WITHOUT_WAIVER",
                "Accepted requirement at level каркас requires an explicit waiver",
                label,
            )
        )

    probe = entry["probe_command"]
    expected = entry["expected"]
    if probe is not None and (not isinstance(probe, str) or not probe.strip()):
        diagnostics.append(
            Diagnostic("PROBE_COMMAND_INVALID", "probe_command must be null or non-empty text", label)
        )
    if expected is not None and (not isinstance(expected, str) or not expected.strip()):
        diagnostics.append(
            Diagnostic("PROBE_EXPECTED_INVALID", "expected must be null or non-empty text", label)
        )

    expectation = entry["expectation"]
    real_probe = (
        isinstance(probe, str)
        and re.search(r"(?:^|\s)causal_probe(?:\s|$)", probe) is not None
        and isinstance(expectation, str)
        and re.search(rf"--expectation(?:\s+|=){re.escape(expectation)}(?:\s|$)", probe)
        is not None
    )
    if probe is not None and not real_probe:
        diagnostics.append(
            Diagnostic(
                "CAUSAL_PROBE_INVALID",
                "probe_command must invoke causal_probe with the registered expectation",
                label,
            )
        )
    if expected is not None and (
        not isinstance(expected, str) or not EXPECTED_PROBE_RE.fullmatch(expected)
    ):
        diagnostics.append(
            Diagnostic(
                "PROBE_EXPECTED_NOT_EXPRESSIBLE",
                "expected must use the causal_probe output vocabulary",
                label,
            )
        )
    if (probe is None) != (expected is None):
        diagnostics.append(
            Diagnostic(
                "PROBE_PAIR_INCOMPLETE",
                "probe_command and expected must either both be null or both be set",
                label,
            )
        )

    if READINESS_LEVELS.index(level) >= READINESS_LEVELS.index("наблюдается"):
        if not real_probe:
            diagnostics.append(
                Diagnostic(
                    "CAUSAL_PROBE_REQUIRED",
                    "level наблюдается or держится requires causal_probe with the registered expectation",
                    label,
                )
            )
        if level == "наблюдается" and isinstance(expected, str):
            required_expected = f"chain passes through {assertion}"
            if expected != required_expected:
                diagnostics.append(
                    Diagnostic(
                        "PROBE_EXPECTED_ASSERTION_MISMATCH",
                        f"expected must be exactly: {required_expected}",
                        label,
                    )
                )
        elif level == "держится" and isinstance(expected, str) and "scene_considers" not in expected:
            diagnostics.append(
                Diagnostic(
                    "ENDURANCE_EXPECTED_REQUIRED",
                    "level держится must expect scene_considers for an explicit number of turns",
                    label,
                )
            )
    return diagnostics


def validate_registries(root: Path) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    expectations: dict[str, str] = {}
    registry_root = root / REGISTRY_REL
    if not registry_root.is_dir():
        return [Diagnostic("REGISTRY_DIRECTORY_MISSING", "registry directory is missing", REGISTRY_REL.as_posix())]
    paths = sorted(registry_root.glob("[0-9][0-9][0-9].yml"))
    if not paths:
        return [Diagnostic("REGISTRY_SET_EMPTY", "no NNN.yml registries found", REGISTRY_REL.as_posix())]
    for path in paths:
        relative = path.relative_to(root).as_posix()
        accepted, status_diagnostics = _decision_status(root, path.stem)
        diagnostics.extend(status_diagnostics)
        entries, parse_diagnostics = parse_registry(path)
        diagnostics.extend(parse_diagnostics)
        for index, entry in enumerate(entries, 1):
            diagnostics.extend(
                validate_registry_entry(entry, accepted=accepted, path=relative, index=index)
            )
            expectation = entry.get("expectation")
            if isinstance(expectation, str):
                if not re.fullmatch(r"[a-z][a-z0-9_]*", expectation):
                    diagnostics.append(
                        Diagnostic(
                            "EXPECTATION_KEY_INVALID",
                            "expectation must be a lowercase snake_case probe key",
                            f"{relative}#{index}",
                        )
                    )
                previous = expectations.get(expectation)
                if previous is not None:
                    diagnostics.append(
                        Diagnostic(
                            "EXPECTATION_KEY_DUPLICATE",
                            f"expectation is already registered by {previous}",
                            f"{relative}#{index}",
                        )
                    )
                else:
                    expectations[expectation] = f"{relative}#{index}"
    return diagnostics


def _parse_mapping_sections(path: Path) -> tuple[dict[str, object], dict[str, dict[str, object]], list[Diagnostic]]:
    top: dict[str, object] = {}
    sections: dict[str, dict[str, object]] = {}
    diagnostics: list[Diagnostic] = []
    current_section: str | None = None
    relative = path.as_posix()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        try:
            if indent == 0:
                if ":" not in raw_line:
                    raise ValueError("expected key: value")
                key, raw_value = raw_line.split(":", 1)
                key = key.strip()
                if raw_value.strip() == "":
                    current_section = key
                    sections.setdefault(key, {})
                else:
                    top[key] = _parse_scalar(raw_value)
                    current_section = None
            elif indent == 2 and current_section is not None:
                key, value = _split_key_value(raw_line.strip())
                sections[current_section][key] = value
            elif current_section == "thresholds":
                raise ValueError("threshold entries must use exactly two spaces")
        except ValueError as exc:
            diagnostics.append(
                Diagnostic("MANIFEST_YAML_INVALID", str(exc), f"{relative}:{line_number}")
            )
    return top, sections, diagnostics


def _normalized_sha256(path: Path) -> str:
    normalized = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(normalized).hexdigest()


def validate_oracle(root: Path) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    manifest_path = root / MANIFEST_REL
    if not manifest_path.is_file():
        return [Diagnostic("ORACLE_MANIFEST_MISSING", "acceptance manifest is missing", MANIFEST_REL.as_posix())]
    top, sections, parse_diagnostics = _parse_mapping_sections(manifest_path)
    diagnostics.extend(parse_diagnostics)
    manifest_label = MANIFEST_REL.as_posix()

    if not isinstance(top.get("version"), int) or int(top.get("version", 0)) < 1:
        diagnostics.append(Diagnostic("ORACLE_VERSION_INVALID", "manifest version must be a positive integer", manifest_label))
    if top.get("labeled_by") != "user":
        diagnostics.append(Diagnostic("ORACLE_LABELER_INVALID", "manifest labeled_by must be user", manifest_label))
    if not isinstance(top.get("frozen_at"), str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}", str(top.get("frozen_at", ""))
    ):
        diagnostics.append(Diagnostic("ORACLE_FROZEN_AT_INVALID", "manifest frozen_at must be YYYY-MM-DD", manifest_label))
    if top.get("per_event_class") is not True:
        diagnostics.append(Diagnostic("ORACLE_PER_CLASS_REQUIRED", "per_event_class must be true", manifest_label))

    thresholds = sections.get("thresholds", {})
    missing_thresholds = [name for name in REQUIRED_THRESHOLDS if name not in thresholds]
    extra_thresholds = sorted(set(thresholds) - set(REQUIRED_THRESHOLDS))
    if missing_thresholds:
        diagnostics.append(
            Diagnostic("ORACLE_THRESHOLD_MISSING", f"missing thresholds: {', '.join(missing_thresholds)}", manifest_label)
        )
    if extra_thresholds:
        diagnostics.append(
            Diagnostic("ORACLE_THRESHOLD_UNKNOWN", f"unknown thresholds: {', '.join(extra_thresholds)}", manifest_label)
        )
    for name, value in thresholds.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
            diagnostics.append(
                Diagnostic("ORACLE_THRESHOLD_INVALID", f"{name} must be numeric in [0, 1]", manifest_label)
            )

    corpus_value = top.get("corpus")
    if not isinstance(corpus_value, str) or not corpus_value.strip():
        diagnostics.append(Diagnostic("ORACLE_CORPUS_MISSING", "manifest corpus must be non-empty", manifest_label))
        return diagnostics
    acceptance_root = (root / ACCEPTANCE_REL).resolve()
    corpus_path = (acceptance_root / corpus_value).resolve()
    try:
        corpus_relative = corpus_path.relative_to(acceptance_root).as_posix()
    except ValueError:
        diagnostics.append(Diagnostic("ORACLE_CORPUS_PATH_ESCAPE", "corpus path escapes acceptance directory", manifest_label))
        return diagnostics
    if not corpus_relative.startswith("corpus/"):
        diagnostics.append(Diagnostic("ORACLE_CORPUS_PATH_INVALID", "corpus must live under acceptance/corpus", manifest_label))
    if not corpus_path.is_file():
        diagnostics.append(Diagnostic("ORACLE_CORPUS_NOT_FOUND", f"corpus file not found: {corpus_value}", manifest_label))
        return diagnostics

    declared_hash = top.get("corpus_hash")
    actual_hash = f"sha256:{_normalized_sha256(corpus_path)}"
    if not isinstance(declared_hash, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", declared_hash):
        diagnostics.append(Diagnostic("ORACLE_HASH_FORMAT_INVALID", "corpus_hash must be sha256:<64 lowercase hex>", manifest_label))
    elif declared_hash != actual_hash:
        diagnostics.append(
            Diagnostic("ORACLE_HASH_MISMATCH", f"declared {declared_hash}, normalized-LF corpus is {actual_hash}", manifest_label)
        )

    corpus_text = corpus_path.read_text(encoding="utf-8")
    corpus_top: dict[str, object] = {}
    for raw_line in corpus_text.splitlines():
        if raw_line.startswith(("version:", "labeled_by:", "source:")):
            key, value = _split_key_value(raw_line)
            corpus_top[key] = value
    if not isinstance(corpus_top.get("version"), int) or int(corpus_top.get("version", 0)) < 1:
        diagnostics.append(Diagnostic("CORPUS_VERSION_INVALID", "corpus version must be a positive integer", corpus_path.relative_to(root).as_posix()))
    if corpus_top.get("labeled_by") != "user":
        diagnostics.append(Diagnostic("CORPUS_LABELER_INVALID", "corpus labeled_by must be user", corpus_path.relative_to(root).as_posix()))
    if not re.search(r"(?m)^examples:\s*$", corpus_text) or not re.search(r"(?m)^  - id:\s*\S", corpus_text):
        diagnostics.append(Diagnostic("CORPUS_EMPTY", "corpus must contain at least one labeled example", corpus_path.relative_to(root).as_posix()))
    source = corpus_top.get("source")
    if not isinstance(source, str) or not re.search(r"(?i)de-identified|обезлич", source):
        diagnostics.append(Diagnostic("PRIVACY_MARKER_MISSING", "corpus source must explicitly state that it is de-identified", corpus_path.relative_to(root).as_posix()))
    if PARTY_ID_RE.search(corpus_text):
        diagnostics.append(Diagnostic("PRIVACY_PARTY_ID_EXPOSED", "public acceptance corpus contains a real-form party identifier", corpus_path.relative_to(root).as_posix()))

    corpus_root = root / ACCEPTANCE_REL / "corpus"
    for public_path in sorted(corpus_root.rglob("*")):
        if not public_path.is_file() or public_path == corpus_path:
            continue
        public_relative = public_path.relative_to(root).as_posix()
        try:
            public_text = public_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            diagnostics.append(
                Diagnostic(
                    "PRIVACY_CORPUS_BINARY_FORBIDDEN",
                    "public acceptance corpus must contain reviewable de-identified text only",
                    public_relative,
                )
            )
            continue
        source_match = re.search(r"(?m)^source:\s*(.+)$", public_text)
        source_text = _strip_comment(source_match.group(1)).strip().strip("\"'") if source_match else ""
        if not re.search(r"(?i)de-identified|обезлич", source_text):
            diagnostics.append(
                Diagnostic(
                    "PRIVACY_MARKER_MISSING",
                    "every public corpus file must attest that its source is de-identified",
                    public_relative,
                )
            )
        if PARTY_ID_RE.search(public_text):
            diagnostics.append(
                Diagnostic(
                    "PRIVACY_PARTY_ID_EXPOSED",
                    "public acceptance corpus contains a real-form party identifier",
                    public_relative,
                )
            )
    return diagnostics


def validate_readiness_surfaces(root: Path) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    seen: set[Path] = set()
    for pattern in POLICY_SURFACE_GLOBS:
        for path in root.glob(pattern):
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            relative = path.relative_to(root).as_posix()
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if BARE_READINESS_RE.search(line) and not any(level in line for level in READINESS_LEVELS):
                    diagnostics.append(
                        Diagnostic(
                            "BARE_READINESS_CLAIM",
                            "readiness claim must use exactly one Decision 022 level",
                            f"{relative}:{line_number}",
                        )
                    )
    return diagnostics


def _git_lines(root: Path, args: list[str]) -> list[str]:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if result.returncode != 0:
        return []
    return [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]


def _default_diff_base(root: Path) -> str | None:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path:
        try:
            event = json.loads(Path(event_path).read_text(encoding="utf-8"))
            base_sha = event.get("pull_request", {}).get("base", {}).get("sha")
            if isinstance(base_sha, str) and base_sha:
                return base_sha
        except (OSError, ValueError, AttributeError):
            pass
    base_ref = os.environ.get("GITHUB_BASE_REF")
    candidates = [f"origin/{base_ref}" if base_ref else None, "origin/main"]
    for candidate in candidates:
        if candidate and _git_lines(root, ["rev-parse", "--verify", candidate]):
            return candidate
    return None


def discover_changed_files(root: Path, diff_base: str | None) -> list[str]:
    effective_base = diff_base or _default_diff_base(root)
    changed: set[str] = set()
    if effective_base:
        changed.update(_git_lines(root, ["diff", "--name-only", f"{effective_base}...HEAD"]))
    changed.update(_git_lines(root, ["diff", "--name-only", "HEAD"]))
    changed.update(_git_lines(root, ["ls-files", "--others", "--exclude-standard"]))
    if not changed:
        changed.update(_git_lines(root, ["diff", "--name-only", "HEAD^", "HEAD"]))
    return sorted(changed)


def validate_oracle_independence(changed_files: Iterable[str]) -> list[Diagnostic]:
    normalized = {item.replace("\\", "/").lstrip("./") for item in changed_files}
    oracle = sorted(path for path in normalized if path in ORACLE_FILES or path.startswith(ORACLE_PREFIX))
    mechanism = sorted(path for path in normalized if path.startswith(MECHANISM_PREFIXES))
    if oracle and mechanism:
        return [
            Diagnostic(
                "ORACLE_MECHANISM_COUPLED_DIFF",
                f"same diff changes acceptance oracle ({oracle[0]}) and mechanism code ({mechanism[0]})",
            )
        ]
    return []


def validate_no_hardcoded_thresholds(root: Path) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    eval_root = root / "roles/apps/files/rp-stack/evals"
    if not eval_root.is_dir():
        return diagnostics
    metric_pattern = "|".join(re.escape(item) for item in REQUIRED_THRESHOLDS)
    metric_assignment = re.compile(
        rf"(?i)[\"']?(?:{metric_pattern})[\"']?\s*[:=]\s*(?:0(?:\.\d+)?|1(?:\.0+)?)\b"
    )
    named_threshold = re.compile(
        rf"(?i)(?:{metric_pattern}).{{0,24}}threshold\s*=\s*(?:0(?:\.\d+)?|1(?:\.0+)?)\b|"
        rf"threshold.{{0,24}}(?:{metric_pattern})\s*=\s*(?:0(?:\.\d+)?|1(?:\.0+)?)\b"
    )
    for path in eval_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".py", ".ps1", ".js", ".ts"}:
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith(ACCEPTANCE_REL.as_posix() + "/"):
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, 1):
            context = "\n".join(lines[max(0, line_number - 4) : line_number])
            hardcoded = named_threshold.search(line) or (
                metric_assignment.search(line) and re.search(r"(?i)threshold", context)
            )
            if hardcoded:
                diagnostics.append(
                    Diagnostic(
                        "EVALUATOR_THRESHOLD_HARDCODED",
                        "acceptance evaluator must read thresholds from manifest.yml",
                        f"{relative}:{line_number}",
                    )
                )
    return diagnostics


def _service_model_bypass(source: str) -> bool:
    return "service_model_settings" in source and "httpx.AsyncClient" in source


def validate_service_model_client_completeness(root: Path) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    app_root = root / "roles/apps/files/rp-stack/rp-gateway/app"
    for path in app_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if _service_model_bypass(source):
            diagnostics.append(
                Diagnostic(
                    "SERVICE_MODEL_CLIENT_BYPASS",
                    "global service-model completion must use ServiceModelClient",
                    path.relative_to(root).as_posix(),
                )
            )
    return diagnostics


def run_self_test() -> int:
    checks: list[tuple[str, str, bool]] = []

    base_entry: dict[str, object] = {
        "requirement": "fixture requirement",
        "level": "каркас",
        "store": "fixture store",
        "assertion": "prompt_presence",
        "expectation": "fixture_expectation",
        "probe_command": None,
        "expected": None,
        "waiver": None,
    }
    codes = {item.code for item in validate_registry_entry(base_entry, accepted=True, path="fixture", index=1)}
    checks.append(("ADR_ACCEPTED_SCAFFOLD_WITHOUT_WAIVER", "accepted scaffold without waiver", "ADR_ACCEPTED_SCAFFOLD_WITHOUT_WAIVER" in codes))

    observed = dict(base_entry, level="наблюдается", waiver="fixture waiver")
    codes = {item.code for item in validate_registry_entry(observed, accepted=True, path="fixture", index=1)}
    checks.append(("CAUSAL_PROBE_REQUIRED", "observed without probe", "CAUSAL_PROBE_REQUIRED" in codes))
    bad_expected = dict(
        observed,
        probe_command="rp-stack-ops causal_probe --party <id> --expectation fixture_expectation",
        expected="works",
    )
    codes = {
        item.code
        for item in validate_registry_entry(bad_expected, accepted=True, path="fixture", index=1)
    }
    checks.append(("PROBE_EXPECTED_NOT_EXPRESSIBLE", "unexpressible expected", "PROBE_EXPECTED_NOT_EXPRESSIBLE" in codes))

    checks.append(
        (
            "BARE_READINESS_CLAIM",
            "bare readiness wording",
            bool(BARE_READINESS_RE.search("Implementation readiness: реализовано")),
        )
    )
    codes = {
        item.code
        for item in validate_oracle_independence(
            [
                MANIFEST_REL.as_posix(),
                "roles/apps/files/rp-stack/rp-gateway/app/services/example.py",
            ]
        )
    }
    checks.append(("ORACLE_MECHANISM_COUPLED_DIFF", "oracle and mechanism coupled diff", "ORACLE_MECHANISM_COUPLED_DIFF" in codes))
    checks.append(("PRIVACY_PARTY_ID_EXPOSED", "real-form party id privacy", PARTY_ID_RE.search("party_deadbeef00") is not None))

    hardcoded = re.compile(
        rf"(?i)[\"']?(?:{'|'.join(REQUIRED_THRESHOLDS)})[\"']?\s*[:=]\s*(?:0(?:\.\d+)?|1(?:\.0+)?)\b"
    )
    checks.append(("EVALUATOR_THRESHOLD_HARDCODED", "hardcoded evaluator threshold", hardcoded.search("event_recall = 0.65") is not None))
    checks.append(
        (
            "SERVICE_MODEL_CLIENT_BYPASS",
            "direct global service-model completion",
            _service_model_bypass(
                "from app.services.service_models import service_model_settings\n"
                "async with httpx.AsyncClient() as client:\n"
                "    await client.post('/chat/completions')\n"
            ),
        )
    )

    failed = False
    for code, name, passed in checks:
        print(f"SELFTEST {'PASS' if passed else 'FAIL'} [{code}]: {name}")
        failed = failed or not passed
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="repository root")
    parser.add_argument("--diff-base", help="validate oracle independence against BASE...HEAD")
    parser.add_argument(
        "--changed-file",
        action="append",
        default=[],
        help="explicit changed path for guard injection; may be repeated",
    )
    parser.add_argument("--self-test", action="store_true", help="run named guard injections")
    args = parser.parse_args(argv)
    if args.self_test:
        return run_self_test()

    root = args.root.resolve()
    diagnostics: list[Diagnostic] = []
    diagnostics.extend(validate_registries(root))
    diagnostics.extend(validate_oracle(root))
    diagnostics.extend(validate_readiness_surfaces(root))
    diagnostics.extend(validate_no_hardcoded_thresholds(root))
    diagnostics.extend(validate_service_model_client_completeness(root))
    changed_files = args.changed_file or discover_changed_files(root, args.diff_base)
    diagnostics.extend(validate_oracle_independence(changed_files))

    for item in diagnostics:
        print(item.render())
    errors = sum(not item.warning for item in diagnostics)
    warnings = sum(item.warning for item in diagnostics)
    if errors:
        print(f"ADR registry validation failed: {errors} error(s), {warnings} warning(s).")
        return 1
    print(f"ADR registry validation passed: {warnings} warning(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
