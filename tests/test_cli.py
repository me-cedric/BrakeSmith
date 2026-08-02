import json
from pathlib import Path

from typer.testing import CliRunner

from brakesmith import cli
from brakesmith.core import MediaFile

runner = CliRunner()


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


def test_candidates_refuses_to_replace_report(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cli, "inspect", lambda *args, **kwargs: [])
    report = tmp_path / "candidates.txt"
    report.write_text("keep me")
    result = runner.invoke(cli.app, ["candidates", str(tmp_path), "--output", str(report)])
    assert result.exit_code == 2
    assert report.read_text() == "keep me"
