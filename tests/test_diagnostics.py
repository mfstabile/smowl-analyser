from smowl_analyser.diagnostics import DiagnosticSession, endpoint_counter


def test_endpoint_counter_tracks_smow_signals():
    counts = endpoint_counter(
        [
            {"url": "https://lti-smowl-global.smowltech.net/lti/ajax/students?state=abc"},
            {"url": "https://results-api.smowltech.net/index.php/V2/results/figures"},
            {"url": "https://results-api.smowltech.net/index.php/V2/results/reasons/allActiveServices"},
            {"url": "https://results-api.smowltech.net/index.php/V2/monitoring/evidence/frontCamera"},
            {"url": "https://front-results.smowltech.net/index.php/ActivityStatus"},
            {"url": "https://insper.blackboard.com/ultra/course"},
        ]
    )

    assert counts["lti_ajax_students"] == 1
    assert counts["results_figures"] == 1
    assert counts["results_reasons_all_services"] == 1
    assert counts["monitoring_evidence"] == 1
    assert counts["front_results_requests"] == 1
    assert counts["lti_smowl_requests"] == 1
    assert counts["results_api_requests"] == 3
    assert counts["blackboard_requests"] == 1


def test_diagnostic_session_writes_context_artifacts(tmp_path):
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

    class FakeContext:
        pages = [FakePage()]

        def on(self, _event, _callback):
            return None

    diagnostic = DiagnosticSession.create(base_dir=tmp_path, session_id="diag-test")
    summary = diagnostic.capture_context(FakeContext())

    assert summary["counts"]["pages"] == 1
    assert summary["counts"]["frames"] == 1
    assert summary["smow"]["has_smowlresults_frame"]
    assert summary["smow"]["has_front_results_frame"]
    assert (tmp_path / "diag-test" / "summary.json").exists()
    assert (tmp_path / "diag-test" / "pages.json").exists()
    assert (tmp_path / "diag-test" / "frames.json").exists()
