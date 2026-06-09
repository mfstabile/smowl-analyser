from smowl_analyser.models import ExtractionReport, LinkOption, RunInfo, StudentRecord, jsonable


def test_report_serializes_to_jsonable_dict():
    report = ExtractionReport(
        run=RunInfo(
            id="run-1",
            course=LinkOption(id="course-1", title="Course", url="https://example.test/course"),
        ),
        students=[StudentRecord(id="123", name="Student Name")],
    )

    payload = jsonable(report)

    assert payload["run"]["id"] == "run-1"
    assert payload["students"][0]["name"] == "Student Name"
    assert "triage" not in payload["students"][0]
