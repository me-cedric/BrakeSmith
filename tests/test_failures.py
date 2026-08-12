import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

import brakesmith.cli as cli_module
from brakesmith import __version__
from brakesmith.cli import (
    app,
    discard_not_smaller,
    exclude_remembered_failures,
    remember_failure,
    remember_not_smaller,
    remember_oversized_partial,
    require_matching_existing_output,
    retryable_plan_status,
)
from brakesmith.core import BrakeSmithError, MediaFile
from brakesmith.failures import HISTORY_LIMIT, FailureStore

runner = CliRunner()


def test_failure_match_uses_source_identity_until_source_changes(tmp_path: Path):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    store = FailureStore(tmp_path / "state")

    store.record(source, "validation", "bad output")

    assert store.active(source)["brakesmith_version"] == __version__
    store.items[store.key(source)]["brakesmith_version"] = "older"
    assert store.active(source) is not None
    source.write_bytes(b"changed source")
    assert store.active(source) is None


def test_policy_change_does_not_release_blocked_outcome(tmp_path: Path):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    store = FailureStore(tmp_path / "state")
    store.record(source, "not-smaller", "large", policy_hash="policy-one")

    assert store.active(source, "policy-one") is not None
    assert store.active(source, "policy-two") is not None


def test_probe_failure_retries_after_policy_change(tmp_path: Path):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    store = FailureStore(tmp_path / "state")
    store.record(source, "probe", "timed out", policy_hash="probe-one")

    assert store.active(source, "probe-one") is not None
    assert store.active(source, "probe-two") is None


def test_failure_cleanup_removes_partial_and_centralizes_log(tmp_path: Path):
    source = tmp_path / "movie.mkv"
    partial = tmp_path / "movie.x265.mkv.part"
    source.write_bytes(b"source")
    partial.write_bytes(b"failed output")
    store = FailureStore(tmp_path / "state")

    log, cleanup_error = remember_failure(
        store,
        source,
        partial,
        "validation",
        "track mismatch",
        ["HandBrake details\n"],
    )

    assert cleanup_error is None
    assert not partial.exists()
    assert log.parent == store.logs
    assert "track mismatch" in log.read_text()
    assert store.active(source)["type"] == "validation"


def test_failure_log_directory_error_is_reported_as_brakesmith_error(tmp_path: Path):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    state = tmp_path / "state"
    state.mkdir()
    (state / "logs").write_text("not a directory")

    store = FailureStore(state)

    try:
        store.record(source, "encode", "failed")
    except BrakeSmithError as error:
        assert "Cannot write failure log" in str(error)
    else:
        raise AssertionError("Expected BrakeSmithError")


def test_registry_directory_error_is_reported_as_brakesmith_error(tmp_path: Path):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    state = tmp_path / "state"
    state.write_text("not a directory")
    store = FailureStore(state)

    with pytest.raises(BrakeSmithError, match="Cannot create failure registry directory"):
        store.record(source, "encode", "failed")


def test_hostile_registry_log_path_never_deletes_media(tmp_path: Path):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    store = FailureStore(tmp_path / "state")
    record = store.record(source, "encode", "failed")
    Path(str(record["log"])).unlink()
    payload = json.loads(store.path.read_text())
    payload["items"][store.key(source)]["log"] = str(source)
    store.path.write_text(json.dumps(payload))

    records_removed, logs_removed = FailureStore(tmp_path / "state").forget([source])

    assert (records_removed, logs_removed) == (1, 0)
    assert source.read_bytes() == b"source"


def test_stale_instances_merge_records_under_lock(tmp_path: Path):
    first = tmp_path / "first.mkv"
    second = tmp_path / "second.mkv"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    state = tmp_path / "state"
    first_store = FailureStore(state)
    stale_store = FailureStore(state)

    first_store.record(first, "encode", "first failure")
    stale_store.record(second, "validation", "second failure")

    assert {record["source"] for record in FailureStore(state).records()} == {
        str(first),
        str(second),
    }


def test_content_fingerprint_detects_same_size_and_timestamp_replacement(tmp_path: Path):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"original")
    store = FailureStore(tmp_path / "state")
    store.record(source, "encode", "failed")
    recorded = source.stat()
    source.write_bytes(b"replaced")
    source.touch()
    source.chmod(recorded.st_mode)
    os.utime(source, ns=(recorded.st_atime_ns, recorded.st_mtime_ns))

    assert source.stat().st_size == recorded.st_size
    assert store.active(source) is None


def test_fingerprint_survives_changed_mount_identity(tmp_path: Path):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    store = FailureStore(tmp_path / "state")
    store.record(source, "encode", "failed")
    record = store.items[store.key(source)]
    record["source_device"] = -1
    record["source_inode"] = -1

    assert store.active(source) is record


def test_unchanged_failure_is_excluded_unless_retry_requested(tmp_path: Path):
    failed = tmp_path / "failed.mkv"
    fresh = tmp_path / "fresh.mkv"
    failed.write_bytes(b"failed")
    fresh.write_bytes(b"fresh")
    items = [
        MediaFile(failed, "h264", 1, failed.stat().st_size),
        MediaFile(fresh, "h264", 1, fresh.stat().st_size),
    ]
    store = FailureStore(tmp_path / "state")
    store.record(failed, "encode", "failed")

    assert exclude_remembered_failures(items, store, False) == [items[1]]
    assert exclude_remembered_failures(items, store, True) == items


def test_existing_success_cannot_be_adopted_for_new_policy(tmp_path: Path):
    source = tmp_path / "movie.mkv"
    output = tmp_path / "movie.x265.mkv"
    source.write_bytes(b"source")
    output.write_bytes(b"output")
    store = FailureStore(tmp_path / "state")
    store.succeeded(source, output, "policy-one", "published")

    with pytest.raises(BrakeSmithError, match="different recorded source or transcode policy"):
        require_matching_existing_output(store, source, output, "policy-two")

    require_matching_existing_output(store, source, output, "policy-one")


def test_successful_keep_source_output_is_not_proposed_for_same_policy(tmp_path: Path):
    source = tmp_path / "movie.mkv"
    output = tmp_path / "movie.x265.mkv"
    source.write_bytes(b"source")
    output.write_bytes(b"output")
    item = MediaFile(source, "h264", 1, source.stat().st_size)
    store = FailureStore(tmp_path / "state")
    store.succeeded(source, output, "policy-one", "published")

    assert exclude_remembered_failures([item], store, False, "policy-one") == []
    assert exclude_remembered_failures([item], store, True, "policy-one") == []
    assert exclude_remembered_failures([item], store, False, "policy-two") == [item]


def test_not_smaller_output_becomes_non_candidate(tmp_path: Path):
    source = tmp_path / "movie.mkv"
    output = tmp_path / "movie.x265.mkv.part"
    source.write_bytes(b"small")
    output.write_bytes(b"larger output")
    store = FailureStore(tmp_path / "state")

    output_size = discard_not_smaller(output, source.stat().st_size)
    assert output_size is not None
    remember_not_smaller(store, source, source.stat().st_size, output_size)

    assert not output.exists()
    assert store.active(source)["type"] == "not-smaller"
    assert str(output_size) in str(store.active(source)["error"])
    assert retryable_plan_status({"status": "completed", "result": "kept-source-not-smaller"})
    assert retryable_plan_status({"status": "failed"})
    assert retryable_plan_status({"status": "cancelled"})
    assert not retryable_plan_status({"status": "completed", "result": "published"})


def test_oversized_partial_cleanup_keeps_source_and_blocks_future_run(tmp_path: Path):
    source = tmp_path / "movie.mkv"
    partial = tmp_path / "movie.x265.mkv.part"
    source.write_bytes(b"source")
    partial.write_bytes(b"larger output")
    store = FailureStore(tmp_path / "state")

    cleanup_error = remember_oversized_partial(
        store,
        source,
        partial,
        source.stat().st_size,
        partial.stat().st_size,
        ["encode output\n"],
        "policy",
        60,
    )

    assert cleanup_error is None
    assert source.read_bytes() == b"source"
    assert not partial.exists()
    assert store.active(source)["type"] == "not-smaller"


def test_oversized_partial_cleanup_error_is_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "movie.mkv"
    partial = tmp_path / "movie.x265.mkv.part"
    source.write_bytes(b"source")
    partial.write_bytes(b"larger output")
    store = FailureStore(tmp_path / "state")
    monkeypatch.setattr(cli_module, "cleanup_partial", lambda path: "permission denied")

    cleanup_error = remember_oversized_partial(
        store,
        source,
        partial,
        source.stat().st_size,
        partial.stat().st_size,
        [],
        "policy",
        60,
    )

    assert cleanup_error == "permission denied"
    assert partial.exists()
    record = store.active(source)
    assert record["type"] == "stale-partial"
    assert record["cleanup_error"] == "permission denied"


def test_repeated_failure_replaces_old_log(tmp_path: Path):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    store = FailureStore(tmp_path / "state")

    old_log = Path(str(store.record(source, "encode", "first")["log"]))
    new_log = Path(str(store.record(source, "encode", "second")["log"]))

    assert not old_log.exists()
    assert new_log.exists()
    assert list(store.logs.glob("*.log")) == [new_log]
    assert len(store.records()[0]["history"]) == 2


def test_success_tracks_output_and_bounded_attempt_history(tmp_path: Path):
    source = tmp_path / "movie.mkv"
    output = tmp_path / "movie.x265.mkv"
    source.write_bytes(b"source")
    output.write_bytes(b"output")
    store = FailureStore(tmp_path / "state")
    for attempt in range(HISTORY_LIMIT + 2):
        store.record(source, "encode", str(attempt))

    store.succeeded(source, output, "policy", "published", duration=60)

    record = store.active(source, "policy")
    assert record["outcome"] == "success"
    assert len(record["history"]) == HISTORY_LIMIT
    output.write_bytes(b"changed output")
    assert store.active(source, "policy") is None


def test_success_can_track_deleted_source(tmp_path: Path):
    source = tmp_path / "movie.mkv"
    output = tmp_path / "movie.x265.mkv"
    source.write_bytes(b"source")
    output.write_bytes(b"output")
    stat = source.stat()
    source.unlink()
    store = FailureStore(tmp_path / "state")

    store.succeeded(
        source,
        output,
        "policy",
        "published",
        source_size=stat.st_size,
        source_modified_ns=stat.st_mtime_ns,
        source_deleted=True,
    )

    assert store.active(source, "policy")["outcome"] == "success"


def test_release_keeps_history_but_unblocks_source(tmp_path: Path):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    store = FailureStore(tmp_path / "state")
    store.record(source, "not-smaller", "large")

    assert store.release([source]) == 1
    assert store.active(source) is None
    assert len(store.records()[0]["history"]) == 1
    assert store.prune()[0] == 0


def test_clear_supports_type_and_logs_only(tmp_path: Path):
    first = tmp_path / "first.mkv"
    second = tmp_path / "second.mkv"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    store = FailureStore(tmp_path / "state")
    validation = store.record(first, "validation", "bad")
    validation_log = Path(str(validation["log"]))
    store.record(second, "encode", "crashed")

    records_removed, logs_removed = store.clear("validation", logs_only=True)
    assert (records_removed, logs_removed) == (0, 1)
    assert store.records("validation")[0]["log"] is None
    assert not validation_log.exists()

    records_removed, logs_removed = store.clear("validation")
    assert (records_removed, logs_removed) == (1, 0)
    assert len(store.records()) == 1
    assert store.records()[0]["type"] == "encode"


def test_clear_failures_preserves_success_history(tmp_path: Path):
    failed = tmp_path / "failed.mkv"
    complete = tmp_path / "complete.mkv"
    output = tmp_path / "complete.x265.mkv"
    failed.write_bytes(b"failed")
    complete.write_bytes(b"complete")
    output.write_bytes(b"output")
    store = FailureStore(tmp_path / "state")
    store.record(failed, "encode", "failed")
    store.succeeded(complete, output, None, "published")

    records_removed, _ = store.clear()

    assert records_removed == 1
    assert [record["outcome"] for record in store.records()] == ["success"]


def test_prune_removes_stale_records_and_orphan_logs(tmp_path: Path):
    active = tmp_path / "active.mkv"
    changed = tmp_path / "changed.mkv"
    missing = tmp_path / "missing.mkv"
    for source in (active, changed, missing):
        source.write_bytes(b"source")
    store = FailureStore(tmp_path / "state")
    store.record(active, "encode", "active")
    store.record(changed, "encode", "changed")
    store.record(missing, "encode", "missing")
    changed.write_bytes(b"new source")
    missing.unlink()
    orphan = store.logs / "orphan.log"
    orphan.write_text("orphan")

    records_removed, logs_removed = store.prune()

    assert (records_removed, logs_removed) == (2, 3)
    assert [record["source"] for record in store.records()] == [str(active)]
    assert not orphan.exists()


def test_failure_cli_lists_and_clears_by_type(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    FailureStore().record(source, "validation", "track mismatch")

    listed = runner.invoke(
        app,
        ["failures", "list", "--type", "validation"],
        terminal_width=240,
    )
    assert listed.exit_code == 0
    assert "movie.mkv" in listed.stdout
    assert "validation" in listed.stdout

    cleared = runner.invoke(
        app,
        ["failures", "clear", "--type", "validation", "--logs-only", "--yes"],
    )
    assert cleared.exit_code == 0
    assert "0 record(s), 1 log(s)" in cleared.stdout
    assert len(FailureStore().records()) == 1

    invalid = runner.invoke(app, ["failures", "list", "--type", "typo"])
    assert invalid.exit_code == 2
    assert "--type must be one of" in invalid.stdout


def test_failure_cli_forgets_one_source_and_prunes_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    retry = tmp_path / "retry.mkv"
    missing = tmp_path / "missing.mkv"
    retry.write_bytes(b"retry")
    missing.write_bytes(b"missing")
    store = FailureStore()
    store.record(retry, "not-smaller", "too large")
    store.record(missing, "encode", "failed")
    missing.unlink()

    forgotten = runner.invoke(app, ["failures", "forget", str(retry)])
    pruned = runner.invoke(app, ["failures", "prune"])

    assert forgotten.exit_code == 0
    assert "Forgot: 1 source(s), 1 log(s)." in forgotten.stdout
    assert pruned.exit_code == 0
    assert "Pruned: 1 stale record(s), 1 orphan log(s)." in pruned.stdout
    assert FailureStore().records() == []


def test_retry_cli_releases_matching_type_without_deleting_history(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    FailureStore().record(source, "not-smaller", "large")

    result = runner.invoke(app, ["retry", "--type", "not-smaller"])

    assert result.exit_code == 0
    assert "Ready to retry: 1 source(s)" in result.stdout
    store = FailureStore()
    assert store.active(source) is None
    assert len(store.records()[0]["history"]) == 1


def test_history_cli_lists_success_and_failure_attempts(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    source = tmp_path / "movie.mkv"
    output = tmp_path / "movie.x265.mkv"
    source.write_bytes(b"source")
    output.write_bytes(b"output")
    store = FailureStore()
    store.record(source, "encode", "failed")
    store.succeeded(source, output, "policy", "published")

    result = runner.invoke(app, ["history", str(tmp_path), "--json"])

    assert result.exit_code == 0
    attempts = json.loads(result.stdout)
    assert [attempt["outcome"] for attempt in attempts] == ["success", "failed"]
