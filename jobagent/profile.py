"""Precision profiles: one TOML file per person with a detailed search (see profiles/example.toml)."""

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .filters import STOPWORDS


def _any(patterns):
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I) if patterns else None


def _places(words):
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I) if words else None


@dataclass
class Profile:
    path: Path
    raw: dict
    id: str = ""
    name: str = ""
    email: str = ""
    candidate: str = ""
    title_include: list = field(default_factory=list)
    title_exclude: re.Pattern | None = None
    home: re.Pattern | None = None
    remote_ok: re.Pattern | None = None
    hard_exclude: list = field(default_factory=list)
    require_any: list = field(default_factory=list)
    require_written_in: list = field(default_factory=list)
    categories: list = field(default_factory=list)
    penalties: list = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Profile":
        raw = tomllib.loads(Path(path).read_text())
        p = cls(path=Path(path), raw=raw)
        p.name = raw.get("name", Path(path).stem)
        p.email = raw["email"]
        p.id = hashlib.sha256(f"{Path(path).stem}|{p.email}".encode()).hexdigest()[:12]
        p.candidate = raw.get("candidate", "").strip()
        p.title_include = [re.compile(x, re.I) for x in raw["titles"]["include"]]
        p.title_exclude = _any(raw["titles"].get("exclude", []))
        p.home = _places(raw["location"].get("home", []))
        p.remote_ok = _places(raw["location"].get("remote_ok", []))
        rules = raw.get("rules", {})
        p.hard_exclude = [re.compile(x, re.I) for x in rules.get("hard_exclude", [])]
        p.require_any = [re.compile(x, re.I) for x in rules.get("require_any", [])]
        p.require_written_in = [x.lower() for x in rules.get("require_written_in", [])]
        if unknown := set(p.require_written_in) - set(STOPWORDS):
            raise ValueError(f"{path}: require_written_in supports {sorted(STOPWORDS)}, not {sorted(unknown)}")
        p.categories = raw.get("scoring", {}).get("category", [])
        p.penalties = raw.get("scoring", {}).get("penalty", [])
        if not p.categories:
            raise ValueError(f"{path}: needs at least one [[scoring.category]]")
        return p

    # convenience accessors with defaults
    def search(self, key, default=None):
        return self.raw.get("search", {}).get(key, default)

    def out(self, key, default=None):
        return self.raw.get("output", {}).get(key, default)

    def comp(self, key, default=None):
        return self.raw.get("compensation", {}).get(key, default)

    @property
    def sources(self) -> dict:
        return self.raw.get("sources", {})

    @property
    def externals(self) -> list[dict]:
        """[[external]] sites to check by hand (not searchable automatically): {name, url, note}. https only."""
        out = []
        for x in self.raw.get("external", []):
            if not str(x.get("url", "")).startswith("https://"):
                raise ValueError(f"{self.path}: [[external]] {x.get('name')!r} needs an https:// url")
            out.append({"name": x["name"], "url": x["url"], "note": x.get("note", "")})
        return out

    @property
    def max_points(self) -> int:
        return sum(int(c["points"]) for c in self.categories)


def discover(paths: list[str] | None, private_dir: Path, example: Path) -> list[Profile]:
    files = [Path(p) for p in paths] if paths else sorted(private_dir.glob("*.toml"))
    if not files:
        raise SystemExit(f"No profiles found. Copy {example} to {private_dir}/<you>.toml and adapt it.")
    return [Profile.load(f) for f in files]
