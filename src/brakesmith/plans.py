from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .core import BrakeSmithError, atomic_write_json

SCHEMA_VERSION = 1


def digest_payload(payload: dict[str, object]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "digest"}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def seal_plan(payload: dict[str, object]) -> dict[str, object]:
    sealed = dict(payload)
    sealed["schema"] = SCHEMA_VERSION
    sealed["digest"] = digest_payload(sealed)
    return sealed


def load_plan(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BrakeSmithError(f"Cannot read plan {path}: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA_VERSION:
        raise BrakeSmithError(f"Unsupported plan schema: {path}")
    if payload.get("digest") != digest_payload(payload):
        raise BrakeSmithError(f"Plan was edited or corrupted; regenerate it: {path}")
    if not isinstance(payload.get("items"), list):
        raise BrakeSmithError(f"Plan has no item list: {path}")
    return payload


def write_plan(path: Path, payload: dict[str, object], force: bool = False) -> None:
    atomic_write_json(path, seal_plan(payload), force)


def load_state(path: Path, plan_digest: str) -> dict[str, object]:
    if not path.exists():
        return {"plan_digest": plan_digest, "items": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BrakeSmithError(f"Cannot read batch state {path}: {error}") from error
    if state.get("plan_digest") != plan_digest or not isinstance(state.get("items"), dict):
        raise BrakeSmithError(f"Batch state does not match plan: {path}")
    return state


def write_state(path: Path, state: dict[str, object]) -> None:
    atomic_write_json(path, state, force=True)
