"""Skills в формате Agent Skills: папка с SKILL.md (YAML frontmatter + инструкции).

Каталог (имя и описание) дешёвый и может постоянно лежать в промпте. Тело и references
подгружает только узел, которому Skill нужен (progressive disclosure).
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_SKILLS_DIR = Path(__file__).resolve().parents[3] / "skills"

MAX_NAME_LEN = 64
MAX_DESCRIPTION_LEN = 1024
RESERVED_WORDS = ("anthropic", "claude")
NAME_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
XML_TAG_RE = re.compile(r"<[^>]+>")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)


class SkillError(ValueError):
    pass


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    root: Path

    def reference(self, relative_path: str) -> str:
        path = (self.root / relative_path).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise SkillError(f"{relative_path!r} выходит за пределы skill {self.name!r}")
        return path.read_text(encoding="utf-8")


def parse_skill(skill_dir: Path) -> Skill:
    file = skill_dir / "SKILL.md"
    match = FRONTMATTER_RE.match(file.read_text(encoding="utf-8"))
    if match is None:
        raise SkillError(f"{file}: нет YAML frontmatter между строками ---")
    meta = yaml.safe_load(match.group(1)) or {}
    name = meta.get("name")
    description = meta.get("description")
    _validate_name(name, skill_dir.name, file)
    _validate_description(description, file)
    return Skill(
        name=name, description=description.strip(), body=match.group(2).strip(), root=skill_dir
    )


def _validate_name(name: object, dir_name: str, file: Path) -> None:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or len(name) > MAX_NAME_LEN:
        raise SkillError(
            f"{file}: name — строчные латинские буквы, цифры и дефисы, до {MAX_NAME_LEN} символов"
        )
    if any(word in name for word in RESERVED_WORDS):
        raise SkillError(f"{file}: name не может содержать {' / '.join(RESERVED_WORDS)}")
    if name != dir_name:
        raise SkillError(f"{file}: name {name!r} не совпадает с именем папки {dir_name!r}")


def _validate_description(description: object, file: Path) -> None:
    if not isinstance(description, str) or not description.strip():
        raise SkillError(f"{file}: description обязателен")
    if len(description) > MAX_DESCRIPTION_LEN:
        raise SkillError(f"{file}: description длиннее {MAX_DESCRIPTION_LEN} символов")
    if XML_TAG_RE.search(description):
        raise SkillError(f"{file}: XML-теги в description запрещены")


class SkillRegistry:
    def __init__(self, skills: dict[str, Skill]) -> None:
        self._skills = skills

    @classmethod
    def load(cls, skills_dir: Path = DEFAULT_SKILLS_DIR) -> "SkillRegistry":
        skill_dirs = sorted(p for p in skills_dir.iterdir() if (p / "SKILL.md").is_file())
        return cls({skill.name: skill for skill in map(parse_skill, skill_dirs)})

    def names(self) -> list[str]:
        return sorted(self._skills)

    def get(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError:
            raise KeyError(f"skill {name!r} не найден, есть: {self.names()}") from None

    def catalog(self) -> str:
        return "\n".join(f"- {s.name}: {s.description}" for s in self._skills.values())
