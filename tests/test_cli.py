import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from brakesmith import cli
from brakesmith.core import MediaFile, Track

runner = CliRunner()


def test_delete_replaced_source_keeps_validated_output(tmp_path: Path):
    source = tmp_path / "movie.x264.mkv"
    destination = tmp_path / "movie.x265.mkv"
    source.write_text("old")
    destination.write_text("new")

    cli.delete_replaced_source(source, destination)

    assert not source.exists()
    assert destination.read_text() == "new"


def test_discard_not_smaller_keeps_only_smaller_output(tmp_path: Path):
    smaller = tmp_path / "smaller.mkv"
    smaller.write_bytes(b"123")
    assert cli.discard_not_smaller(smaller, 4) is None
    assert smaller.exists()

    equal = tmp_path / "equal.mkv"
    equal.write_bytes(b"1234")
    assert cli.discard_not_smaller(equal, 4) == 4
    assert not equal.exists()


def test_early_size_guard_waits_until_partial_reaches_source_size(tmp_path: Path):
    partial = tmp_path / "movie.part"
    partial.write_bytes(b"123")
    assert cli.partial_reached_source_size(partial, 4) is None
    partial.write_bytes(b"1234")
    assert cli.partial_reached_source_size(partial, 4) == 4


def test_batch_setup_defaults_to_recommended_and_can_save(monkeypatch, tmp_path: Path):
    class Answer:
        def __init__(self, value):
            self.value = value

        def ask(self):
            return self.value

    def select(*args, **kwargs):
        assert kwargs["default"] == "recommended"
        recommended = kwargs["choices"][0]
        assert "Recommended" in recommended.title
        assert "1080p RF 20/medium" in recommended.description
        assert "4k RF 18/slow" in recommended.description
        return Answer("recommended")

    def text(*args, **kwargs):
        assert kwargs["default"] == "1"
        assert kwargs["validate"]("5") is True
        assert isinstance(kwargs["validate"]("0"), str)
        return Answer("1")

    monkeypatch.setattr(cli.questionary, "select", select)
    monkeypatch.setattr(cli.questionary, "text", text)
    monkeypatch.setattr(cli.questionary, "confirm", lambda *args, **kwargs: Answer(True))

    settings = tmp_path / "format.json"
    choice = cli.reconcile_format_choice(None, 18, "slow", 10, None, False, settings)
    assert choice.name == "recommended"
    assert json.loads(settings.read_text())["format_preset"] == "recommended"
    assert cli.reconcile_max_files(None, False) == 1
    assert cli.reconcile_format_choice(None, 21, "fast", 8, None, True).name == "recommended"
    assert cli.reconcile_max_files(5, True) == 5


def test_saved_format_can_continue_without_reopening_profiles(monkeypatch, tmp_path: Path):
    settings = tmp_path / "format.json"
    settings.write_text(
        json.dumps(
            {
                "format_preset": "compact",
                "quality": 18,
                "preset": "slow",
                "bit_depth": 10,
                "encoder_profile": None,
            }
        )
    )

    class Answer:
        def ask(self):
            return "saved"

    def select(*args, **kwargs):
        assert kwargs["default"] == "saved"
        assert kwargs["choices"][0].title.startswith("Use saved: Compact")
        return Answer()

    monkeypatch.setattr(cli.questionary, "select", select)
    choice = cli.reconcile_format_choice(None, 18, "slow", 10, None, False, settings)
    assert choice.name == "compact"


def test_candidates_exports_only_non_hevc(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.touch()
    monkeypatch.setattr(
        cli,
        "inspect",
        lambda *args, **kwargs: [
            MediaFile(source, "h264", 60, 10),
            MediaFile(tmp_path / "done.mkv", "hevc", 60, 5),
        ],
    )
    report = tmp_path / "candidates.json"
    result = runner.invoke(cli.app, ["candidates", str(tmp_path), "--output", str(report)])
    assert result.exit_code == 0
    assert [item["path"] for item in json.loads(report.read_text())] == [str(source)]


def test_candidates_excludes_blocked_unless_requested(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    monkeypatch.setattr(
        cli,
        "inspect",
        lambda *args, **kwargs: [MediaFile(source, "h264", 60, source.stat().st_size)],
    )
    cli.FailureStore().record(source, "not-smaller", "too large")

    blocked_report = tmp_path / "blocked.json"
    all_report = tmp_path / "all.json"
    blocked = runner.invoke(cli.app, ["candidates", str(tmp_path), "--output", str(blocked_report)])
    included = runner.invoke(
        cli.app,
        [
            "candidates",
            str(tmp_path),
            "--output",
            str(all_report),
            "--include-blocked",
        ],
    )

    assert blocked.exit_code == included.exit_code == 0
    assert json.loads(blocked_report.read_text()) == []
    included_items = json.loads(all_report.read_text())
    assert [item["path"] for item in included_items] == [str(source)]
    assert included_items[0]["transcode_status"] == "blocked"
    assert included_items[0]["blocked_reason"] == "not-smaller"


def test_candidates_refuses_to_replace_report(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cli, "inspect", lambda *args, **kwargs: [])
    report = tmp_path / "candidates.txt"
    report.write_text("keep me")
    result = runner.invoke(cli.app, ["candidates", str(tmp_path), "--output", str(report)])
    assert result.exit_code == 2
    assert report.read_text() == "keep me"


def test_status_groups_ready_success_blocked_hevc_and_stale(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    ready = tmp_path / "ready.mkv"
    blocked = tmp_path / "blocked.mkv"
    complete = tmp_path / "complete.mkv"
    output = tmp_path / "complete.x265.mkv"
    hevc = tmp_path / "native-hevc.mkv"
    missing = tmp_path / "missing.mkv"
    for path in (ready, blocked, complete, hevc, missing):
        path.write_bytes(b"source")
    output.write_bytes(b"output")
    store = cli.FailureStore()
    store.record(blocked, "not-smaller", "large")
    store.succeeded(complete, output, None, "published", duration=60)
    store.record(missing, "encode", "failed")
    missing.unlink()
    monkeypatch.setattr(
        cli,
        "inspect",
        lambda *args, **kwargs: [
            MediaFile(ready, "h264", 10, ready.stat().st_size),
            MediaFile(blocked, "h264", 20, blocked.stat().st_size),
            MediaFile(complete, "h264", 60, complete.stat().st_size),
            MediaFile(hevc, "hevc", 30, hevc.stat().st_size),
        ],
    )

    result = runner.invoke(cli.app, ["status", str(tmp_path), "--json"])

    assert result.exit_code == 0
    totals = json.loads(result.stdout)["totals"]
    assert {group: totals[group]["files"] for group in totals} == {
        "ready": 1,
        "success": 2,
        "blocked": 1,
        "stale": 1,
    }


def test_health_cli_supports_json_quick_check(tmp_path: Path, monkeypatch):
    video = tmp_path / "movie.mkv"
    video.touch()
    monkeypatch.setattr(cli, "discover", lambda *args, **kwargs: [video])
    monkeypatch.setattr(cli, "find_executable", lambda *args, **kwargs: "/usr/bin/tool")
    monkeypatch.setattr(
        cli,
        "check_media_health",
        lambda *args, **kwargs: {
            "path": str(video),
            "mode": "quick",
            "status": "healthy",
            "error": None,
        },
    )

    result = runner.invoke(cli.app, ["health", str(tmp_path), "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["status"] == "healthy"


def test_probe_failure_is_blocked_until_retry(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"video")
    calls = []

    def fail_probe(*args, **kwargs):
        calls.append(1)
        raise cli.BrakeSmithError("cannot decode header")

    monkeypatch.setattr(cli, "find_executable", lambda *args, **kwargs: "/usr/bin/ffprobe")
    monkeypatch.setattr(cli, "probe", fail_probe)

    assert cli.inspect(tmp_path, -1, None, use_cache=False) == []
    assert cli.inspect(tmp_path, -1, None, use_cache=False) == []
    assert len(calls) == 1
    store = cli.FailureStore()
    assert store.active(source)["type"] == "probe"

    monkeypatch.setattr(
        cli,
        "probe",
        lambda *args, **kwargs: MediaFile(source, "h264", 1, source.stat().st_size),
    )

    assert [
        item.path for item in cli.inspect(tmp_path, -1, None, use_cache=False, retry_blocked=True)
    ] == [source]
    resolved = cli.FailureStore().active(source)
    assert resolved["outcome"] == "ready"
    assert resolved["result"] == "probe succeeded"
    assert resolved["log"] is None


def test_run_early_stop_terminates_process_and_keeps_source(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source data")
    item = MediaFile(source, "h264", 1, source.stat().st_size)
    summary = tmp_path / "summary.json"
    processes = []

    class FakeProcess:
        def __init__(self, command, **kwargs):
            self.stdout = ["Encoding: task 1 of 1, 50.00 %\n"]
            self.returncode = None
            self.terminated = False
            output = Path(command[command.index("-o") + 1])
            output.write_bytes(b"x" * source.stat().st_size)
            processes.append(self)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(cli, "inspect", lambda *args, **kwargs: [item])
    monkeypatch.setattr(cli, "find_executable", lambda *args, **kwargs: "/usr/bin/tool")
    monkeypatch.setattr(cli.subprocess, "Popen", FakeProcess)

    result = runner.invoke(
        cli.app,
        [
            "run",
            str(tmp_path),
            "--max-files",
            "1",
            "--format-preset",
            "custom",
            "--unknown-audio",
            "drop",
            "--unknown-subtitles",
            "drop",
            "--replace-source",
            "--stop-when-larger",
            "--summary",
            str(summary),
            "--yes",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0, result.output
    assert processes[0].terminated
    assert source.read_bytes() == b"source data"
    assert not (tmp_path / "movie.x265.mkv.part").exists()
    assert cli.FailureStore().active(source)["type"] == "not-smaller"
    assert json.loads(summary.read_text())[0]["status"] == "kept-source-not-smaller"


def test_quick_health_zero_timeout_disables_timeout(tmp_path: Path, monkeypatch):
    video = tmp_path / "movie.mkv"
    video.touch()
    seen = []
    monkeypatch.setattr(cli, "probe", lambda path, executable, timeout: seen.append(timeout))

    result = cli.check_media_health(video, False, "/usr/bin/ffprobe", None, 0)

    assert result["status"] == "healthy"
    assert seen == [None]


def test_full_health_check_reports_decoder_error(tmp_path: Path, monkeypatch):
    video = tmp_path / "movie.mkv"
    video.touch()
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="decode failed", stdout=""),
    )

    result = cli.check_media_health(video, True, None, "/usr/bin/ffmpeg", 0)

    assert result["status"] == "error"
    assert result["error"] == "decode failed"


def test_language_picker_selects_once_for_whole_batch(tmp_path: Path, monkeypatch):
    items = [
        MediaFile(
            tmp_path / "one.mkv",
            "h264",
            60,
            10,
            audio=[
                Track(1, 1, "audio", "eng"),
                Track(2, 2, "audio", "jpn"),
                Track(3, 3, "audio", "und"),
            ],
        ),
        MediaFile(
            tmp_path / "two.mkv",
            "h264",
            60,
            10,
            audio=[Track(1, 1, "audio", "fra"), Track(2, 2, "audio", "jpn")],
            subtitles=[Track(3, 1, "subtitle", "spa")],
        ),
    ]
    calls = []

    class Answer:
        def __init__(self, choices):
            self.choices = choices

        def ask(self):
            calls.append(self.choices)
            checked = {choice.value: choice.checked for choice in self.choices}
            assert checked == {
                "eng": True,
                "fra": True,
                "jpn": False,
                "spa": False,
                "und": False,
            }
            labels = [choice.title for choice in self.choices]
            assert any(label.startswith("Japanese —") for label in labels)
            assert any(label.startswith("Spanish —") for label in labels)
            return ["eng", "fra", "jpn"]

    monkeypatch.setattr(
        cli.questionary, "checkbox", lambda *args, **kwargs: Answer(kwargs["choices"])
    )

    audio, subtitles, keep_unknown_audio, keep_unknown_subtitles = cli.reconcile_languages(
        items,
        "eng,fra",
        "eng,fra",
        non_interactive=False,
        choose_unknown_audio=True,
        choose_unknown_subtitles=True,
    )

    assert audio == subtitles == ["eng", "fra", "jpn"]
    assert keep_unknown_audio is False
    assert keep_unknown_subtitles is None
    assert len(calls) == 1


def test_only_undefined_language_starts_selected(tmp_path: Path, monkeypatch):
    item = MediaFile(
        tmp_path / "unknown.mkv",
        "h264",
        60,
        10,
        audio=[Track(1, 1, "audio", "und")],
    )

    class Answer:
        def __init__(self, choices):
            self.choices = choices

        def ask(self):
            assert len(self.choices) == 1
            assert self.choices[0].value == "und"
            assert self.choices[0].checked is True
            return ["und"]

    monkeypatch.setattr(
        cli.questionary, "checkbox", lambda *args, **kwargs: Answer(kwargs["choices"])
    )

    audio, subtitles, keep_unknown_audio, keep_unknown_subtitles = cli.reconcile_languages(
        [item],
        "eng,fra",
        "eng,fra",
        non_interactive=False,
        choose_unknown_audio=True,
        choose_unknown_subtitles=True,
    )

    assert audio == subtitles == []
    assert keep_unknown_audio is True
    assert keep_unknown_subtitles is None

    audio, subtitles, keep_unknown_audio, keep_unknown_subtitles = cli.reconcile_languages(
        [item],
        "eng,fra",
        "eng,fra",
        non_interactive=True,
        choose_unknown_audio=False,
        choose_unknown_subtitles=False,
    )
    assert audio == subtitles == ["eng", "fra"]
    assert keep_unknown_audio is keep_unknown_subtitles is None
