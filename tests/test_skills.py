from pathlib import Path

import pytest

from mailpulse.agent.skills import SkillError, SkillRegistry, parse_skill


def write_skill(root: Path, dir_name: str, frontmatter: str, body: str = "Инструкции") -> Path:
    skill_dir = root / dir_name
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return skill_dir


def test_repository_skills_are_valid():
    registry = SkillRegistry.load()

    assert registry.names() == ["email-triage", "reply-drafting"]
    for name in registry.names():
        skill = registry.get(name)
        assert skill.body
        assert name in registry.catalog()


def test_triage_skill_reference_is_loadable():
    skill = SkillRegistry.load().get("email-triage")

    assert "Пограничные случаи" in skill.reference("references/examples.md")


def test_reference_cannot_escape_skill_directory():
    skill = SkillRegistry.load().get("email-triage")

    with pytest.raises(SkillError):
        skill.reference("../../pyproject.toml")


@pytest.mark.parametrize(
    "name", ["Email-Triage", "email_triage", "-triage", "claude-helper", "a" * 65]
)
def test_invalid_names_are_rejected(tmp_path, name):
    skill_dir = write_skill(tmp_path, name, f"name: {name}\ndescription: Does things.")

    with pytest.raises(SkillError):
        parse_skill(skill_dir)


def test_name_must_match_directory(tmp_path):
    skill_dir = write_skill(tmp_path, "triage", "name: email-triage\ndescription: Does things.")

    with pytest.raises(SkillError, match="папки"):
        parse_skill(skill_dir)


@pytest.mark.parametrize("description", ["''", "Use <tool> here", "x" * 1025])
def test_invalid_descriptions_are_rejected(tmp_path, description):
    skill_dir = write_skill(tmp_path, "triage", f"name: triage\ndescription: {description}")

    with pytest.raises(SkillError):
        parse_skill(skill_dir)


def test_missing_frontmatter_is_rejected(tmp_path):
    skill_dir = tmp_path / "triage"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Без frontmatter\n", encoding="utf-8")

    with pytest.raises(SkillError, match="frontmatter"):
        parse_skill(skill_dir)
