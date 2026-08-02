import json
from pathlib import Path

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


def test_candidates_refuses_to_replace_report(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cli, "inspect", lambda *args, **kwargs: [])
    report = tmp_path / "candidates.txt"
    report.write_text("keep me")
    result = runner.invoke(cli.app, ["candidates", str(tmp_path), "--output", str(report)])
    assert result.exit_code == 2
    assert report.read_text() == "keep me"


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
