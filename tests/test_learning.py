from pathlib import Path

from smowl_analyser.learning import (
    CHECKPOINTS,
    LearningSession,
    sanitize_html,
    sanitize_json,
    sanitize_post_data,
    sanitize_url,
    summarize_html,
)


FIXTURES = Path(__file__).parent / "fixtures"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_sanitize_url_redacts_sensitive_query_params():
    url = sanitize_url("https://example.test/report?course=abc&token=secret&student=123456")

    assert "token=%5BREDACTED%5D" in url
    assert "student=%5BNUMBER%5D" in url
    assert "secret" not in url


def test_sanitize_html_removes_scripts_and_redacts_sensitive_values():
    sanitized = sanitize_html(read_fixture("learning_page.html"), "https://example.test")

    assert "<script" not in sanitized
    assert "joao@example.edu" not in sanitized
    assert "123456" not in sanitized
    assert "secret" not in sanitized


def test_summarize_html_preserves_structure_without_raw_sensitive_values():
    summary = summarize_html(read_fixture("learning_page.html"), "https://example.test")

    assert summary["counts"]["links"] == 1
    assert summary["counts"]["tables"] == 1
    assert summary["counts"]["iframes"] == 1
    assert summary["tables"][0]["headers"] == ["Nome", "Matrícula", "Status"]
    assert summary["links"][0]["text"] == "João [EMAIL]"
    assert "token=%5BREDACTED%5D" in summary["links"][0]["href"]


def test_sanitize_json_redacts_tokens_and_emails():
    payload = sanitize_json(
        {
            "access_token": "secret",
            "student": {"email": "maria@example.edu", "registration": "123456"},
        }
    )

    assert payload["access_token"] == "[REDACTED]"
    assert payload["student"]["email"] == "[EMAIL]"
    assert payload["student"]["registration"] == "[NUMBER]"


def test_sanitize_json_sanitizes_signed_urls():
    payload = sanitize_json(
        {
            "src": (
                "https://example.test/file.jpg?"
                "X-Amz-Security-Token=secret&X-Amz-Signature=abc&student=123456"
            )
        }
    )

    assert "secret" not in payload["src"]
    assert "abc" not in payload["src"]
    assert "%5BREDACTED%5D" in payload["src"]


def test_sanitize_post_data_keeps_useful_payload_shape():
    payload = sanitize_post_data(
        '{"userId":"abc123","activityName":"testactivity","token":"secret","email":"a@example.edu"}'
    )

    assert payload["userId"] == "abc123"
    assert payload["activityName"] == "testactivity"
    assert payload["token"] == "[REDACTED]"
    assert payload["email"] == "[EMAIL]"


def test_learning_checkpoints_include_evidence_actions():
    names = [name for name, _prompt in CHECKPOINTS]

    assert "smow-students-list" in names
    assert "front-camera-detail" in names
    assert "computer-monitoring-detail" in names
    assert "student-download-action" in names
    assert "evidence-preview" in names


def test_learning_checkpoint_saves_frames_and_network_delta(tmp_path):
    class FakeFrame:
        name = "smowlresults"
        url = "https://front-results.smowltech.net/index.php/ActivityStatus"

        def content(self):
            return "<html><body><table><tr><th>Nome</th></tr><tr><td>Aluno</td></tr></table></body></html>"

    class FakePage:
        url = "https://insper.blackboard.com/ultra/courses/_1_1/outline/lti/launchFrame"
        frames = [FakeFrame()]

        def title(self):
            return "Inicialização LTI"

        def content(self):
            return "<html><body><iframe name='smowlresults'></iframe></body></html>"

    learning = LearningSession.create(base_dir=tmp_path, session_id="learn-test")
    learning.responses.append(
        {
            "url": "https://results-api.smowltech.net/index.php/V2/results/reasons/allActiveServices",
            "status": 200,
        }
    )
    learning.json_responses.append(
        {
            "url": "https://results-api.smowltech.net/index.php/V2/results/reasons/allActiveServices",
            "status": 200,
            "payload": {"users": []},
        }
    )

    metadata = learning.capture_checkpoint(FakePage(), 1, "smow-students-list")

    assert metadata["frames"][0]["is_smowish"]
    assert metadata["network_delta"]["responses"] == 1
    assert metadata["network_delta"]["json_responses"] == 1
    assert metadata["network_delta"]["smow_endpoint_counts"]["results_reasons_all_services"] == 1
    assert (tmp_path / "learn-test" / "frames").exists()
    assert (tmp_path / "learn-test" / "network" / "001-smow-students-list-responses.json").exists()
