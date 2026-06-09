from __future__ import annotations

from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

import typer
from rich.console import Console
from rich.table import Table

from .analysis import analyze_run, write_analysis_html
from .browser import BrowserConfig, BrowserSession, JsonResponseCapture, ensure_local_state_dirs
from .config import DEFAULT_BLACKBOARD_URL
from .diagnostics import DEFAULT_DIAGNOSTICS_DIR, DiagnosticSession
from .extractors.blackboard import discover_courses, discover_smow_activities
from .extractors.smow import (
    DEFAULT_DOWNLOAD_WORKERS,
    discover_reports,
    discover_reports_from_responses,
    extract_report,
)
from .learning import CHECKPOINTS, DEFAULT_LEARNING_DIR, LearningSession
from .models import LinkOption, Selection, utc_now
from .storage import DEFAULT_RUNS_DIR, DEFAULT_STATE_PATH, RunStorage


app = typer.Typer(help="Extract Blackboard + SMOW report data.")
console = Console()
SMOWL_TOOL_NAME = "Smowl Proctoring Tool"


def _state_path(path: Path | None) -> Path:
    return path or DEFAULT_STATE_PATH


@app.command()
def doctor(
    state_path: Path | None = typer.Option(None, help="Path to Playwright storage state."),
    runs_dir: Path = typer.Option(DEFAULT_RUNS_DIR, help="Directory where runs are stored."),
) -> None:
    """Check local extractor setup."""
    ensure_local_state_dirs()
    table = Table(title="smowl-analyser doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_row("Runs directory", runs_dir.as_posix())
    table.add_row("Session file", "found" if _state_path(state_path).exists() else "missing")
    for package in ("playwright", "typer", "rich", "pydantic", "bs4"):
        table.add_row(package, "ok" if _can_import(package) else "missing")
    console.print(table)


@app.command()
def login(
    url: str = typer.Option(DEFAULT_BLACKBOARD_URL, "--url", help="Blackboard login or landing URL."),
    state_path: Path | None = typer.Option(None, help="Path to save Playwright storage state."),
    slow_mo_ms: int = typer.Option(0, help="Slow Playwright interactions by N milliseconds."),
) -> None:
    """Open a browser for manual login and save the authenticated session."""
    session = BrowserSession(
        BrowserConfig(state_path=_state_path(state_path), headless=False, slow_mo_ms=slow_mo_ms)
    )
    session.save_manual_login(url)
    console.print(f"Saved session to [bold]{session.config.state_path}[/bold]")


@app.command()
def inspect(
    url: str = typer.Option(DEFAULT_BLACKBOARD_URL, "--url", help="Blackboard landing URL."),
    state_path: Path | None = typer.Option(None, help="Path to saved Playwright storage state."),
    headless: bool = typer.Option(False, help="Run browser headless."),
) -> None:
    """List courses, SMOW activities, reports and students without downloading files."""
    session = BrowserSession(BrowserConfig(state_path=_state_path(state_path), headless=headless))

    def flow(page, context):
        capture = JsonResponseCapture()
        capture.attach_context(context)
        page.goto(url)
        _open_courses_tab(page, url)
        course = _choose("course", discover_courses(page))
        _open_course(page, course)
        activity = _open_smowl_activity(page)
        reports = discover_reports_from_responses(capture.responses) or discover_reports(page)
        _print_options("Reports", reports)
        if reports:
            report = _choose("report", reports)
            if not report.url.startswith("smow-api://"):
                page.goto(report.url)
                _settle(page)
            from .extractors.smow import parse_students_from_html
            from .extractors.smow import parse_students_from_responses

            students = parse_students_from_responses(capture.responses, report.metadata.get("activity_id"))
            if not students:
                students = parse_students_from_html(page.content(), page.url)
            _print_student_summary(students)

    session.run_with_page(flow)


@app.command()
def extract(
    url: str = typer.Option(DEFAULT_BLACKBOARD_URL, "--url", help="Blackboard landing URL."),
    state_path: Path | None = typer.Option(None, help="Path to saved Playwright storage state."),
    runs_dir: Path = typer.Option(DEFAULT_RUNS_DIR, help="Directory where runs are stored."),
    slow_mo_ms: int = typer.Option(0, help="Slow Playwright interactions by N milliseconds."),
    download_workers: int = typer.Option(
        DEFAULT_DOWNLOAD_WORKERS,
        min=1,
        help="Number of parallel image downloads per student after ComputerMonitoring data is fetched.",
    ),
) -> None:
    """Let you navigate to the SMOW student list, then extract from the current page."""
    _run_manual_extract(
        url=url,
        state_path=state_path,
        runs_dir=runs_dir,
        slow_mo_ms=slow_mo_ms,
        download_workers=download_workers,
    )


@app.command()
def analyze(
    run_id: str = typer.Option(..., "--run-id", help="Run id from data/runs/{run_id}."),
    runs_dir: Path = typer.Option(DEFAULT_RUNS_DIR, help="Directory where runs are stored."),
    rarity_ratio: float = typer.Option(
        0.10,
        min=0.01,
        max=1.0,
        help="Maximum cohort ratio for rare program/title findings.",
    ),
) -> None:
    """Analyze an extracted SMOW run and generate a local HTML report."""
    storage = RunStorage(runs_dir=runs_dir, run_id=run_id)
    if not storage.root.exists():
        raise typer.BadParameter(f"Run not found: {storage.root}")
    result = analyze_run(storage, rarity_ratio=rarity_ratio)
    output_path = write_analysis_html(storage, result)
    console.print(
        f"Analysis saved to [bold]{output_path}[/bold] "
        f"({len(result.findings)} findings across {result.student_count} students)."
    )


@app.command()
def diagnose(
    url: str = typer.Option(DEFAULT_BLACKBOARD_URL, "--url", help="Blackboard landing URL."),
    state_path: Path | None = typer.Option(None, help="Path to saved Playwright storage state."),
    diagnostics_dir: Path = typer.Option(
        DEFAULT_DIAGNOSTICS_DIR,
        help="Directory where diagnostic sessions are stored.",
    ),
    session_id: str | None = typer.Option(None, help="Optional diagnostic session id."),
    slow_mo_ms: int = typer.Option(0, help="Slow Playwright interactions by N milliseconds."),
) -> None:
    """Capture pages, frames and network data after you navigate to the SMOW student list."""
    diagnostic = DiagnosticSession.create(base_dir=diagnostics_dir, session_id=session_id)
    session = BrowserSession(
        BrowserConfig(
            state_path=_state_path(state_path),
            headless=False,
            slow_mo_ms=slow_mo_ms,
        )
    )

    def flow(page, context):
        diagnostic.attach_context(context)
        page.goto(url)
        console.print(f"Diagnostic session: [bold]{diagnostic.root}[/bold]")
        typer.prompt(
            "Navegue até a página do SMOW onde aparece a lista de alunos e pressione Enter",
            default="",
            show_default=False,
        )
        _settle(page)
        page.wait_for_timeout(3_000)
        summary = diagnostic.capture_context(context)
        _print_diagnostic_summary(summary)
        console.print(f"Diagnostic artifacts saved to [bold]{diagnostic.root}[/bold]")

    session.run_with_context_page(flow)


@app.command("auto-extract")
def auto_extract(
    url: str = typer.Option(DEFAULT_BLACKBOARD_URL, "--url", help="Blackboard landing URL."),
    state_path: Path | None = typer.Option(None, help="Path to saved Playwright storage state."),
    runs_dir: Path = typer.Option(DEFAULT_RUNS_DIR, help="Directory where runs are stored."),
    headless: bool = typer.Option(False, help="Run browser headless."),
    download_workers: int = typer.Option(
        DEFAULT_DOWNLOAD_WORKERS,
        min=1,
        help="Number of parallel image downloads per student after ComputerMonitoring data is fetched.",
    ),
) -> None:
    """Use the older semi-automatic course/activity/report selection flow."""
    storage = RunStorage(runs_dir=runs_dir)
    storage.prepare()
    session = BrowserSession(BrowserConfig(state_path=_state_path(state_path), headless=headless))

    def flow(page, context):
        capture = JsonResponseCapture()
        capture.attach_context(context)
        page.goto(url)
        _open_courses_tab(page, url)
        course = _choose("course", discover_courses(page))
        _open_course(page, course)
        activity = _open_smowl_activity(page)
        reports = discover_reports_from_responses(capture.responses) or discover_reports(page)
        if not reports:
            typer.prompt(
                "Chegue no relatório/lista de alunos no SMOW e pressione Enter",
                default="",
                show_default=False,
            )
            _settle(page)
            reports = discover_reports_from_responses(capture.responses) or discover_reports(page)
        report = _choose("report", reports)
        if report.url.startswith("smow-api://") and not _has_smow_report_api_data(capture.responses):
            typer.prompt(
                "Chegue no relatório/lista de alunos no SMOW e pressione Enter",
                default="",
                show_default=False,
            )
            _settle(page)
        elif not report.url.startswith("smow-api://"):
            page.goto(report.url)
            _settle(page)
        selection = Selection(course=course, activity=activity, report=report)
        storage.save_selection(selection)
        manifest = storage.create_manifest(urls=[url, course.url, activity.url, report.url])
        storage.save_manifest(manifest)
        result = extract_report(
            page,
            selection,
            storage,
            api_responses=capture.responses,
            download_workers=download_workers,
            download_progress=_download_progress_printer(download_workers),
        )
        total_files = sum(len(student.files) for student in result.report.students)
        manifest.counts = {
            "students": len(result.report.students),
            "done": sum(1 for entry in result.progress.entries.values() if entry.status == "done"),
            "failed": sum(1 for entry in result.progress.entries.values() if entry.status == "failed"),
            "files": total_files,
            "computer_monitoring_events": _computer_monitoring_event_count(result.report.students),
            "computer_monitoring_screenshots": _computer_monitoring_screenshot_count(result.report.students),
        }
        manifest.finished_at = utc_now()
        storage.save_manifest(manifest)
        console.print(f"Extraction saved to [bold]{storage.root}[/bold]")

    session.run_with_context_page(flow)


@app.command("manual-extract")
def manual_extract(
    url: str = typer.Option(DEFAULT_BLACKBOARD_URL, "--url", help="Blackboard landing URL."),
    state_path: Path | None = typer.Option(None, help="Path to saved Playwright storage state."),
    runs_dir: Path = typer.Option(DEFAULT_RUNS_DIR, help="Directory where runs are stored."),
    slow_mo_ms: int = typer.Option(0, help="Slow Playwright interactions by N milliseconds."),
    download_workers: int = typer.Option(
        DEFAULT_DOWNLOAD_WORKERS,
        min=1,
        help="Number of parallel image downloads per student after ComputerMonitoring data is fetched.",
    ),
) -> None:
    """Let you navigate to the SMOW student list, then extract from the current page."""
    _run_manual_extract(
        url=url,
        state_path=state_path,
        runs_dir=runs_dir,
        slow_mo_ms=slow_mo_ms,
        download_workers=download_workers,
    )


def _run_manual_extract(
    *,
    url: str,
    state_path: Path | None,
    runs_dir: Path,
    slow_mo_ms: int = 0,
    download_workers: int = DEFAULT_DOWNLOAD_WORKERS,
) -> None:
    storage = RunStorage(runs_dir=runs_dir)
    storage.prepare()
    session = BrowserSession(
        BrowserConfig(
            state_path=_state_path(state_path),
            headless=False,
            slow_mo_ms=slow_mo_ms,
        )
    )

    def flow(page, context):
        capture = JsonResponseCapture()
        capture.attach_context(context)
        page.goto(url)
        typer.prompt(
            "Navegue até a página do SMOW onde aparece a lista de alunos e pressione Enter",
            default="",
            show_default=False,
        )
        _settle(page)
        if not _wait_for_smow_student_data(page, capture.responses):
            typer.prompt(
                "Ainda não capturei os dados internos do SMOW. "
                "Aguarde a lista terminar de carregar no navegador e pressione Enter novamente",
                default="",
                show_default=False,
            )
            _settle(page)
        if not _wait_for_smow_student_data(page, capture.responses):
            raise typer.BadParameter(
                "SMOW student API data was not captured. "
                "Make sure the visible page is the SMOW student report/list, not just the Blackboard LTI wrapper. "
                "Run `smowl-analyser diagnose` on the same page to save frames and network details."
            )
        report = _current_page_report(page, capture.responses)
        selection = Selection(report=report)
        storage.save_selection(selection)
        manifest = storage.create_manifest(urls=_unique_urls([url, page.url, report.url]))
        storage.save_manifest(manifest)
        result = extract_report(
            page,
            selection,
            storage,
            api_responses=capture.responses,
            download_workers=download_workers,
            download_progress=_download_progress_printer(download_workers),
        )
        total_files = sum(len(student.files) for student in result.report.students)
        manifest.counts = {
            "students": len(result.report.students),
            "done": sum(1 for entry in result.progress.entries.values() if entry.status == "done"),
            "failed": sum(1 for entry in result.progress.entries.values() if entry.status == "failed"),
            "files": total_files,
            "computer_monitoring_events": _computer_monitoring_event_count(result.report.students),
            "computer_monitoring_screenshots": _computer_monitoring_screenshot_count(result.report.students),
        }
        if total_files == 0:
            manifest.notes.append(
                "No evidence files were captured. The SMOW list/results APIs were captured, "
                "but no monitoring evidence endpoint was observed during this run."
            )
        manifest.finished_at = utc_now()
        storage.save_manifest(manifest)
        console.print(f"Extraction saved to [bold]{storage.root}[/bold]")

    session.run_with_context_page(flow)


@app.command()
def learn(
    url: str = typer.Option(DEFAULT_BLACKBOARD_URL, "--url", help="Blackboard landing URL."),
    state_path: Path | None = typer.Option(None, help="Path to saved Playwright storage state."),
    learning_dir: Path = typer.Option(DEFAULT_LEARNING_DIR, help="Directory where learning data is stored."),
    session_id: str | None = typer.Option(None, help="Optional learning session id."),
    slow_mo_ms: int = typer.Option(0, help="Slow Playwright interactions by N milliseconds."),
) -> None:
    """Record the real manual path through Blackboard and SMOW without screenshots."""
    learning = LearningSession.create(base_dir=learning_dir, session_id=session_id)
    session = BrowserSession(
        BrowserConfig(
            state_path=_state_path(state_path),
            headless=False,
            slow_mo_ms=slow_mo_ms,
        )
    )

    def flow(page, context):
        learning.attach(page)
        learning.attach_context(context)
        page.goto(url)
        console.print(f"Learning session: [bold]{learning.root}[/bold]")
        for index, (name, prompt) in enumerate(CHECKPOINTS, start=1):
            typer.prompt(prompt, default="", show_default=False)
            metadata = learning.capture_checkpoint(page, index, name)
            console.print(
                f"Captured [bold]{index:03d}-{name}[/bold]: "
                f"{metadata['title'] or '(sem título)'}"
            )
        learning.flush()

    session.run_with_context_page(flow)
    console.print(f"Learning artifacts saved to [bold]{learning.root}[/bold]")


@app.command()
def resume(
    run_id: str = typer.Option(..., "--run-id", help="Run id from data/runs/{run_id}."),
    state_path: Path | None = typer.Option(None, help="Path to saved Playwright storage state."),
    runs_dir: Path = typer.Option(DEFAULT_RUNS_DIR, help="Directory where runs are stored."),
    headless: bool = typer.Option(False, help="Run browser headless."),
    download_workers: int = typer.Option(
        DEFAULT_DOWNLOAD_WORKERS,
        min=1,
        help="Number of parallel image downloads per student after ComputerMonitoring data is fetched.",
    ),
) -> None:
    """Resume an interrupted extraction."""
    storage = RunStorage(runs_dir=runs_dir, run_id=run_id)
    selection = storage.load_selection()
    existing_report = storage.load_report()
    existing_progress = storage.load_progress()
    session = BrowserSession(BrowserConfig(state_path=_state_path(state_path), headless=headless))

    def flow(page):
        result = extract_report(
            page,
            selection,
            storage,
            existing_report=existing_report,
            existing_progress=existing_progress,
            download_workers=download_workers,
            download_progress=_download_progress_printer(download_workers),
        )
        console.print(
            f"Resume complete: {len(result.report.students)} students tracked in [bold]{storage.root}[/bold]"
        )

    session.run_with_page(flow)


def _choose(label: str, options: list[LinkOption]) -> LinkOption:
    if not options:
        raise typer.BadParameter(f"No {label} options found on the current page.")
    _print_options(label.title(), options)
    index = typer.prompt(f"Choose {label} number", type=int)
    if index < 1 or index > len(options):
        raise typer.BadParameter(f"Invalid {label} number: {index}")
    return options[index - 1]


def _open_option(page, option: LinkOption) -> None:
    if option.metadata.get("action") in {"click", "click-course-card"}:
        selector = option.metadata.get("selector")
        if selector:
            try:
                page.locator(selector).first.click(timeout=5_000)
            except Exception:
                fallback_selector = option.metadata.get("fallback_selector")
                if not fallback_selector:
                    raise
                page.locator(fallback_selector).first.click(timeout=5_000)
        else:
            page.get_by_text(option.metadata.get("text") or option.title, exact=True).first.click(timeout=5_000)
    else:
        page.goto(option.url)
    _settle(page)


def _open_course(page, option: LinkOption) -> None:
    before_url = page.url
    last_error: Exception | None = None
    course_url = option.metadata.get("course_url") or option.url
    if _is_course_page_url(course_url):
        page.goto(course_url)
        _settle(page)
        if _is_course_page_url(page.url):
            return
        if _prompt_manual_course_open(page, option):
            return
        raise _course_open_error(option, before_url, page.url, opened_url=course_url)

    if option.metadata.get("action") == "click-course-card":
        if not _click_course_card(page, option):
            try:
                _open_option(page, option)
            except Exception as error:
                last_error = error
        else:
            _settle(page)
    else:
        try:
            _open_option(page, option)
        except Exception as error:
            last_error = error
    if _is_course_page_url(page.url):
        return

    try:
        page.wait_for_url("**/ultra/courses/**", timeout=15_000)
        _settle(page)
    except Exception:
        pass
    if _is_course_page_url(page.url):
        return

    for text in (option.metadata.get("course_title"), option.title):
        if not text:
            continue
        try:
            page.get_by_text(text, exact=True).first.click()
            page.wait_for_url("**/ultra/courses/**", timeout=15_000)
            _settle(page)
        except Exception:
            continue
        if _is_course_page_url(page.url):
            return

    if _prompt_manual_course_open(page, option):
        return

    raise _course_open_error(option, before_url, page.url, last_error=last_error)


def _open_smowl_activity(page) -> LinkOption:
    options = discover_smow_activities(page)
    activity = _find_smowl_tool_option(options) or LinkOption(
        id="smowl-tool",
        title=SMOWL_TOOL_NAME,
        url=page.url,
        metadata={
            "source": "known-title",
            "action": "click",
            "text": SMOWL_TOOL_NAME,
        },
    )
    _open_option(page, activity)
    return activity


def _find_smowl_tool_option(options: list[LinkOption]) -> LinkOption | None:
    normalized_name = _normalize_label(SMOWL_TOOL_NAME)
    for option in options:
        if _normalize_label(option.title) == normalized_name:
            return option
    return None


def _current_page_report(page, responses: list[dict]) -> LinkOption:
    smow_frame_url = _current_smow_frame_url(page)
    api_reports = discover_reports_from_responses(responses)
    metadata = {"source": "current-page"}
    report_id = "current-smow-student-list"
    title = "SMOW student report" if smow_frame_url else _current_page_title(page)
    report_url = smow_frame_url or page.url
    if len(api_reports) == 1:
        report = api_reports[0]
        report_id = report.id
        title = report.title or title
        metadata.update(report.metadata)
        metadata["source"] = "current-page"
        metadata["api_activity_url"] = report.url
    if smow_frame_url:
        metadata["blackboard_wrapper_url"] = page.url
    return LinkOption(
        id=report_id,
        title=title,
        url=report_url,
        metadata=metadata,
    )


def _current_smow_frame_url(page) -> str | None:
    frames = getattr(page, "frames", [])
    for frame in frames:
        if getattr(frame, "name", "") == "smowlresults":
            return getattr(frame, "url", "") or None
    for frame in frames:
        frame_url = getattr(frame, "url", "")
        if "front-results.smowltech.net" in frame_url:
            return frame_url
    return None


def _current_page_title(page) -> str:
    try:
        title = page.title()
    except Exception:
        title = ""
    title = " ".join((title or "").split())
    return title or "SMOW student list"


def _prompt_manual_course_open(page, option: LinkOption) -> bool:
    typer.prompt(
        "Não consegui abrir a turma automaticamente. "
        f"Abra a turma {option.title!r} neste navegador e pressione Enter",
        default="",
        show_default=False,
    )
    _settle(page)
    return _is_course_page_url(page.url)


def _course_open_error(
    option: LinkOption,
    before_url: str,
    current_url: str,
    *,
    opened_url: str | None = None,
    last_error: Exception | None = None,
) -> typer.BadParameter:
    attempted = f" opening {opened_url!r}" if opened_url else f" selecting {option.title!r}"
    detail = f" Last error: {last_error}" if last_error else ""
    return typer.BadParameter(
        f"Selected course did not open after{attempted}. "
        f"Still on {current_url!r} from {before_url!r}.{detail}"
    )


def _click_course_card(page, option: LinkOption) -> bool:
    code = option.metadata.get("course_code") or ""
    title = option.metadata.get("course_title") or option.title
    try:
        return bool(
            page.evaluate(
                """
                ({ code, title }) => {
                  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                  const visible = (el) => Boolean(el.getClientRects && el.getClientRects().length);
                  const matchesCourse = (el) => {
                    const text = normalize(el.innerText || '');
                    if (!text || !text.includes('Abrir')) {
                      return false;
                    }
                    if (code && !text.includes(code)) {
                      return false;
                    }
                    if (title && !text.includes(title)) {
                      return false;
                    }
                    return true;
                  };
                  const containers = Array.from(document.querySelectorAll('li, article, section, div'))
                    .filter((el) => visible(el) && matchesCourse(el))
                    .sort((a, b) => normalize(a.innerText).length - normalize(b.innerText).length);
                  for (const container of containers) {
                    const controls = Array.from(container.querySelectorAll('a, button'))
                      .filter(visible);
                    const open = controls.find((node) =>
                      /^Abrir$/i.test(normalize(node.innerText || node.getAttribute('aria-label')))
                    );
                    if (open) {
                      open.click();
                      return true;
                    }
                  }
                  return false;
                }
                """,
                {"code": code, "title": title},
            )
        )
    except Exception:
        return False


def _open_courses_tab(page, fallback_url: str) -> None:
    _settle(page)
    if "/ultra/course" in page.url and "/ultra/courses/" not in page.url:
        return
    if "/ultra/courses/" in page.url:
        return
    page.goto(_courses_tab_url(page.url or fallback_url))
    _settle(page)


def _courses_tab_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/ultra/course", "", ""))


def _is_course_page_url(url: str | None) -> bool:
    return bool(url and "/ultra/courses/" in url)


def _normalize_label(value: str) -> str:
    return " ".join(value.casefold().split())


def _unique_urls(urls: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


def _settle(page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        page.wait_for_timeout(1_000)


def _has_smow_report_api_data(responses: list[dict]) -> bool:
    has_students = any("/lti/ajax/students" in response.get("url", "") for response in responses)
    has_figures = any(
        "/V2/results/figures" in response.get("url", "")
        or "/V2/results/reasons/allActiveServices" in response.get("url", "")
        for response in responses
    )
    return has_students and has_figures


def _wait_for_smow_student_data(page, responses: list[dict], timeout_ms: int = 30_000) -> bool:
    deadline = timeout_ms
    while deadline >= 0:
        if _has_smow_report_api_data(responses):
            return True
        page.wait_for_timeout(1_000)
        deadline -= 1_000
    return _has_smow_report_api_data(responses)


def _print_options(title: str, options: Iterable[LinkOption]) -> None:
    table = Table(title=title)
    table.add_column("#", justify="right")
    table.add_column("Title")
    table.add_column("URL")
    for index, option in enumerate(options, start=1):
        table.add_row(str(index), option.title, option.url)
    console.print(table)


def _print_student_summary(students) -> None:
    table = Table(title="Students")
    table.add_column("#", justify="right")
    table.add_column("Name")
    table.add_column("Identifier")
    table.add_column("Status")
    for index, student in enumerate(students, start=1):
        table.add_row(str(index), student.name, student.id, student.status or "")
    console.print(table)


def _computer_monitoring_event_count(students) -> int:
    return sum(len(student.smow.computer_monitoring) for student in students)


def _computer_monitoring_screenshot_count(students) -> int:
    total = 0
    for student in students:
        for event in student.smow.computer_monitoring:
            screenshots = event.get("screenshots") if isinstance(event, dict) else None
            if isinstance(screenshots, list):
                total += len(screenshots)
    return total


def _download_progress_printer(workers: int):
    last_printed = -1

    def print_progress(done: int, total: int, downloaded: int, failed: int) -> None:
        nonlocal last_printed
        if total == 0:
            if last_printed != 0:
                console.print("No ComputerMonitoring images queued for download.")
                last_printed = 0
            return
        if done == 0:
            console.print(f"Downloading {total} ComputerMonitoring images with {workers} workers...")
            last_printed = 0
            return
        if done == total or done - last_printed >= 50:
            console.print(
                f"Processed {done}/{total} ComputerMonitoring images "
                f"({downloaded} downloaded, {failed} failed)."
            )
            last_printed = done

    return print_progress


def _print_diagnostic_summary(summary: dict) -> None:
    table = Table(title="SMOW Diagnostic Summary")
    table.add_column("Signal")
    table.add_column("Value")
    counts = summary.get("counts", {})
    smow = summary.get("smow", {})
    endpoints = smow.get("endpoint_counts", {})
    table.add_row("Pages", str(counts.get("pages", 0)))
    table.add_row("Frames", str(counts.get("frames", 0)))
    table.add_row("Responses", str(counts.get("responses", 0)))
    table.add_row("JSON responses", str(counts.get("json_responses", 0)))
    table.add_row("smowlresults frame", str(smow.get("has_smowlresults_frame", False)))
    table.add_row("front-results frame", str(smow.get("has_front_results_frame", False)))
    for key in (
        "lti_ajax_activities",
        "lti_ajax_students",
        "results_figures",
        "results_reasons_all_services",
        "monitoring_evidence",
        "front_results_requests",
        "lti_smowl_requests",
        "results_api_requests",
    ):
        table.add_row(key, str(endpoints.get(key, 0)))
    console.print(table)


def _can_import(package: str) -> bool:
    try:
        __import__(package)
    except ImportError:
        return False
    return True
