import inspect

from smowl_analyser.cli import (
    _current_page_report,
    _find_smowl_tool_option,
    _open_course,
    _run_manual_extract,
    _wait_for_smow_student_data,
)
from smowl_analyser.models import LinkOption


class FakePage:
    def __init__(self, *, course_card_opens: bool = False):
        self.url = "https://insper.blackboard.com/ultra/course"
        self.visited_urls: list[str] = []
        self.course_card_opens = course_card_opens
        self.frames = []

    def goto(self, url: str) -> None:
        self.visited_urls.append(url)
        self.url = url

    def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None

    def evaluate(self, *_args, **_kwargs) -> bool:
        if self.course_card_opens:
            self.url = "https://insper.blackboard.com/ultra/courses/_456_1/outline"
            return True
        return False

    def title(self) -> str:
        return "SMOW Results"


def test_open_course_uses_known_course_url():
    page = FakePage()
    option = LinkOption(
        id="course-card-1",
        title="DESIGN DE SOFTWARE - 1A - 2026/61",
        url="https://insper.blackboard.com/ultra/courses/_123_1/outline",
        metadata={
            "source": "course-card-link",
            "course_url": "https://insper.blackboard.com/ultra/courses/_123_1/outline",
        },
    )

    _open_course(page, option)

    assert page.visited_urls == ["https://insper.blackboard.com/ultra/courses/_123_1/outline"]
    assert page.url == "https://insper.blackboard.com/ultra/courses/_123_1/outline"


def test_open_course_can_click_live_course_card():
    page = FakePage(course_card_opens=True)
    option = LinkOption(
        id="course-card-2",
        title="202661.GRENG_201561_0007.DESIGNSOFT_1A | DESIGN DE SOFTWARE - 1A - 2026/61",
        url="https://insper.blackboard.com/ultra/course",
        metadata={
            "source": "course-card",
            "action": "click-course-card",
            "course_code": "202661.GRENG_201561_0007.DESIGNSOFT_1A",
            "course_title": "DESIGN DE SOFTWARE - 1A - 2026/61",
        },
    )

    _open_course(page, option)

    assert page.visited_urls == []
    assert page.url == "https://insper.blackboard.com/ultra/courses/_456_1/outline"


def test_find_smowl_tool_option_prefers_exact_known_name():
    options = [
        LinkOption(
            id="activity-1",
            title="SMOW - Prova Final",
            url="https://blackboard.example.test/smow-final",
        ),
        LinkOption(
            id="activity-2",
            title="Smowl Proctoring Tool",
            url="https://blackboard.example.test/smowl-tool",
        ),
    ]

    selected = _find_smowl_tool_option(options)

    assert selected is options[1]


def test_current_page_report_uses_current_url_without_api_activity():
    page = FakePage()
    page.url = "https://front-results.smowltech.net/results/students"

    report = _current_page_report(page, [])

    assert report.id == "current-smow-student-list"
    assert report.title == "SMOW Results"
    assert report.url == "https://front-results.smowltech.net/results/students"
    assert report.metadata["source"] == "current-page"


def test_current_page_report_keeps_single_api_activity_id():
    page = FakePage()
    page.url = "https://front-results.smowltech.net/results/students"
    responses = [
        {
            "url": "https://lti-smowl-global.smowltech.net/lti/ajax/activities",
            "payload": [
                {
                    "activityId": "activity-123",
                    "displayName": "Prova Final",
                }
            ],
        }
    ]

    report = _current_page_report(page, responses)

    assert report.id == "activity-123"
    assert report.url == "https://front-results.smowltech.net/results/students"
    assert report.metadata["activity_id"] == "activity-123"
    assert report.metadata["api_activity_url"] == "smow-api://activity/activity-123"
    assert report.metadata["source"] == "current-page"


def test_current_page_report_prefers_smowlresults_frame_url():
    class FakeFrame:
        name = "smowlresults"
        url = "https://front-results.smowltech.net/index.php/ActivityStatus"

    page = FakePage()
    page.url = "https://insper.blackboard.com/ultra/courses/_1_1/outline/lti/launchFrame"
    page.frames = [FakeFrame()]

    report = _current_page_report(page, [])

    assert report.url == "https://front-results.smowltech.net/index.php/ActivityStatus"
    assert report.title == "SMOW student report"
    assert report.metadata["blackboard_wrapper_url"] == page.url


def test_wait_for_smow_student_data_waits_for_required_api_responses():
    responses: list[dict] = []

    class FakeWaitingPage:
        waits = 0

        def wait_for_timeout(self, _timeout):
            self.waits += 1
            if self.waits == 1:
                responses.append({"url": "https://example.test/lti/ajax/students", "payload": {}})
            if self.waits == 2:
                responses.append({"url": "https://example.test/V2/results/figures", "payload": {}})

    assert _wait_for_smow_student_data(FakeWaitingPage(), responses, timeout_ms=3_000)


def test_manual_extract_flow_uses_browser_context_capture():
    source = inspect.getsource(_run_manual_extract)

    assert "def flow(page, context):" in source
    assert "capture.attach_context(context)" in source
