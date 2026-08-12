from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .core import BrakeSmithError, atomic_write_json

SCHEMA_VERSION = 1
HISTORY_LIMIT = 20
FAILURE_TYPES = {
    "cancelled",
    "encode",
    "existing-output",
    "not-smaller",
    "probe",
    "publish",
    "source",
    "source-delete",
    "stale-partial",
    "validation",
}
BLOCKING_OUTCOMES = {"blocked", "failed", "cancelled"}
BLOCKED_TYPES = {"existing-output", "not-smaller", "stale-partial"}
LOCK_TIMEOUT = 30.0
STALE_LOCK_AGE = 300.0


def default_failure_dir() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        root = Path(configured).expanduser()
    elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        root = Path(str(os.environ["LOCALAPPDATA"]))
    else:
        root = Path.home() / ".local" / "state"
    return root / "brakesmith"


def file_fingerprint(path: Path, size: int | None = None) -> str:
    size = path.stat().st_size if size is None else size
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with path.open("rb") as stream:
        digest.update(stream.read(65_536))
        if size > 65_536:
            stream.seek(max(0, size - 65_536))
            digest.update(stream.read(65_536))
    return digest.hexdigest()


class FailureStore:
    def __init__(self, root: Path | None = None):
        self.root = (root or default_failure_dir()).expanduser().resolve()
        self.path = self.root / "failures.json"
        self.lock_path = self.root / "failures.lock"
        self.logs = self.root / "logs"
        self.items: dict[str, dict[str, object]] = {}
        self._reload()

    def _reload(self) -> None:
        self.items = {}
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise BrakeSmithError(f"Cannot read failure registry {self.path}: {error}") from error
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != SCHEMA_VERSION
            or not isinstance(payload.get("items"), dict)
            or not all(isinstance(record, dict) for record in payload["items"].values())
        ):
            raise BrakeSmithError(f"Unsupported failure registry: {self.path}")
        self.items = payload["items"]

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise BrakeSmithError(
                f"Cannot create failure registry directory {self.root}: {error}"
            ) from error
        deadline = time.monotonic() + LOCK_TIMEOUT
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                try:
                    stale = time.time() - self.lock_path.stat().st_mtime > STALE_LOCK_AGE
                except OSError:
                    stale = False
                if stale:
                    try:
                        self.lock_path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise BrakeSmithError(f"Failure registry is busy: {self.path}")
                time.sleep(0.05)
            except OSError as error:
                raise BrakeSmithError(
                    f"Cannot lock failure registry {self.path}: {error}"
                ) from error
        try:
            try:
                os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            except OSError as error:
                raise BrakeSmithError(
                    f"Cannot lock failure registry {self.path}: {error}"
                ) from error
            self._reload()
            yield
        finally:
            os.close(descriptor)
            try:
                self.lock_path.unlink()
            except OSError:
                pass

    def _save_unlocked(self) -> None:
        atomic_write_json(
            self.path,
            {"schema": SCHEMA_VERSION, "items": self.items},
            force=True,
        )

    @staticmethod
    def key(source: Path) -> str:
        return str(source.expanduser().resolve())

    def _managed_log(self, value: object) -> Path | None:
        if not value or self.logs.is_symlink():
            return None
        try:
            path = Path(str(value)).expanduser().resolve()
            logs = self.logs.resolve()
        except (OSError, RuntimeError):
            return None
        return path if path.parent == logs and path.suffix == ".log" else None

    def _remove_log(self, value: object) -> bool:
        path = self._managed_log(value)
        if not path or not path.exists():
            return False
        path.unlink()
        return True

    def _managed_logs(self, pattern: str = "*.log") -> list[Path]:
        if self.logs.is_symlink():
            return []
        return [
            path
            for candidate in self.logs.glob(pattern)
            if (path := self._managed_log(candidate)) is not None
        ]

    def active(
        self,
        source: Path,
        policy_hash: str | None = None,
        include_released: bool = False,
    ) -> dict[str, object] | None:
        record = self.items.get(self.key(source))
        if not record or (record.get("retry_requested") and not include_released):
            return None
        if (
            policy_hash
            and (record.get("outcome") == "success" or record.get("type") == "probe")
            and record.get("policy_hash") not in {None, policy_hash}
        ):
            return None
        if record.get("source_deleted"):
            if source.exists():
                return None
        else:
            try:
                stat = source.stat()
            except OSError:
                return None
            if (
                record.get("source_size") != stat.st_size
                or record.get("source_modified_ns") != stat.st_mtime_ns
            ):
                return None
            fingerprint = record.get("source_fingerprint")
            if fingerprint:
                try:
                    if fingerprint != file_fingerprint(source, stat.st_size):
                        return None
                except OSError:
                    return None
            elif record.get("source_device") not in {None, stat.st_dev} or record.get(
                "source_inode"
            ) not in {None, stat.st_ino}:
                return None
        output = record.get("output")
        if record.get("outcome") == "success" and output:
            try:
                output_stat = Path(str(output)).stat()
            except OSError:
                return None
            if (
                record.get("output_size") != output_stat.st_size
                or record.get("output_modified_ns") != output_stat.st_mtime_ns
            ):
                return None
            output_fingerprint = record.get("output_fingerprint")
            if output_fingerprint:
                try:
                    if output_fingerprint != file_fingerprint(
                        Path(str(output)), output_stat.st_size
                    ):
                        return None
                except OSError:
                    return None
            elif record.get("output_device") not in {None, output_stat.st_dev} or record.get(
                "output_inode"
            ) not in {None, output_stat.st_ino}:
                return None
        return record

    @staticmethod
    def blocks(record: dict[str, object] | None) -> bool:
        if not record:
            return False
        return str(record.get("outcome", "blocked")) in BLOCKING_OUTCOMES

    @staticmethod
    def _history(
        previous: dict[str, object] | None, attempt: dict[str, object]
    ) -> list[dict[str, object]]:
        history = previous.get("history", []) if previous else []
        if not isinstance(history, list):
            history = []
        if previous and not history:
            history = [
                {
                    "recorded_at": previous.get("recorded_at", previous.get("failed_at")),
                    "outcome": previous.get("outcome", "blocked"),
                    "type": previous.get("type"),
                    "error": previous.get("error"),
                    "result": previous.get("result"),
                    "output": previous.get("output"),
                    "policy_hash": previous.get("policy_hash"),
                }
            ]
        return [
            *[entry for entry in history if isinstance(entry, dict)],
            attempt,
        ][-HISTORY_LIMIT:]

    def _replace(
        self,
        key: str,
        record: dict[str, object],
        log_path: Path | None = None,
        attempt: dict[str, object] | None = None,
        log_content: str | None = None,
    ) -> dict[str, object]:
        try:
            with self._locked():
                previous = self.items.get(key)
                if attempt is not None:
                    record["history"] = self._history(previous, attempt)
                if log_path and log_content is not None:
                    try:
                        if self.logs.is_symlink():
                            raise OSError(f"log directory is a symbolic link: {self.logs}")
                        self.logs.mkdir(parents=True, exist_ok=True)
                        with log_path.open("x", encoding="utf-8") as stream:
                            stream.write(log_content)
                    except OSError as error:
                        raise BrakeSmithError(
                            f"Cannot write failure log {log_path}: {error}"
                        ) from error
                self.items[key] = record
                self._save_unlocked()
        except BrakeSmithError:
            if log_path:
                try:
                    self._remove_log(log_path)
                except OSError:
                    pass
            raise
        previous_log = previous.get("log") if previous else None
        if previous_log and previous_log != str(log_path or ""):
            try:
                self._remove_log(previous_log)
            except OSError:
                pass
        return record

    def record(
        self,
        source: Path,
        kind: str,
        error: str,
        diagnostics: Iterable[str] = (),
        cleanup_error: str | None = None,
        policy_hash: str | None = None,
        duration: float | None = None,
    ) -> dict[str, object]:
        if kind not in FAILURE_TYPES:
            raise BrakeSmithError(f"Unknown failure type: {kind}")
        now = datetime.now(timezone.utc)
        key = self.key(source)
        try:
            stat = source.stat()
            source_size: int | None = stat.st_size
            source_modified_ns: int | None = stat.st_mtime_ns
            source_device: int | None = stat.st_dev
            source_inode: int | None = stat.st_ino
            source_fingerprint: str | None = file_fingerprint(source, stat.st_size)
        except OSError:
            source_size = source_modified_ns = None
            source_device = source_inode = None
            source_fingerprint = None
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
        log_path = self.logs / f"{now:%Y%m%dT%H%M%S.%fZ}-{digest}-{kind}.log"
        header = [
            f"BrakeSmith {__version__}\n",
            f"Time: {now.isoformat()}\n",
            f"Type: {kind}\n",
            f"Source: {key}\n",
            f"Error: {error}\n",
        ]
        if cleanup_error:
            header.append(f"Cleanup: {cleanup_error}\n")
        header.append("\n--- Diagnostics ---\n")
        log_content = "".join([*header, *diagnostics])
        outcome = (
            "cancelled" if kind == "cancelled" else "blocked" if kind in BLOCKED_TYPES else "failed"
        )
        record: dict[str, object] = {
            "source": key,
            "source_size": source_size,
            "source_modified_ns": source_modified_ns,
            "source_device": source_device,
            "source_inode": source_inode,
            "source_fingerprint": source_fingerprint,
            "brakesmith_version": __version__,
            "type": kind,
            "outcome": outcome,
            "error": error,
            "cleanup_error": cleanup_error,
            "failed_at": now.isoformat(),
            "recorded_at": now.isoformat(),
            "log": str(log_path),
            "policy_hash": policy_hash,
            "duration": duration,
            "retry_requested": False,
        }
        attempt = {
            "recorded_at": now.isoformat(),
            "outcome": record["outcome"],
            "type": kind,
            "error": error,
            "policy_hash": policy_hash,
        }
        return self._replace(key, record, log_path, attempt, log_content)

    def succeeded(
        self,
        source: Path,
        output: Path,
        policy_hash: str | None,
        result: str,
        duration: float | None = None,
        source_size: int | None = None,
        source_modified_ns: int | None = None,
        source_deleted: bool = False,
    ) -> dict[str, object]:
        now = datetime.now(timezone.utc)
        key = self.key(source)
        if source_size is None or source_modified_ns is None:
            try:
                stat = source.stat()
            except OSError as error:
                raise BrakeSmithError(
                    f"Cannot record successful source {source}: {error}"
                ) from error
            source_size = stat.st_size
            source_modified_ns = stat.st_mtime_ns
        try:
            output_stat = output.stat()
            output_fingerprint = file_fingerprint(output, output_stat.st_size)
        except OSError as error:
            raise BrakeSmithError(f"Cannot record successful output {output}: {error}") from error
        source_device = source_inode = None
        source_fingerprint = None
        if not source_deleted:
            try:
                source_stat = source.stat()
                source_device = source_stat.st_dev
                source_inode = source_stat.st_ino
                source_fingerprint = file_fingerprint(source, source_stat.st_size)
            except OSError as error:
                raise BrakeSmithError(
                    f"Cannot record successful source {source}: {error}"
                ) from error
        record: dict[str, object] = {
            "source": key,
            "source_size": source_size,
            "source_modified_ns": source_modified_ns,
            "source_deleted": source_deleted,
            "source_device": source_device,
            "source_inode": source_inode,
            "source_fingerprint": source_fingerprint,
            "output": str(output.expanduser().resolve()),
            "output_size": output_stat.st_size,
            "output_modified_ns": output_stat.st_mtime_ns,
            "output_device": output_stat.st_dev,
            "output_inode": output_stat.st_ino,
            "output_fingerprint": output_fingerprint,
            "brakesmith_version": __version__,
            "type": "success",
            "outcome": "success",
            "result": result,
            "error": None,
            "cleanup_error": None,
            "recorded_at": now.isoformat(),
            "log": None,
            "policy_hash": policy_hash,
            "duration": duration,
            "retry_requested": False,
        }
        attempt = {
            "recorded_at": now.isoformat(),
            "outcome": "success",
            "type": "success",
            "result": result,
            "output": record["output"],
            "policy_hash": policy_hash,
        }
        return self._replace(key, record, attempt=attempt)

    def release(self, sources: Iterable[Path]) -> int:
        wanted = {self.key(source) for source in sources}
        with self._locked():
            selected = wanted & self.items.keys()
            for key in selected:
                self.items[key]["retry_requested"] = True
            if selected:
                self._save_unlocked()
        return len(selected)

    def resolve_probe(self, source: Path) -> bool:
        key = self.key(source)
        known = self.items.get(key)
        if not known or known.get("type") != "probe" or not self.blocks(known):
            return False
        try:
            stat = source.stat()
            fingerprint = file_fingerprint(source, stat.st_size)
        except OSError as error:
            raise BrakeSmithError(f"Cannot resolve probe outcome for {source}: {error}") from error
        with self._locked():
            previous = self.items.get(key)
            if not previous or previous.get("type") != "probe" or not self.blocks(previous):
                return False
            now = datetime.now(timezone.utc).isoformat()
            previous_log = previous.get("log")
            record = {
                **previous,
                "source_size": stat.st_size,
                "source_modified_ns": stat.st_mtime_ns,
                "source_device": stat.st_dev,
                "source_inode": stat.st_ino,
                "source_fingerprint": fingerprint,
                "outcome": "ready",
                "result": "probe succeeded",
                "error": None,
                "cleanup_error": None,
                "recorded_at": now,
                "log": None,
                "retry_requested": False,
            }
            record["history"] = self._history(
                previous,
                {
                    "recorded_at": now,
                    "outcome": "ready",
                    "type": "probe",
                    "result": "probe succeeded",
                    "policy_hash": None,
                },
            )
            self.items[key] = record
            self._save_unlocked()
        try:
            self._remove_log(previous_log)
        except OSError:
            pass
        return True

    def records(self, kind: str | None = None) -> list[dict[str, object]]:
        records = [
            record for record in self.items.values() if kind is None or record.get("type") == kind
        ]
        return sorted(
            records,
            key=lambda record: str(record.get("recorded_at", record.get("failed_at", ""))),
            reverse=True,
        )

    def forget(self, sources: Iterable[Path], keep_logs: bool = False) -> tuple[int, int]:
        wanted = {self.key(source) for source in sources}
        with self._locked():
            selected = wanted & self.items.keys()
            logs = [self.items[key].get("log") for key in selected]
            for key in selected:
                self.items.pop(key)
            if selected:
                self._save_unlocked()
        logs_removed = 0
        if not keep_logs:
            for log in logs:
                if self._remove_log(log):
                    logs_removed += 1
        return len(selected), logs_removed

    def prune(self) -> tuple[int, int]:
        self._reload()
        snapshot = dict(self.items)
        stale_candidates = {
            key
            for key, record in snapshot.items()
            if not self.active(Path(str(record.get("source", key))), include_released=True)
        }
        with self._locked():
            stale = {key for key in stale_candidates if self.items.get(key) == snapshot.get(key)}
            for key in stale:
                self.items.pop(key)
            if stale:
                self._save_unlocked()
            referenced_logs = {
                str(record["log"]) for record in self.items.values() if record.get("log")
            }
            logs_removed = 0
            for log in self._managed_logs():
                if str(log) not in referenced_logs:
                    log.unlink()
                    logs_removed += 1
        return len(stale), logs_removed

    def clear(
        self,
        kind: str | None = None,
        logs_only: bool = False,
        keep_logs: bool = False,
    ) -> tuple[int, int]:
        with self._locked():
            selected = {
                key: record
                for key, record in self.items.items()
                if (kind is None and self.blocks(record)) or record.get("type") == kind
            }
            logs_removed = 0
            if not keep_logs:
                pattern = f"*-{kind}.log" if kind else "*.log"
                for log in self._managed_logs(pattern):
                    log.unlink()
                    logs_removed += 1
                for record in selected.values():
                    if logs_only:
                        record["log"] = None
            if not logs_only:
                for key in selected:
                    self.items.pop(key, None)
            if selected:
                self._save_unlocked()
        return (0 if logs_only else len(selected), logs_removed)
