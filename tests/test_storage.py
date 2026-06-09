from smowl_analyser.models import (
    ExtractedFile,
    ExtractionReport,
    Progress,
    RunInfo,
    Selection,
    SmowData,
    StudentRecord,
)
from smowl_analyser.storage import read_json
from smowl_analyser.storage import RunStorage, safe_slug


def test_safe_slug_removes_sensitive_path_characters():
    assert safe_slug("Aluno / 123: teste") == "Aluno-123-teste"


def test_storage_writes_report_progress_selection_and_file(tmp_path):
    storage = RunStorage(runs_dir=tmp_path, run_id="run-1")
    storage.prepare()

    report = ExtractionReport(
        run=RunInfo(id="run-1"),
        students=[StudentRecord(id="student-1", name="Student One")],
    )
    progress = Progress(run_id="run-1")
    progress.ensure_students(report.students)

    storage.save_report(report)
    storage.save_progress(progress)
    storage.save_selection(Selection())
    stored_file = storage.save_file(
        "student-1",
        "capture.jpg",
        b"image-bytes",
        original_url="https://example.test/capture.jpg",
        mime_type="image/jpeg",
    )

    assert storage.load_report().students[0].name == "Student One"
    assert storage.load_progress().entries["student-1"].status == "pending"
    assert storage.load_selection().course is None
    assert stored_file.sha256
    assert (storage.root / stored_file.local_path).exists()


def test_storage_saves_compact_report_without_computer_monitoring_details(tmp_path):
    storage = RunStorage(runs_dir=tmp_path, run_id="run-1")
    report = ExtractionReport(
        run=RunInfo(id="run-1"),
        students=[
            StudentRecord(
                id="student-1",
                name="Student One",
                smow=SmowData(computer_monitoring=[{"id": "event-1"}]),
                files=[
                    ExtractedFile(
                        original_url="https://example.test/screen.png",
                        local_path="files/student-1/0001-screen.png",
                    )
                ],
            )
        ],
    )

    storage.save_report(report)
    payload = read_json(storage.report_path)

    assert "files" not in payload["students"][0]
    assert "computer_monitoring" not in payload["students"][0]["smow"]
    loaded = storage.load_report()
    assert loaded.students[0].files == []
    assert loaded.students[0].smow.computer_monitoring == []


def test_storage_records_failed_file_without_writing_bytes(tmp_path):
    storage = RunStorage(runs_dir=tmp_path, run_id="run-1")

    failed = storage.failed_file("https://example.test/missing.jpg", "HTTP 404")

    assert failed.status == "failed"
    assert failed.local_path is None
    assert failed.error == "HTTP 404"
