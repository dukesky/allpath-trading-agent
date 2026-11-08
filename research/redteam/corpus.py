"""Loads the committed attack corpus (corpus.yaml)."""

from __future__ import annotations

from pathlib import Path

import yaml

CORPUS_PATH = Path(__file__).with_name("corpus.yaml")
STATUSES = ("caught", "neutralised", "bounded", "not caught")


def _raw() -> dict:
    return yaml.safe_load(CORPUS_PATH.read_text())


def load() -> list[dict]:
    return list(_raw()["cases"])


def by_id() -> dict[str, dict]:
    return {c["id"]: c for c in load()}


def fixtures() -> dict[str, str]:
    return dict(_raw()["fixtures"])
