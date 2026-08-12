from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .core import BrakeSmithError

PROTOCOL_VERSION = 1
EVENT_PREFIX = "BRAKESMITH_EVENT "


class BridgeError(Exception):
    """A safe, user-facing machine bridge error."""


def emit(event: str, **payload: object) -> None:
    sys.stdout.write(json.dumps({"protocol": PROTOCOL_VERSION, "event": event, **payload}) + "\n")
    sys.stdout.flush()


def require_string(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BridgeError(f"{key} must be a non-empty string")
    return value


def optional_string(params: dict[str, Any], key: str) -> Optional[str]:
    value = params.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise BridgeError(f"{key} must be a string")
    return value


def option(args: list[str], name: str, value: object) -> None:
    if value is None or value == "":
        return
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise BridgeError(f"{name} has an invalid value")
    args.extend([f"--{name.replace('_', '-')}", str(value)])


def flag(args: list[str], name: str, enabled: object) -> None:
    if enabled is None:
        return
    if not isinstance(enabled, bool):
        raise BridgeError(f"{name} must be true or false")
    if enabled:
        args.append(f"--{name.replace('_', '-')}")


def tool_options(args: list[str], params: dict[str, Any], *names: str) -> None:
    for name in names:
        option(args, name, optional_string(params, name))


def scan_options(args: list[str], params: dict[str, Any]) -> None:
    for name in ("depth", "extensions", "workers", "probe_timeout", "cache_file"):
        option(args, name, params.get(name))
    if params.get("use_cache") is False:
        args.append("--no-cache")
    tool_options(args, params, "ffprobe")


def write_selection(params: dict[str, Any]) -> Optional[Path]:
    sources = params.get("sources")
    if sources is None:
        return None
    if (
        not isinstance(sources, list)
        or not sources
        or not all(isinstance(item, str) for item in sources)
    ):
        raise BridgeError("sources must be a non-empty list of paths")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".json",
        prefix="brakesmith-selection-",
        delete=False,
    ) as handle:
        json.dump(sources, handle)
    return Path(handle.name)


def command_for(
    method: str, params: dict[str, Any]
) -> tuple[list[str], bool, set[int], Optional[Path]]:
    selection: Optional[Path] = None
    if method == "doctor":
        args = ["doctor", "--json"]
        tool_options(args, params, "handbrake", "ffprobe", "ffmpeg")
        return args, True, {0, 1}, None
    if method in {"scan", "status"}:
        args = [method, require_string(params, "root"), "--json"]
        scan_options(args, params)
        return args, True, {0}, None
    if method == "history":
        args = ["history", require_string(params, "root"), "--json"]
        option(args, "type", optional_string(params, "type"))
        return args, True, {0}, None
    if method == "failures.list":
        args = ["failures", "list", "--json"]
        option(args, "type", optional_string(params, "type"))
        return args, True, {0}, None
    if method == "health":
        args = ["health", require_string(params, "root"), "--json"]
        if params.get("full") is True:
            args.append("--full")
        for name in ("depth", "timeout", "extensions"):
            option(args, name, params.get(name))
        tool_options(args, params, "ffprobe", "ffmpeg")
        selection = write_selection(params)
        option(args, "selection", str(selection) if selection else None)
        return args, True, {0, 1}, selection
    if method == "candidates.export":
        args = ["candidates", require_string(params, "root")]
        option(args, "output", require_string(params, "output"))
        for name in (
            "depth",
            "extensions",
            "workers",
            "probe_timeout",
            "cache_file",
            "include",
            "exclude",
            "min_size",
            "max_size",
            "min_duration",
            "max_duration",
            "codecs",
        ):
            option(args, name, params.get(name))
        flag(args, "force", params.get("force"))
        flag(args, "include_blocked", params.get("include_blocked"))
        if params.get("use_cache") is False:
            args.append("--no-cache")
        tool_options(args, params, "ffprobe")
        return args, False, {0}, None
    if method == "plan.create":
        args = ["plan", require_string(params, "root")]
        option(args, "output", require_string(params, "output"))
        args.append("--non-interactive")
        selection = write_selection(params)
        option(args, "selection", str(selection) if selection else None)
        for name in (
            "depth",
            "max_files",
            "audio",
            "subtitles",
            "unknown_audio",
            "unknown_subtitles",
            "exclude_titles",
            "original_language",
            "format_preset",
            "quality",
            "preset",
            "bit_depth",
            "tune",
            "encoder_profile",
            "encoder_level",
            "crop",
            "deinterlace",
            "output_directory",
            "overrides",
            "extensions",
            "workers",
            "probe_timeout",
            "cache_file",
            "profile",
            "config",
        ):
            option(args, name, params.get(name))
        for name in (
            "keep_commentary",
            "forced_subtitles_only",
            "lossless",
            "stop_when_larger",
            "include_hevc",
            "allow_no_audio",
            "retry_blocked",
            "force",
        ):
            flag(args, name, params.get(name))
        if params.get("keep_original") is True:
            args.append("--keep-original")
        else:
            args.append("--no-keep-original")
        if params.get("replace_source") is True:
            args.append("--replace-source")
        else:
            args.append("--keep-source")
        if params.get("use_cache") is False:
            args.append("--no-cache")
        tool_options(args, params, "handbrake", "ffprobe")
        return args, False, {0}, selection
    if method == "plan.execute":
        args = ["execute", require_string(params, "plan_file")]
        option(args, "state_file", optional_string(params, "state_file"))
        option(args, "max_failures", params.get("max_failures"))
        flag(args, "retry_blocked", params.get("retry_blocked"))
        flag(args, "stop_after_current", params.get("stop_after_current"))
        return args, False, {0, 1, 130}, None
    if method == "outcomes.retry":
        args = ["retry"]
        sources = params.get("sources", [])
        if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
            raise BridgeError("sources must be a list of paths")
        args.extend(sources)
        option(args, "root", optional_string(params, "root"))
        option(args, "type", optional_string(params, "type"))
        return args, False, {0}, None
    if method == "outcomes.forget":
        sources = params.get("sources")
        if (
            not isinstance(sources, list)
            or not sources
            or not all(isinstance(item, str) for item in sources)
        ):
            raise BridgeError("sources must be a non-empty list of paths")
        args = ["failures", "forget", *sources]
        flag(args, "keep_logs", params.get("keep_logs"))
        return args, False, {0}, None
    if method == "outcomes.prune":
        return ["failures", "prune"], False, {0}, None
    if method == "outcomes.clear":
        args = ["failures", "clear", "--yes"]
        option(args, "type", optional_string(params, "type"))
        flag(args, "logs_only", params.get("logs_only"))
        flag(args, "keep_logs", params.get("keep_logs"))
        return args, False, {0}, None
    raise BridgeError(f"Unknown method: {method}")


def cli_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "brakesmith"]


def run_child(
    args: list[str],
    parse_json: bool,
    accepted_codes: set[int],
    cancel_file: Optional[Path] = None,
) -> tuple[int, object, str]:
    environment = os.environ.copy()
    environment.update({"BRAKESMITH_EVENTS": "1", "PYTHONUNBUFFERED": "1", "NO_COLOR": "1"})
    if cancel_file:
        environment["BRAKESMITH_CANCEL_FILE"] = str(cancel_file)
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        [*cli_command(), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        env=environment,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
    streams: queue.Queue[tuple[str, Optional[str]]] = queue.Queue()

    def read_stream(name: str, stream: Any) -> None:
        try:
            for line in stream:
                streams.put((name, line.rstrip("\r\n")))
        finally:
            streams.put((name, None))

    assert process.stdout is not None and process.stderr is not None
    threads = [
        threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    watcher_done = threading.Event()

    def watch_cancel() -> None:
        if not cancel_file:
            return
        while not watcher_done.wait(0.1):
            if cancel_file.exists() and process.poll() is None:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGTERM)
                return

    threading.Thread(target=watch_cancel, daemon=True).start()

    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def cancel_child(signum: int, frame: object) -> None:
        del signum, frame
        if process.poll() is None:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)

    signal.signal(signal.SIGTERM, cancel_child)
    stdout_lines: list[str] = []
    diagnostic_tail: deque[str] = deque(maxlen=250)
    closed = 0
    try:
        while closed < 2:
            name, line = streams.get()
            if line is None:
                closed += 1
                continue
            if name == "stdout" or not line.startswith(EVENT_PREFIX):
                diagnostic_tail.append(line)
            if name == "stdout" and parse_json:
                stdout_lines.append(line)
            if name == "stdout" and not parse_json:
                emit("log", stream=name, message=line)
            elif line.startswith(EVENT_PREFIX):
                try:
                    event = json.loads(line[len(EVENT_PREFIX) :])
                except json.JSONDecodeError:
                    emit("log", stream=name, message=line)
                else:
                    if isinstance(event, dict):
                        event_name = str(event.pop("event", "progress"))
                        emit(event_name, **event)
            else:
                emit("log", stream=name, message=line)
        return_code = process.wait()
    finally:
        watcher_done.set()
        signal.signal(signal.SIGTERM, previous_sigterm)
    output = "\n".join(diagnostic_tail).strip()
    result: object = {"output": output, "exit_code": return_code}
    if return_code not in accepted_codes:
        raise BridgeError(output or f"BrakeSmith exited with code {return_code}")
    if parse_json:
        raw = "\n".join(stdout_lines).strip()
        if raw:
            try:
                result = json.loads(raw)
            except json.JSONDecodeError as error:
                raise BridgeError(f"Command returned invalid JSON: {error}") from error
    return return_code, result, output


def watch_parent(parent_pid: int) -> None:
    """Stop a detached bridge when its desktop parent exits."""
    while True:
        time.sleep(0.5)
        if os.getppid() != parent_pid:
            os.kill(os.getpid(), signal.SIGTERM)
            return


def run_bridge(protocol: int) -> None:
    if protocol != PROTOCOL_VERSION:
        emit("error", message=f"Unsupported protocol {protocol}", supported=PROTOCOL_VERSION)
        raise SystemExit(2)
    threading.Thread(target=watch_parent, args=(os.getppid(),), daemon=True).start()
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise BridgeError("Request must be a JSON object")
        if request.get("protocol", PROTOCOL_VERSION) != PROTOCOL_VERSION:
            raise BridgeError(f"Request protocol must be {PROTOCOL_VERSION}")
        method = request.get("method")
        params = request.get("params", {})
        if not isinstance(method, str) or not method:
            raise BridgeError("method must be a non-empty string")
        if not isinstance(params, dict):
            raise BridgeError("params must be a JSON object")
        emit("accepted", method=method)
        if method == "capabilities":
            emit(
                "result",
                data={
                    "protocol": PROTOCOL_VERSION,
                    "version": __version__,
                    "methods": [
                        "doctor",
                        "scan",
                        "status",
                        "history",
                        "health",
                        "failures.list",
                        "candidates.export",
                        "plan.create",
                        "plan.read",
                        "plan.execute",
                        "outcomes.retry",
                        "outcomes.forget",
                        "outcomes.prune",
                        "outcomes.clear",
                    ],
                },
            )
            emit("finished", ok=True, exit_code=0)
            return
        if method == "plan.read":
            from .plans import load_plan

            plan = load_plan(Path(require_string(params, "plan_file")))
            emit("result", data=plan)
            emit("finished", ok=True, exit_code=0)
            return
        args, parse_json, accepted_codes, selection = command_for(method, params)
        try:
            raw_cancel_file = request.get("cancel_file")
            cancel_file = (
                Path(raw_cancel_file)
                if isinstance(raw_cancel_file, str) and raw_cancel_file
                else None
            )
            return_code, result, _ = run_child(args, parse_json, accepted_codes, cancel_file)
            if method == "plan.create":
                result = json.loads(
                    Path(require_string(params, "output")).read_text(encoding="utf-8")
                )
            elif method == "plan.execute":
                plan_file = Path(require_string(params, "plan_file"))
                state_file = optional_string(params, "state_file")
                journal = (
                    Path(state_file)
                    if state_file
                    else plan_file.with_name(f"{plan_file.stem}.state.json")
                )
                if journal.is_file():
                    result = json.loads(journal.read_text(encoding="utf-8"))
            emit("result", data=result)
            emit("finished", ok=return_code == 0, exit_code=return_code)
        finally:
            if selection:
                try:
                    selection.unlink()
                except OSError:
                    pass
    except (BridgeError, BrakeSmithError, OSError, json.JSONDecodeError) as error:
        emit("error", message=str(error))
        emit("finished", ok=False, exit_code=2)
        raise SystemExit(2) from error
