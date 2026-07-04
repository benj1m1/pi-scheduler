from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from . import config

SKILL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class SkillCatalogError(ValueError):
    pass


@dataclass(frozen=True)
class SkillEntry:
    id: str
    name: str
    description: str
    path: Path


def is_valid_skill_id(skill_id: str) -> bool:
    return bool(SKILL_ID_RE.fullmatch(skill_id or ""))


def parse_skill_ids(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if raw is None:
        return []
    values = raw.splitlines() if isinstance(raw, str) else list(raw)
    result: list[str] = []
    for value in values:
        item = str(value).strip()
        if item and item not in result:
            result.append(item)
    return result


def _frontmatter_value(text: str, key: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    prefix = f"{key}:"
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith(prefix):
            return line[len(prefix) :].strip().strip('"').strip("'")
    return ""


def _metadata(skill_id: str, skill_file: Path) -> tuple[str, str]:
    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError:
        return skill_id, ""
    name = _frontmatter_value(text, "name") or skill_id
    description = _frontmatter_value(text, "description")
    return name, description


def _safe_child_path(root: Path, skill_id: str) -> Path:
    if not is_valid_skill_id(skill_id):
        raise SkillCatalogError("Skill ID is invalid")
    resolved_root = root.resolve()
    candidate = root / skill_id
    if candidate.is_symlink():
        raise SkillCatalogError(f"Skill {skill_id!r} is not an approved skill")
    resolved = candidate.resolve()
    if resolved.parent != resolved_root:
        raise SkillCatalogError("Skill path escapes the approved skills directory")
    return resolved


def list_skills() -> list[SkillEntry]:
    root = config.APPROVED_SKILLS_DIR
    if not root.exists() or not root.is_dir():
        return []
    entries: list[SkillEntry] = []
    for child in root.iterdir():
        skill_id = child.name
        if not is_valid_skill_id(skill_id):
            continue
        if child.is_symlink() or not child.is_dir():
            continue
        try:
            resolved = _safe_child_path(root, skill_id)
        except SkillCatalogError:
            continue
        skill_file = resolved / "SKILL.md"
        if not skill_file.is_file():
            continue
        name, description = _metadata(skill_id, skill_file)
        entries.append(SkillEntry(id=skill_id, name=name, description=description, path=resolved))
    return sorted(entries, key=lambda entry: entry.id)


def resolve_skill_path(skill_id: str) -> Path:
    root = config.APPROVED_SKILLS_DIR
    resolved = _safe_child_path(root, skill_id)
    if not resolved.is_dir() or not (resolved / "SKILL.md").is_file():
        raise SkillCatalogError(f"Skill {skill_id!r} is not an approved skill")
    return resolved
