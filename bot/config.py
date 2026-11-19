"""Config loading. All YAML lives in config/."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


@dataclass
class Config:
    account: dict
    challenge: dict
    risk: dict
    strategy: dict
    sim: dict
    root: Path = field(default=ROOT)

    def copy(self) -> "Config":
        return copy.deepcopy(self)

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p


def load_config(root: Path | str = ROOT) -> Config:
    root = Path(root)
    c = root / "config"
    return Config(
        account=_load(c / "account.yaml"),
        challenge=_load(c / "challenge.yaml"),
        risk=_load(c / "risk.yaml"),
        strategy=_load(c / "strategy.yaml"),
        sim=_load(c / "sim.yaml"),
        root=root,
    )
