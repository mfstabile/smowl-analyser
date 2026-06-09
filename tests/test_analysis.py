from typer.testing import CliRunner

from smowl_analyser.analysis import analyze_run, looks_like_code, normalize_title, write_analysis_html
from smowl_analyser.cli import app
from smowl_analyser.models import ExtractionReport, RunInfo, SmowData, StudentRecord
from smowl_analyser.storage import RunStorage


def make_student(student_id, name, events):
    student = StudentRecord(id=student_id, name=name, smow=SmowData(computer_monitoring=events))
    return student


def save_run(tmp_path, students):
    storage = RunStorage(runs_dir=tmp_path, run_id="run-1")
    storage.prepare()
    storage.save_report(ExtractionReport(run=RunInfo(id="run-1"), students=students))
    for student in students:
        storage.save_student_computer_monitoring(student)
    return storage


def test_monitoring_relaunch_detection_and_severity(tmp_path):
    storage = save_run(
        tmp_path,
        [
            make_student(
                "student-1",
                "Student One",
                [
                    {"type": "CM_LAUNCHED", "timestamp": "2026-06-01 19:00:00"},
                    {"type": "CM_CLOSED_MANUALLY", "timestamp": "2026-06-01 19:10:00"},
                    {"type": "CM_LAUNCHED", "timestamp": "2026-06-01 19:12:30"},
                ],
            ),
            make_student(
                "student-2",
                "Student Two",
                [
                    {"type": "CM_LAUNCHED", "timestamp": "2026-06-01 19:00:00"},
                    {"type": "CM_CLOSED_MANUALLY", "timestamp": "2026-06-01 19:10:00"},
                ],
            ),
        ],
    )

    result = analyze_run(storage)
    findings = result.findings_by_category("monitoring_relaunch")

    assert len(findings) == 1
    assert findings[0].student_name == "Student One"
    assert findings[0].severity == "high"
    assert findings[0].metadata["interval"] == "2min 30s"


def test_rare_programs_use_distinct_students_and_allowlist(tmp_path):
    storage = save_run(
        tmp_path,
        [
            make_student(
                "student-1",
                "Student One",
                [
                    {"type": "OPENED_PROGRAM", "timestamp": "2026-06-01 19:00:00", "program_name": "TeamViewer"},
                    {"type": "OPENED_PROGRAM", "timestamp": "2026-06-01 19:01:00", "program_name": "TeamViewer"},
                    {"type": "OPENED_PROGRAM", "timestamp": "2026-06-01 19:02:00", "program_name": "python.exe"},
                ],
            ),
            make_student("student-2", "Student Two", [{"type": "OPENED_PROGRAM", "program_name": "Zoom"}]),
            make_student("student-3", "Student Three", [{"type": "OPENED_PROGRAM", "program_name": "Zoom"}]),
            make_student("student-4", "Student Four", []),
            make_student("student-5", "Student Five", []),
        ],
    )

    result = analyze_run(storage)
    program_findings = result.findings_by_category("rare_program")

    assert len(program_findings) == 1
    assert program_findings[0].student_name == "Student One"
    assert program_findings[0].metadata["value"] == "teamviewer"
    assert program_findings[0].metadata["event_count"] == 2


def test_rare_window_titles_are_normalized_and_allowlisted(tmp_path):
    assert normalize_title("Stack Overflow - Pessoal — Microsoft Edge") == "stack overflow"
    storage = save_run(
        tmp_path,
        [
            make_student(
                "student-1",
                "Student One",
                [
                    {
                        "type": "CM_WEB_NAVIGATION_OUTSIDE_EXAM",
                        "timestamp": "2026-06-01 19:00:00",
                        "window_title": "Stack Overflow - Pessoal — Microsoft Edge",
                    }
                ],
            ),
            make_student(
                "student-2",
                "Student Two",
                [
                    {
                        "type": "CM_WEB_NAVIGATION_OUTSIDE_EXAM",
                        "window_title": "Workspace — E2.3 — ENG DESSOFT | PrairieLearn",
                    }
                ],
            ),
            make_student("student-3", "Student Three", []),
            make_student("student-4", "Student Four", []),
            make_student("student-5", "Student Five", []),
        ],
    )

    result = analyze_run(storage)
    title_findings = result.findings_by_category("rare_window_title")

    assert len(title_findings) == 1
    assert title_findings[0].metadata["value"] == "stack overflow"


def test_multiline_code_clipboard_detection(tmp_path):
    assert looks_like_code("def f():\n    return 1")
    storage = save_run(
        tmp_path,
        [
            make_student(
                "student-1",
                "Student One",
                [
                    {
                        "type": "CM_TEXT_PASTED",
                        "timestamp": "2026-06-01 19:00:00",
                        "text_copied_pasted": "def a():\n    x = 1\n    y = 2\n    z = x + y\n    return z",
                    },
                    {
                        "type": "CM_TEXT_COPIED",
                        "timestamp": "2026-06-01 19:02:00",
                        "text_copied_pasted": "linha um\nlinha dois",
                    },
                    {
                        "type": "CM_TEXT_COPIED",
                        "timestamp": "2026-06-01 19:03:00",
                        "text_copied_pasted": "apenas uma linha",
                    },
                ],
            )
        ],
    )

    result = analyze_run(storage)
    findings = result.findings_by_category("multiline_code_clipboard")

    assert [finding.severity for finding in findings] == ["high", "low"]
    assert findings[0].metadata["line_count"] == 5
    assert "def a()" in findings[0].metadata["text_preview"]


def test_analysis_html_and_cli_command(tmp_path):
    storage = save_run(
        tmp_path,
        [
            make_student(
                "student-1",
                "Student One",
                [
                    {"type": "CM_LAUNCHED", "timestamp": "2026-06-01 19:00:00"},
                    {"type": "CM_CLOSED_MANUALLY", "timestamp": "2026-06-01 19:10:00"},
                    {"type": "CM_LAUNCHED", "timestamp": "2026-06-01 19:12:00"},
                ],
            )
        ],
    )
    result = analyze_run(storage)
    output = write_analysis_html(storage, result)

    assert output.exists()
    html = output.read_text(encoding="utf-8")
    assert "Monitoramento fechado e relançado" in html
    assert "Student One" in html

    runner = CliRunner()
    cli_result = runner.invoke(app, ["analyze", "--run-id", "run-1", "--runs-dir", str(tmp_path)])

    assert cli_result.exit_code == 0
    assert "Analysis saved" in cli_result.output
