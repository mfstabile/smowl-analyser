from pathlib import Path

from smowl_analyser.extractors import smow as smow_module
from smowl_analyser.extractors.blackboard import (
    discover_course_cards,
    parse_courses_from_html,
    parse_smow_activities_from_html,
)
from smowl_analyser.extractors.smow import (
    collect_student_evidence_requests,
    computer_monitoring_user_ids_from_responses,
    discover_reports_from_responses,
    extract_report,
    parse_file_references_from_html,
    parse_flags_from_html,
    parse_students_from_responses,
    parse_reports_from_html,
    parse_student_detail_from_html,
    parse_students_from_html,
)
from smowl_analyser.models import StudentRecord
from smowl_analyser.models import LinkOption, Selection
from smowl_analyser.storage import RunStorage, read_json


FIXTURES = Path(__file__).parent / "fixtures"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_blackboard_course_and_activity_parsing():
    courses = parse_courses_from_html(
        read_fixture("blackboard_course_list.html"),
        "https://blackboard.example.test",
    )
    activities = parse_smow_activities_from_html(
        read_fixture("course_with_smow_activity.html"),
        "https://blackboard.example.test",
    )

    assert courses[0].title == "Algoritmos - 2026.1"
    assert activities[0].title == "SMOW - Prova Final"
    assert activities[1].title == "Smowl Proctoring Tool"
    assert activities[1].metadata["action"] == "click"


def test_blackboard_main_navigation_is_not_treated_as_courses():
    courses = parse_courses_from_html(
        read_fixture("blackboard_main_nav.html"),
        "https://insper.blackboard.com",
    )

    assert courses == []


def test_blackboard_course_cards_are_parsed_without_links():
    courses = parse_courses_from_html(
        read_fixture("blackboard_course_cards.html"),
        "https://insper.blackboard.com/ultra/course",
    )

    assert courses == []


def test_discover_course_cards_maps_playwright_results_to_click_options():
    class FakePage:
        url = "https://insper.blackboard.com/ultra/course"

        def evaluate(self, _script):
            return [
                {
                    "index": 1,
                    "code": "202661.GRENG_201561_0007.DESIGNSOFT_1A",
                    "title": "DESIGN DE SOFTWARE - 1A - 2026/61",
                }
            ]

    courses = discover_course_cards(FakePage())

    assert courses[0].title == (
        "202661.GRENG_201561_0007.DESIGNSOFT_1A | DESIGN DE SOFTWARE - 1A - 2026/61"
    )
    assert courses[0].metadata["action"] == "click-course-card"
    assert courses[0].metadata["selector"] == "[data-smowl-course-open='1']"


def test_discover_course_cards_prefers_real_course_url_when_available():
    class FakePage:
        url = "https://insper.blackboard.com/ultra/course"

        def evaluate(self, _script):
            return [
                {
                    "index": 1,
                    "code": "202661.GRENG_201561_0007.DESIGNSOFT_1A",
                    "title": "DESIGN DE SOFTWARE - 1A - 2026/61",
                    "url": "https://insper.blackboard.com/ultra/courses/_123_1/outline",
                }
            ]

    courses = discover_course_cards(FakePage())

    assert courses[0].url == "https://insper.blackboard.com/ultra/courses/_123_1/outline"
    assert courses[0].metadata["course_url"] == courses[0].url
    assert "action" not in courses[0].metadata


def test_smow_report_and_students_parsing():
    html = read_fixture("smow_report.html")

    reports = parse_reports_from_html(html, "https://smow.example.test")
    students = parse_students_from_html(html, "https://smow.example.test")

    assert reports[0].title == "Relatório de alunos"
    assert len(students) == 2
    assert students[0].registration == "12345"
    assert students[0].source_url == "https://smow.example.test/smow/students/joao"


def test_student_detail_flags_and_files_parsing():
    html = read_fixture("student_detail.html")
    student = StudentRecord(id="12345", name="João Silva")

    updated = parse_student_detail_from_html(html, student, "https://smow.example.test")
    files = parse_file_references_from_html(html, "https://smow.example.test")
    flags = parse_flags_from_html(html, "https://smow.example.test")

    assert len(updated.smow.flags) >= 1
    assert len(files) == 2
    assert len(flags) >= 1
    assert files[0].original_url == "https://smow.example.test/smow/files/capture-1.jpg"


def test_activity_without_report_returns_empty_list():
    reports = parse_reports_from_html(
        read_fixture("activity_without_report.html"),
        "https://smow.example.test",
    )

    assert reports == []


def test_student_without_files_returns_empty_file_list():
    files = parse_file_references_from_html(
        read_fixture("student_without_files.html"),
        "https://smow.example.test",
    )

    assert files == []


def test_smow_api_responses_become_reports_and_students():
    responses = [
        {
            "url": "https://lti-smowl-global.smowltech.net/lti/ajax/activities?state=abc",
            "payload": [
                {
                    "activityId": "usmwl1704516usmwl1",
                    "displayName": "Avaliação Final",
                    "lmsActivityId": "_1704516_1",
                    "enabled": True,
                    "numberUsers": "2",
                    "flags": {"frontCamera": {"enabled": True}},
                }
            ],
        },
        {
            "url": "https://lti-smowl-global.smowltech.net/lti/ajax/students?state=abc",
            "payload": {"student-1": "Student One"},
        },
        {
            "url": "https://results-api.smowltech.net/index.php/V2/results/figures",
            "payload": {
                "users": [
                    {
                        "userId": "student-1",
                        "globalStatus": "UNSUCCESSFUL",
                        "activities": {
                            "testusmwl1704516usmwl1": {
                                "FrontCamera": {
                                    "status": "UNSUCCESSFUL",
                                    "figures": {"MORE_THAN_ONE": 2, "NOBODY": 0},
                                }
                            }
                        },
                    }
                ]
            },
        },
        {
            "url": "https://results-api.smowltech.net/index.php/V2/monitoring/evidence/frontCamera",
            "payload": {
                "activityName": "testusmwl1704516usmwl1",
                "userId": "student-1",
                "evidence": {
                    "FrontCamera": [
                        {"date": "2026-06-01", "src": "https://example.test/front.jpg"}
                    ]
                },
            },
        },
    ]

    reports = discover_reports_from_responses(responses)
    students = parse_students_from_responses(responses, activity_id="usmwl1704516usmwl1")

    assert reports[0].title == "Avaliação Final"
    assert reports[0].url == "smow-api://activity/usmwl1704516usmwl1"
    assert students[0].id == "student-1"
    assert students[0].status == "UNSUCCESSFUL"
    assert students[0].source_url == "smow-api:/results/figures"
    assert students[0].files[0].original_url == "https://example.test/front.jpg"
    assert any(event.label == "FrontCamera.MORE_THAN_ONE" for event in students[0].smow.events)


def test_smow_api_responses_collect_files_from_all_known_payload_shapes():
    responses = [
        {
            "url": "https://lti-smowl-global.smowltech.net/lti/ajax/students?state=abc",
            "payload": {"student-1": "Student One"},
        },
        {
            "url": "https://results-api.smowltech.net/index.php/V2/results/reasons/allActiveServices",
            "request_headers": {
                "accept": "application/json",
                "authorization": "Bearer token",
                "content-type": "application/x-www-form-urlencoded",
            },
            "payload": {
                "users": [
                    {
                        "userId": "student-1",
                        "globalStatus": "UNSUCCESSFUL",
                        "activities": {"testactivity-1": {"FrontCamera": {"status": "UNSUCCESSFUL"}}},
                    }
                ]
            },
        },
        {
            "url": "https://results-api.smowltech.net/index.php/V2/registers/status/user/includePhotos",
            "payload": {
                "user": {"userId": "student-1"},
                "images": {
                    "RegisterImage": {
                        "src": "https://smowlireland.s3.eu-west-1.amazonaws.com/Register/student-1.jpg"
                    }
                },
            },
        },
        {
            "url": "https://results-api.smowltech.net/index.php/V2/resultsReasonsCaptures/frontCamera",
            "payload": {
                "userId": "student-1",
                "activities": {
                    "testactivity-1": {
                        "FrontCamera": {
                            "issues": {
                                "MORE_THAN_ONE": {
                                    "captures": [
                                        {
                                            "src": (
                                                "https://smowlireland.s3.eu-west-1.amazonaws.com/"
                                                "Images/student-1/capture.jpg"
                                            )
                                        }
                                    ]
                                }
                            }
                        }
                    }
                },
            },
        },
        {
            "url": "https://results-api.smowltech.net/index.php/V2/monitoring/evidence/computerMonitoring",
            "payload": {
                "activityName": "testactivity-1",
                "userId": "student-1",
                "evidence": [
                    {
                        "detail": {
                            "desktopScreenshots": [
                                {
                                    "src": (
                                        "https://smowl-prod-cm.s3.eu-west-1.amazonaws.com/"
                                        "Images/student-1/screen.png"
                                    )
                                }
                            ]
                        }
                    }
                ],
            },
        },
    ]

    students = parse_students_from_responses(responses, activity_id="activity-1")

    urls = [file.original_url for file in students[0].files]
    assert urls == [
        "https://smowlireland.s3.eu-west-1.amazonaws.com/Register/student-1.jpg",
        "https://smowlireland.s3.eu-west-1.amazonaws.com/Images/student-1/capture.jpg",
        "https://smowl-prod-cm.s3.eu-west-1.amazonaws.com/Images/student-1/screen.png",
    ]
    assert students[0].files[2].mime_type == "image/png"
    assert students[0].smow.computer_monitoring == [
        {
            "id": None,
            "timestamp": None,
            "incident": None,
            "issue": None,
            "type": None,
            "program_name": None,
            "window_title": None,
            "text_copied_pasted": None,
            "detail": {
                "desktopScreenshots": [
                    {
                        "src": (
                            "https://smowl-prod-cm.s3.eu-west-1.amazonaws.com/"
                            "Images/student-1/screen.png"
                        )
                    }
                ]
            },
            "screenshots": [
                {
                    "source": "detail.desktopScreenshots[0].src",
                    "original_url": (
                        "https://smowl-prod-cm.s3.eu-west-1.amazonaws.com/"
                        "Images/student-1/screen.png"
                    ),
                    "local_path": None,
                    "mime_type": "image/png",
                    "size_bytes": None,
                    "sha256": None,
                    "status": "pending",
                    "error": None,
                }
            ],
            "source_url": "smow-api:/monitoring/evidence/computerMonitoring",
        }
    ]


def test_smow_api_file_collection_deduplicates_signed_image_urls_by_path():
    responses = [
        {
            "url": "https://lti-smowl-global.smowltech.net/lti/ajax/students?state=abc",
            "payload": {"student-1": "Student One"},
        },
        {
            "url": "https://results-api.smowltech.net/index.php/V2/results/reasons/allActiveServices",
            "payload": {
                "users": [
                    {
                        "userId": "student-1",
                        "activities": {"testactivity-1": {"ComputerMonitoring": {"status": "UNSUCCESSFUL"}}},
                    }
                ]
            },
        },
        {
            "url": "https://results-api.smowltech.net/index.php/V2/monitoring/evidence/computerMonitoring",
            "payload": {
                "activityName": "testactivity-1",
                "userId": "student-1",
                "evidence": [
                    {
                        "id": "event-1",
                        "src": "https://smowl-prod-cm.s3.eu-west-1.amazonaws.com/Images/student-1/screen.png?sig=1",
                    },
                    {
                        "id": "event-2",
                        "src": "https://smowl-prod-cm.s3.eu-west-1.amazonaws.com/Images/student-1/screen.png?sig=2",
                    },
                ],
            },
        },
    ]

    students = parse_students_from_responses(responses, activity_id="activity-1")

    assert len(students[0].files) == 1
    assert students[0].files[0].original_url.endswith("screen.png?sig=1")
    assert len(students[0].smow.computer_monitoring) == 2


def test_smow_results_reasons_computer_monitoring_payload_becomes_events():
    image_url = "https://smowl-prod-cm.s3.eu-west-1.amazonaws.com/Images/student-1/screen.jpg"
    responses = [
        {
            "url": "https://lti-smowl-global.smowltech.net/lti/ajax/students?state=abc",
            "payload": {"student-1": "Student One"},
        },
        {
            "url": "https://results-api.smowltech.net/index.php/V2/results/reasons/allActiveServices",
            "request_headers": {
                "accept": "application/json",
                "authorization": "Bearer token",
                "content-type": "application/x-www-form-urlencoded",
            },
            "payload": {
                "users": [
                    {
                        "userId": "student-1",
                        "globalStatus": "UNSUCCESSFUL",
                        "activities": {"testactivity-1": {"ComputerMonitoring": {"status": "UNSUCCESSFUL"}}},
                    }
                ]
            },
        },
        {
            "url": "https://results-api.smowltech.net/index.php/V2/resultsReasonsCaptures/computerMonitoring",
            "payload": {
                "userId": "student-1",
                "activities": {
                    "testactivity-1": {
                        "ComputerMonitoring": {
                            "issues": {
                                "COMMANDS": {
                                    "captures": [
                                        {
                                            "date": "2026-06-01 19:01:07",
                                            "detail": {
                                                "programName": "Google Chrome (macOS)",
                                                "textCopiedPasted": "abc",
                                                "type": "CM_TEXT_COPIED",
                                                "windowTitle": "",
                                            },
                                            "id": "event-1",
                                            "incident": True,
                                            "src": image_url,
                                        }
                                    ]
                                }
                            }
                        }
                    }
                },
            },
        },
    ]

    students = parse_students_from_responses(responses, activity_id="activity-1")

    event = students[0].smow.computer_monitoring[0]
    assert event["issue"] == "COMMANDS"
    assert event["type"] == "CM_TEXT_COPIED"
    assert event["text_copied_pasted"] == "abc"
    assert event["screenshots"][0]["original_url"] == image_url
    assert students[0].files[0].original_url == image_url


def test_extract_report_saves_computer_monitoring_json_with_linked_images(tmp_path):
    image_url = "https://smowl-prod-cm.s3.eu-west-1.amazonaws.com/Images/student-1/screen.png"
    stale_image_url = "https://smowl-prod-cm.s3.eu-west-1.amazonaws.com/Images/student-1/screen.png?expired=1"

    class FakeResponse:
        ok = True
        status = 200
        headers = {"content-type": "image/png"}

        def body(self):
            return b"png-bytes"

    class FakeApiResponse:
        ok = True
        status = 200

        def json(self):
            return {
                "activityName": "testactivity-1",
                "userId": "student-1",
                "evidence": [
                    {
                        "date": "2026-06-01 19:01:07",
                        "id": "event-1",
                        "incident": True,
                        "detail": {
                            "programName": "Google Chrome (macOS)",
                            "textCopiedPasted": "abc",
                            "type": "CM_TEXT_COPIED",
                            "windowTitle": "",
                        },
                        "src": image_url,
                    }
                ],
            }

    class FakeRequest:
        def __init__(self):
            self.posts: list[dict] = []

        def post(self, url, **kwargs):
            self.posts.append({"url": url, **kwargs})
            return FakeApiResponse()

        def get(self, url):
            assert url == image_url
            return FakeResponse()

    class FakeKeyboard:
        def press(self, _key):
            return None

    class FakeFrame:
        name = "smowlresults"
        url = "https://front-results.smowltech.net/index.php/ActivityStatus"

        def evaluate(self, _script, _payload):
            return False

    class FakePage:
        url = "https://front-results.smowltech.net/index.php/ActivityStatus"
        frames = [FakeFrame()]
        keyboard = FakeKeyboard()

        def __init__(self):
            self.request = FakeRequest()

        def wait_for_timeout(self, _timeout_ms):
            return None

        def content(self):
            return ""

    responses = [
        {
            "url": "https://lti-smowl-global.smowltech.net/lti/ajax/students?state=abc",
            "payload": {"student-1": "Student One"},
        },
        {
            "url": "https://results-api.smowltech.net/index.php/V2/results/reasons/allActiveServices",
            "request_headers": {
                "accept": "application/json",
                "authorization": "Bearer token",
                "content-type": "application/x-www-form-urlencoded",
            },
            "payload": {
                "users": [
                    {
                        "userId": "student-1",
                        "globalStatus": "UNSUCCESSFUL",
                        "activities": {"testactivity-1": {"ComputerMonitoring": {"status": "UNSUCCESSFUL"}}},
                    }
                ]
            },
        },
        {
            "url": "https://results-api.smowltech.net/index.php/V2/monitoring/evidence/computerMonitoring",
            "payload": {
                "activityName": "testactivity-1",
                "userId": "student-1",
                "evidence": [
                    {
                        "date": "2026-06-01 19:01:07",
                        "id": "event-1",
                        "incident": True,
                        "detail": {
                            "programName": "Google Chrome (macOS)",
                            "textCopiedPasted": "abc",
                            "type": "CM_TEXT_COPIED",
                            "windowTitle": "",
                        },
                        "src": stale_image_url,
                    }
                ],
            },
        },
    ]
    storage = RunStorage(runs_dir=tmp_path)
    storage.prepare()
    selection = Selection(
        report=LinkOption(
            id="current-smow-student-list",
            title="SMOW Results",
            url=FakePage.url,
            metadata={"source": "current-page", "activity_id": "activity-1"},
        )
    )

    page = FakePage()
    result = extract_report(page, selection, storage, api_responses=responses, download_workers=1)

    student = result.report.students[0]
    assert result.report.students[0].smow.computer_monitoring
    assert any(
        post.get("headers", {}).get("authorization") == "Bearer token"
        for post in page.request.posts
    )
    computer_json = storage.student_computer_monitoring_path(student.id)
    assert computer_json.exists()
    saved = computer_json.read_text(encoding="utf-8")
    assert "CM_TEXT_COPIED" in saved
    assert '"global_status": "UNSUCCESSFUL"' in saved
    assert '"ComputerMonitoring": "UNSUCCESSFUL"' in saved
    assert "files/student-1/0001-screen.png" in saved
    assert (storage.root / "files/student-1/0001-screen.png").read_bytes() == b"png-bytes"
    report_json = read_json(storage.report_path)
    assert "files" not in report_json["students"][0]
    assert "computer_monitoring" not in report_json["students"][0]["smow"]


def test_extract_report_downloads_images_immediately_after_each_student_json(
    tmp_path,
    monkeypatch,
):
    class FakeApiResponse:
        ok = True
        status = 200

        def __init__(self, user_id):
            self.user_id = user_id

        def json(self):
            return {
                "activityName": "testactivity-1",
                "userId": self.user_id,
                "evidence": [
                    {
                        "id": f"event-{self.user_id}",
                        "src": f"https://smowl-prod-cm.s3.eu-west-1.amazonaws.com/{self.user_id}/screen.png",
                    }
                ],
            }

    class FakeRequest:
        def post(self, _url, **kwargs):
            payload = kwargs.get("form") or kwargs.get("data") or kwargs.get("json") or {}
            return FakeApiResponse(payload.get("userId"))

    class FakePage:
        url = "https://front-results.smowltech.net/index.php/ActivityStatus"
        frames = []
        request = FakeRequest()

    storage = RunStorage(runs_dir=tmp_path)
    storage.prepare()
    responses = [
        {
            "url": "https://lti-smowl-global.smowltech.net/lti/ajax/students?state=abc",
            "payload": {"student-1": "Student One", "student-2": "Student Two"},
        },
        {
            "url": "https://results-api.smowltech.net/index.php/V2/results/reasons/allActiveServices",
            "request_headers": {"authorization": "Bearer token"},
            "payload": {
                "users": [
                    {
                        "userId": "student-1",
                        "activities": {"testactivity-1": {"ComputerMonitoring": {"status": "UNSUCCESSFUL"}}},
                    },
                    {
                        "userId": "student-2",
                        "activities": {"testactivity-1": {"ComputerMonitoring": {"status": "UNSUCCESSFUL"}}},
                    },
                ]
            },
        },
    ]
    selection = Selection(
        report=LinkOption(
            id="current-smow-student-list",
            title="SMOW Results",
            url=FakePage.url,
            metadata={"source": "current-page", "activity_id": "activity-1"},
        )
    )
    started_downloads = []

    def fake_parallel_download(task, run_storage):
        assert run_storage.student_computer_monitoring_path("student-1").exists()
        if task.student_id == "student-1":
            assert not run_storage.student_computer_monitoring_path("student-2").exists()
        if task.student_id == "student-2":
            assert run_storage.student_computer_monitoring_path("student-2").exists()
        started_downloads.append(task.student_id)
        return run_storage.save_file(
            task.student_id,
            f"{task.index:04d}-screen.png",
            b"png-bytes",
            original_url=task.file_ref.original_url,
            mime_type="image/png",
        )

    monkeypatch.setattr(smow_module, "_download_file_with_urllib", fake_parallel_download)

    result = extract_report(
        FakePage(),
        selection,
        storage,
        api_responses=responses,
        download_workers=2,
    )

    assert started_downloads == ["student-1", "student-2"]
    assert all(student.files[0].status == "downloaded" for student in result.report.students)
    assert "files/student-1/0001-screen.png" in storage.student_computer_monitoring_path(
        "student-1"
    ).read_text(encoding="utf-8")
    assert "files/student-2/0001-screen.png" in storage.student_computer_monitoring_path(
        "student-2"
    ).read_text(encoding="utf-8")


def test_extract_report_filters_to_single_student(tmp_path, monkeypatch):
    class FakeApiResponse:
        ok = True
        status = 200

        def __init__(self, user_id):
            self.user_id = user_id

        def json(self):
            return {
                "activityName": "testactivity-1",
                "userId": self.user_id,
                "evidence": [
                    {
                        "id": f"event-{self.user_id}",
                        "src": f"https://smowl-prod-cm.s3.eu-west-1.amazonaws.com/{self.user_id}/screen.png",
                    }
                ],
            }

    class FakeRequest:
        def post(self, _url, **kwargs):
            payload = kwargs.get("form") or kwargs.get("data") or {}
            return FakeApiResponse(payload.get("userId"))

    class FakePage:
        url = "https://front-results.smowltech.net/index.php/ActivityStatus"
        frames = []
        request = FakeRequest()

    responses = [
        {
            "url": "https://lti-smowl-global.smowltech.net/lti/ajax/students?state=abc",
            "payload": {"student-1": "Student One", "student-2": "Student Two"},
        },
        {
            "url": "https://results-api.smowltech.net/index.php/V2/results/figures",
            "request_headers": {"authorization": "Bearer token"},
            "payload": {
                "users": [
                    {"userId": "student-1", "activities": {"testactivity-1": {}}},
                    {"userId": "student-2", "activities": {"testactivity-1": {}}},
                ]
            },
        },
    ]
    selection = Selection(
        report=LinkOption(
            id="current-smow-student-list",
            title="SMOW Results",
            url=FakePage.url,
            metadata={"source": "current-page", "activity_id": "activity-1"},
        )
    )
    storage = RunStorage(runs_dir=tmp_path)
    storage.prepare()

    def fake_parallel_download(task, run_storage):
        return run_storage.save_file(
            task.student_id,
            f"{task.index:04d}-screen.png",
            b"png-bytes",
            original_url=task.file_ref.original_url,
            mime_type="image/png",
        )

    monkeypatch.setattr(smow_module, "_download_file_with_urllib", fake_parallel_download)

    result = extract_report(
        FakePage(),
        selection,
        storage,
        api_responses=responses,
        student_ids={"student-2"},
        download_workers=2,
    )

    assert [student.id for student in result.report.students] == ["student-2"]
    assert storage.student_computer_monitoring_path("student-2").exists()
    assert not storage.student_computer_monitoring_path("student-1").exists()
    assert (storage.root / "files/student-2/0001-screen.png").exists()


def test_extract_report_creates_single_student_from_computer_monitoring_payload(tmp_path):
    class FakePage:
        url = "https://front-results.smowltech.net/index.php/ActivityStatus"
        frames = []

    responses = [
        {
            "url": "https://results-api.smowltech.net/index.php/V2/monitoring/evidence/computerMonitoring",
            "payload": {
                "activityName": "testactivity-1",
                "userId": "student-1",
                "evidence": [
                    {
                        "id": "event-1",
                        "date": "2026-06-04 10:00:00",
                        "detail": {"type": "CM_CLOSED_MANUALLY"},
                    }
                ],
            },
        }
    ]
    selection = Selection(
        report=LinkOption(
            id="current-smow-student",
            title="SMOW Student",
            url=FakePage.url,
            metadata={"source": "current-page"},
        )
    )
    storage = RunStorage(runs_dir=tmp_path)
    storage.prepare()

    result = extract_report(
        FakePage(),
        selection,
        storage,
        api_responses=responses,
        student_ids={"student-1"},
        download_workers=1,
    )

    assert [student.id for student in result.report.students] == ["student-1"]
    assert result.report.students[0].name == "student-1"
    assert result.report.students[0].smow.computer_monitoring[0]["type"] == "CM_CLOSED_MANUALLY"
    assert storage.student_computer_monitoring_path("student-1").exists()


def test_computer_monitoring_user_ids_from_responses_preserves_capture_order():
    responses = [
        {
            "url": "https://results-api.smowltech.net/index.php/V2/monitoring/evidence/computerMonitoring",
            "payload": {"userId": "student-2", "evidence": []},
        },
        {
            "url": "https://results-api.smowltech.net/index.php/V2/resultsReasonsCaptures/computerMonitoring",
            "payload": {"userId": "student-1", "activities": {}},
        },
        {
            "url": "https://results-api.smowltech.net/index.php/V2/monitoring/evidence/computerMonitoring",
            "payload": {"userId": "student-2", "evidence": []},
        },
    ]

    assert computer_monitoring_user_ids_from_responses(responses) == ["student-2", "student-1"]


def test_extract_report_resume_hydrates_computer_monitoring_from_student_json(
    tmp_path,
    monkeypatch,
):
    class FakePage:
        url = "https://front-results.smowltech.net/index.php/ActivityStatus"
        frames = []

        def content(self):
            return ""

    storage = RunStorage(runs_dir=tmp_path, run_id="run-1")
    storage.prepare()
    selection = Selection(
        report=LinkOption(
            id="current-smow-student-list",
            title="SMOW Results",
            url=FakePage.url,
            metadata={"source": "current-page", "activity_id": "activity-1"},
        )
    )
    report = smow_module.ExtractionReport(
        run=smow_module.RunInfo(id="run-1", report=selection.report),
        students=[StudentRecord(id="student-1", name="Student One")],
    )
    progress = smow_module.Progress(run_id="run-1")
    progress.ensure_students(report.students)
    progress.mark_done("student-1")
    storage.save_report(report)
    storage.save_progress(progress)
    student_with_json = StudentRecord(id="student-1", name="Student One")
    student_with_json.smow.computer_monitoring = [
        {
            "id": "event-1",
            "screenshots": [
                {
                    "original_url": "https://smowl-prod-cm.s3.eu-west-1.amazonaws.com/student-1/screen.png",
                    "status": "pending",
                }
            ],
        }
    ]
    storage.save_student_computer_monitoring(student_with_json)
    started_downloads = []

    def fake_parallel_download(task, run_storage):
        started_downloads.append(task.student_id)
        return run_storage.save_file(
            task.student_id,
            f"{task.index:04d}-screen.png",
            b"png-bytes",
            original_url=task.file_ref.original_url,
            mime_type="image/png",
        )

    monkeypatch.setattr(smow_module, "_download_file_with_urllib", fake_parallel_download)

    result = extract_report(
        FakePage(),
        selection,
        storage,
        existing_report=storage.load_report(),
        existing_progress=storage.load_progress(),
        download_workers=2,
    )

    assert started_downloads == ["student-1"]
    assert result.report.students[0].files[0].status == "downloaded"
    assert "files/student-1/0001-screen.png" in storage.student_computer_monitoring_path(
        "student-1"
    ).read_text(encoding="utf-8")


def test_smow_all_active_services_responses_become_student_events():
    responses = [
        {
            "url": "https://lti-smowl-global.smowltech.net/lti/ajax/students?state=abc",
            "payload": {"student-1": "Student One"},
        },
        {
            "url": "https://results-api.smowltech.net/index.php/V2/results/reasons/allActiveServices",
            "payload": {
                "users": [
                    {
                        "userId": "student-1",
                        "globalStatus": "UNSUCCESSFUL",
                        "activities": {
                            "testactivity-1": {
                                "FrontCamera": {
                                    "status": "UNSUCCESSFUL",
                                    "issues": {
                                        "MORE_THAN_ONE": {
                                            "captures_total": 6,
                                            "filters_total": 1,
                                        }
                                    },
                                },
                                "Audio": {"status": "SUCCESSFUL"},
                            }
                        },
                    }
                ]
            },
        },
    ]

    students = parse_students_from_responses(responses, activity_id="activity-1")

    assert students[0].status == "UNSUCCESSFUL"
    assert students[0].smow.service_statuses == {
        "FrontCamera": "UNSUCCESSFUL",
        "Audio": "SUCCESSFUL",
    }
    assert students[0].source_url == "smow-api:/results/reasons/allActiveServices"
    assert any(
        event.label == "FrontCamera.MORE_THAN_ONE.captures_total" and event.value == "6"
        for event in students[0].smow.events
    )
    assert any(event.label == "Audio" and event.value == "SUCCESSFUL" for event in students[0].smow.events)


def test_smow_dashboard_table_preserves_full_names_and_tool_counts():
    html = """
    <table id="dashboard-datatable">
      <tr>
        <th>Status</th><th>User ID</th><th>Username</th>
        <th>Frontal</th><th>Computer</th><th>Audio</th>
      </tr>
      <tr data-iduser="abc123" data-isblocked="false">
        <td id="dt-status" data-filter="issue noReviewed toolIssue" data-sort="2"></td>
        <td id="dt-id"><span title="abc123">abc...</span></td>
        <td id="dt-username"><span title="ANA LUIZA MARSILIO ALVES">ANA LUIZA...</span></td>
        <td id="dt-FrontCamera" data-filter="issue" data-order="-10">
          <span class="toolDetail filterRed unsuccessful">10</span>
        </td>
        <td id="dt-ComputerMonitoring" data-filter="issue" data-order="-288">
          <span class="toolDetail filterRed unsuccessful">288</span>
        </td>
        <td id="dt-Audio" data-filter="correct" data-order="0">
          <span class="successful toolDetail filterGreen"></span>
        </td>
      </tr>
    </table>
    """

    students = parse_students_from_html(html, "https://front-results.smowltech.net/index.php/ActivityStatus")

    assert students[0].id == "abc123"
    assert students[0].name == "ANA LUIZA MARSILIO ALVES"
    assert students[0].status == "UNSUCCESSFUL"
    assert any(
        event.label == "FrontCamera" and "count=10" in (event.value or "")
        for event in students[0].smow.events
    )


def test_extract_report_uses_current_page_without_reloading(tmp_path):
    class FakePage:
        url = "https://front-results.smowltech.net/results/students"
        frames = []

        def __init__(self):
            self.visited_urls: list[str] = []

        def goto(self, url: str) -> None:
            self.visited_urls.append(url)
            self.url = url

        def content(self) -> str:
            return """
            <table>
              <tr><th>Nome</th><th>Matrícula</th><th>Status</th></tr>
              <tr><td>João Silva</td><td>12345</td><td>OK</td></tr>
            </table>
            """

    page = FakePage()
    selection = Selection(
        report=LinkOption(
            id="current-smow-student-list",
            title="SMOW Results",
            url=page.url,
            metadata={"source": "current-page"},
        )
    )

    result = extract_report(page, selection, RunStorage(runs_dir=tmp_path))

    assert page.visited_urls == []
    assert result.report.students[0].name == "João Silva"
    assert result.report.students[0].registration == "12345"


def test_collect_student_evidence_requests_clicks_known_smow_controls():
    class FakeKeyboard:
        def __init__(self):
            self.pressed: list[str] = []

        def press(self, key: str) -> None:
            self.pressed.append(key)

    class FakeFrame:
        name = "smowlresults"
        url = "https://front-results.smowltech.net/index.php/ActivityStatus"

        def __init__(self):
            self.calls: list[dict] = []

        def evaluate(self, _script, payload):
            self.calls.append(payload)
            return payload["serviceName"] in {"FrontCamera", "ComputerMonitoring"}

    class FakePage:
        def __init__(self):
            self.frame = FakeFrame()
            self.frames = [self.frame]
            self.keyboard = FakeKeyboard()
            self.waits: list[int] = []

        def wait_for_timeout(self, timeout_ms: int) -> None:
            self.waits.append(timeout_ms)

    page = FakePage()
    clicks = collect_student_evidence_requests(
        page,
        [StudentRecord(id="student-1", name="Student One")],
        wait_ms=10,
    )

    assert clicks == 2
    assert page.frame.calls == [
        {"studentId": "student-1", "serviceName": "FrontCamera"},
        {"studentId": "student-1", "serviceName": "ComputerMonitoring"},
    ]
    assert page.waits == [10, 10]
