from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import mimetypes
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    import certifi
except ImportError:  # pragma: no cover - dependency fallback for partially installed environments.
    certifi = None

from .html import absolute_url, clean_text, looks_interesting, soup_from_html
from ..models import (
    ExtractionReport,
    ExtractedFile,
    FileStatus,
    LinkOption,
    Progress,
    RunInfo,
    Selection,
    SmowData,
    SmowEvent,
    SmowFlag,
    StudentRecord,
)
from ..storage import RunStorage, safe_slug


REPORT_KEYWORDS = (
    "report",
    "reports",
    "relatório",
    "relatorio",
    "resultado",
    "results",
    "student",
    "aluno",
)
FILE_KEYWORDS = (
    "capture",
    "captura",
    "snapshot",
    "photo",
    "foto",
    "image",
    "imagem",
    "evidence",
    "evidência",
    "evidencia",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
)
FLAG_KEYWORDS = (
    "flag",
    "alert",
    "alerta",
    "warning",
    "suspeita",
    "incidente",
    "event",
    "evento",
)
COMPUTER_MONITORING_REASONS_ENDPOINT = (
    "https://results-api.smowltech.net/index.php/V2/resultsReasonsCaptures/computerMonitoring"
)
COMPUTER_MONITORING_EVIDENCE_ENDPOINT = (
    "https://results-api.smowltech.net/index.php/V2/monitoring/evidence/computerMonitoring"
)
COMPUTER_MONITORING_ENDPOINTS = (
    COMPUTER_MONITORING_EVIDENCE_ENDPOINT,
    COMPUTER_MONITORING_REASONS_ENDPOINT,
)
DEFAULT_DOWNLOAD_WORKERS = 8
DOWNLOAD_TIMEOUT_SECONDS = 20
DownloadProgressCallback = Callable[[int, int, int, int], None]


@dataclass(frozen=True)
class ExtractionResult:
    report: ExtractionReport
    progress: Progress


@dataclass(frozen=True)
class FileDownloadTask:
    student_id: str
    index: int
    file_ref: ExtractedFile


def parse_reports_from_html(html: str, base_url: str | None = None) -> list[LinkOption]:
    soup = soup_from_html(html)
    reports: list[LinkOption] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        title = clean_text(anchor.get_text(" "))
        href = anchor.get("href")
        combined = f"{title} {href}"
        if not title or not looks_interesting(combined, REPORT_KEYWORDS):
            continue
        url = absolute_url(href, base_url)
        if not url or url in seen:
            continue
        seen.add(url)
        reports.append(
            LinkOption(
                id=f"report-{len(reports) + 1}",
                title=title,
                url=url,
                metadata={"source": "anchor"},
            )
        )
    return reports


def parse_students_from_html(html: str, base_url: str | None = None) -> list[StudentRecord]:
    soup = soup_from_html(html)
    dashboard_students = _students_from_smow_dashboard(soup, base_url)
    if dashboard_students:
        return dashboard_students
    students = _students_from_tables(soup, base_url)
    if students:
        return students
    return _students_from_links(soup, base_url)


def parse_student_detail_from_html(
    html: str,
    student: StudentRecord,
    base_url: str | None = None,
) -> StudentRecord:
    soup = soup_from_html(html)
    student.smow = SmowData(
        status=student.smow.status,
        score=student.smow.score,
        flags=parse_flags_from_html(html, base_url),
        events=parse_events_from_html(html, base_url),
        source_url=base_url or student.source_url,
    )
    student.files = parse_file_references_from_html(html, base_url)
    return student


def parse_flags_from_html(html: str, base_url: str | None = None) -> list[SmowFlag]:
    soup = soup_from_html(html)
    flags: list[SmowFlag] = []
    for node in soup.find_all(["li", "tr", "div", "span"]):
        text = clean_text(node.get_text(" "))
        if not text or not looks_interesting(text, FLAG_KEYWORDS):
            continue
        flags.append(SmowFlag(label=text, source_url=base_url))
    return _dedupe_flags(flags)


def parse_events_from_html(html: str, base_url: str | None = None) -> list[SmowEvent]:
    soup = soup_from_html(html)
    events: list[SmowEvent] = []
    for row in soup.find_all("tr"):
        cells = [clean_text(cell.get_text(" ")) for cell in row.find_all(["td", "th"])]
        if len(cells) < 2:
            continue
        combined = " | ".join(cells)
        if looks_interesting(combined, FLAG_KEYWORDS):
            events.append(SmowEvent(label=cells[0], value=" | ".join(cells[1:]), source_url=base_url))
    return events


def parse_file_references_from_html(
    html: str,
    base_url: str | None = None,
) -> list[ExtractedFile]:
    soup = soup_from_html(html)
    files: list[ExtractedFile] = []
    seen: set[str] = set()
    candidates: list[tuple[str | None, str]] = []
    for node in soup.find_all(["a", "img"]):
        if node.name == "a" and node.get("href"):
            candidates.append((node.get("href"), clean_text(node.get_text(" "))))
        elif node.name == "img" and node.get("src"):
            candidates.append((node.get("src"), clean_text(node.get("alt"))))

    for href, label in candidates:
        combined = f"{href or ''} {label}"
        if not looks_interesting(combined, FILE_KEYWORDS):
            continue
        url = absolute_url(href, base_url)
        key = _file_identity(url)
        if not url or key in seen:
            continue
        seen.add(key)
        mime_type = mimetypes.guess_type(urlparse(url).path)[0]
        files.append(
            ExtractedFile(
                original_url=url,
                mime_type=mime_type,
                status=FileStatus.PENDING,
            )
        )
    return files


def discover_reports(page: Any) -> list[LinkOption]:
    html, url = _best_smow_html(page)
    return parse_reports_from_html(html, url)


def discover_reports_from_responses(responses: list[dict[str, Any]]) -> list[LinkOption]:
    for response in responses:
        if "/lti/ajax/activities" not in response.get("url", ""):
            continue
        options = parse_activity_options_from_payload(response.get("payload"))
        if options:
            return options
    return []


def parse_activity_options_from_payload(payload: Any) -> list[LinkOption]:
    if not isinstance(payload, list):
        return []
    options: list[LinkOption] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        activity_id = str(item.get("activityId") or "")
        title = str(item.get("displayName") or activity_id)
        if not activity_id or not title:
            continue
        options.append(
            LinkOption(
                id=activity_id,
                title=title,
                url=f"smow-api://activity/{activity_id}",
                metadata={
                    "source": "smow-api",
                    "activity_id": activity_id,
                    "lms_activity_id": item.get("lmsActivityId"),
                    "enabled": item.get("enabled"),
                    "number_users": item.get("numberUsers"),
                    "flags": item.get("flags"),
                },
            )
        )
    return options


def parse_students_from_responses(
    responses: list[dict[str, Any]],
    activity_id: str | None = None,
) -> list[StudentRecord]:
    names = _student_names_from_responses(responses)
    if not names:
        return []
    figures_by_user = _results_by_user_from_responses(responses, activity_id)
    files_by_user = _files_by_user_from_responses(responses, activity_id)
    computer_monitoring_by_user = _computer_monitoring_by_user_from_responses(responses, activity_id)
    students: list[StudentRecord] = []
    student_items = (
        [(student_id, names.get(student_id, student_id)) for student_id in figures_by_user]
        if figures_by_user
        else list(names.items())
    )
    for student_id, name in student_items:
        figures = figures_by_user.get(student_id, {})
        status = figures.get("globalStatus")
        student = StudentRecord(
            id=safe_slug(student_id, f"student-{len(students) + 1}"),
            name=str(name),
            status=status,
            source_url=figures.get("source_url") or "smow-api:/lti/ajax/students",
            smow=SmowData(
                status=status,
                service_statuses=_service_statuses_from_figures(figures),
                source_url=figures.get("source_url") or "smow-api",
                events=_events_from_figures(student_id, figures),
                computer_monitoring=computer_monitoring_by_user.get(student_id, []),
            ),
        )
        student.files = files_by_user.get(student_id, [])
        students.append(student)
    return students


def extract_report(
    page: Any,
    selection: Selection,
    storage: RunStorage,
    *,
    existing_report: ExtractionReport | None = None,
    existing_progress: Progress | None = None,
    api_responses: list[dict[str, Any]] | None = None,
    download_workers: int = DEFAULT_DOWNLOAD_WORKERS,
    download_progress: DownloadProgressCallback | None = None,
) -> ExtractionResult:
    if selection.report is None:
        raise ValueError("A report selection is required for extraction.")

    activity_id = selection.report.metadata.get("activity_id") if selection.report.metadata else None
    students = parse_students_from_responses(api_responses or [], activity_id=activity_id)
    if not students:
        if (
            not selection.report.url.startswith("smow-api://")
            and selection.report.metadata.get("source") != "current-page"
        ):
            page.goto(selection.report.url)
        html, url = _best_smow_html(page)
        students = parse_students_from_html(html, url)
    report = existing_report or ExtractionReport(
        run=RunInfo(
            id=storage.run_id,
            course=selection.course,
            activity=selection.activity,
            report=selection.report,
        ),
        students=students,
    )
    if existing_report:
        known = {student.id: student for student in existing_report.students}
        for student in students:
            known.setdefault(student.id, student)
        report.students = list(known.values())

    progress = existing_progress or Progress(run_id=storage.run_id)
    progress.ensure_students(report.students)
    _hydrate_computer_monitoring_from_storage(report.students, storage)
    storage.save_progress(progress)
    storage.save_report(report)

    students_by_id = {student.id: student for student in report.students}
    activity_name = _activity_name_from_responses(api_responses or [], activity_id)
    download_total = sum(len(_download_candidates_for_student(student)) for student in report.students)
    download_processed = 0
    download_succeeded = 0
    download_failed = 0
    if api_responses and download_progress and download_total:
        download_progress(download_processed, download_total, download_succeeded, download_failed)
    for student_id in progress.pending_student_ids():
        student = students_by_id.get(student_id)
        if not student:
            continue
        try:
            if api_responses:
                fetched = _fetch_computer_monitoring_evidence(
                    page,
                    student.id,
                    activity_name=activity_name,
                    activity_id=activity_id,
                    api_responses=api_responses,
                )
                api_responses.extend(fetched)
                if not fetched:
                    collect_student_evidence_requests(page, [student])
                refreshed = {
                    updated.id: updated
                    for updated in parse_students_from_responses(api_responses, activity_id=activity_id)
                }.get(student.id)
                if refreshed:
                    student.status = refreshed.status
                    student.source_url = refreshed.source_url
                    student.smow = refreshed.smow
                    student.files = refreshed.files
                _prefer_fresh_computer_monitoring(student, fetched, activity_id)
                storage.save_student_computer_monitoring(student)
                stats = (
                    _download_one_student_files_parallel(student, storage, max(1, download_workers))
                    if download_workers > 1
                    else _download_one_student_files_sequential(page, student, storage)
                )
                storage.save_student_computer_monitoring(student)
                if download_progress:
                    download_total = max(download_total, download_processed + stats["processed"])
                    download_processed += stats["processed"]
                    download_succeeded += stats["downloaded"]
                    download_failed += stats["failed"]
                    download_progress(
                        download_processed,
                        download_total,
                        download_succeeded,
                        download_failed,
                    )
            else:
                _extract_student(page, student, storage)
                storage.save_student_computer_monitoring(student)
            progress.mark_done(student.id)
        except Exception as exc:
            progress.mark_failed(student.id, str(exc))
        storage.save_progress(progress)
        storage.save_report(report)

    if not api_responses and any(student.smow.computer_monitoring for student in report.students):
        _download_report_files(
            page,
            report.students,
            storage,
            download_workers=download_workers,
            download_progress=download_progress,
        )
        for student in report.students:
            storage.save_student_computer_monitoring(student)
        storage.save_report(report)

    return ExtractionResult(report=report, progress=progress)


def _hydrate_computer_monitoring_from_storage(
    students: list[StudentRecord],
    storage: RunStorage,
) -> None:
    for student in students:
        if student.smow.computer_monitoring:
            continue
        payload = storage.load_student_computer_monitoring(student.id)
        if not payload:
            continue
        summary = payload.get("smow_summary")
        if isinstance(summary, dict):
            student.status = student.status or summary.get("global_status")
            service_statuses = summary.get("service_statuses")
            if isinstance(service_statuses, dict):
                student.smow.service_statuses = {
                    str(key): str(value) for key, value in service_statuses.items()
                }
        computer_monitoring = payload.get("computer_monitoring")
        if isinstance(computer_monitoring, list):
            student.smow.computer_monitoring = [
                item for item in computer_monitoring if isinstance(item, dict)
            ]


def _fetch_computer_monitoring_evidence(
    page: Any,
    student_id: str,
    *,
    activity_name: str | None,
    activity_id: str | None,
    api_responses: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    activity_name = activity_name or _activity_name_from_activity_id(activity_id)
    if not activity_name:
        return []
    request_headers = _results_api_request_headers(api_responses or [])
    payloads = _computer_monitoring_request_payloads(student_id, activity_name, activity_id)
    attempts: list[tuple[str, dict[str, str]]] = []
    for payload in payloads:
        attempts.append(("form", payload))
        attempts.append(("json", payload))
    results: list[dict[str, Any]] = []
    seen_endpoint_payloads: set[tuple[str, str]] = set()
    for endpoint in COMPUTER_MONITORING_ENDPOINTS:
        for mode, payload in attempts:
            dedupe_key = (endpoint, repr(sorted(payload.items())))
            if dedupe_key in seen_endpoint_payloads:
                continue
            seen_endpoint_payloads.add(dedupe_key)
            try:
                if mode == "form":
                    response = page.request.post(endpoint, form=payload, headers=request_headers)
                else:
                    response = page.request.post(endpoint, data=payload, headers=request_headers)
            except Exception:
                continue
            if not getattr(response, "ok", False):
                continue
            try:
                body = response.json()
            except Exception:
                continue
            if _is_computer_monitoring_payload(body, student_id, activity_name):
                results.append(
                    {
                        "url": endpoint,
                        "status": getattr(response, "status", None),
                        "method": "POST",
                        "request_payload": payload,
                        "request_payload_mode": mode,
                        "payload": body,
                    }
                )
                break
    return results


def _activity_name_from_activity_id(activity_id: str | None) -> str | None:
    if not activity_id:
        return None
    return activity_id if activity_id.startswith("test") else f"test{activity_id}"


def _results_api_request_headers(responses: list[dict[str, Any]]) -> dict[str, str]:
    preferred_keys = {"accept", "authorization", "referer", "user-agent"}
    for response in reversed(responses):
        if "results-api.smowltech.net" not in response.get("url", ""):
            continue
        headers = response.get("request_headers")
        if not isinstance(headers, dict):
            continue
        result = {
            str(key): str(value)
            for key, value in headers.items()
            if str(key).lower() in preferred_keys and value
        }
        if result:
            return result
    return {}


def _computer_monitoring_request_payloads(
    student_id: str,
    activity_name: str,
    activity_id: str | None,
) -> list[dict[str, str]]:
    clean_activity_id = activity_id or activity_name.removeprefix("test")
    return [
        {
            "activityType": "test",
            "activityId": clean_activity_id,
            "userId": student_id,
        },
        {
            "activityType": "test",
            "activityId": clean_activity_id,
            "idUser": student_id,
        },
        {"userId": student_id, "activityName": activity_name},
    ]


def _is_computer_monitoring_payload(
    payload: Any,
    student_id: str,
    activity_name: str,
) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("userId") and str(payload.get("userId")) != student_id:
        return False
    if payload.get("activityName") and str(payload.get("activityName")) != activity_name:
        return False
    return isinstance(payload.get("evidence"), list) or isinstance(payload.get("activities"), dict)


def collect_student_evidence_requests(
    page: Any,
    students: list[StudentRecord],
    *,
    wait_ms: int = 1_000,
) -> int:
    """Click known SMOW dashboard controls so Playwright can capture evidence JSON."""
    frame = _best_smow_frame(page) or page
    clicks = 0
    for student in students:
        for service_name in ("FrontCamera", "ComputerMonitoring"):
            if _click_student_service_control(frame, student.id, service_name):
                clicks += 1
                _quiet_wait(page, wait_ms)
                _quiet_escape(page)
    return clicks


def _students_from_tables(soup: Any, base_url: str | None) -> list[StudentRecord]:
    students: list[StudentRecord] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [clean_text(cell.get_text(" ")).lower() for cell in rows[0].find_all(["th", "td"])]
        if not headers:
            continue
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            values = [clean_text(cell.get_text(" ")) for cell in cells]
            if not any(values):
                continue
            data = {headers[index]: values[index] for index in range(min(len(headers), len(values)))}
            name = _pick(data, "name", "nome", "student", "aluno")
            if not name:
                continue
            link = row.find("a", href=True)
            source_url = absolute_url(link.get("href"), base_url) if link else None
            student_id = _pick(data, "id", "identifier", "matrícula", "matricula", "registration")
            email = _pick(data, "email", "e-mail")
            registration = _pick(data, "matrícula", "matricula", "registration")
            status = _pick(data, "status", "situação", "situacao")
            score = _pick(data, "score", "pontuação", "pontuacao", "nota")
            students.append(
                StudentRecord(
                    id=safe_slug(student_id or email or name, f"student-{len(students) + 1}"),
                    name=name,
                    email=email,
                    registration=registration,
                    status=status,
                    source_url=source_url,
                    smow=SmowData(status=status, score=score, source_url=source_url),
                )
            )
    return _dedupe_students(students)


def _students_from_smow_dashboard(soup: Any, base_url: str | None) -> list[StudentRecord]:
    rows = soup.select("table#dashboard-datatable tr[data-iduser]")
    students: list[StudentRecord] = []
    for row in rows:
        user_id = clean_text(row.get("data-iduser") or "")
        if not user_id:
            continue
        name_node = row.select_one("#dt-username [title], td[id='dt-username'] [title]")
        id_node = row.select_one("#dt-id [title], td[id='dt-id'] [title]")
        name = clean_text(
            (name_node.get("title") if name_node else "")
            or (name_node.get_text(" ") if name_node else "")
            or user_id
        )
        source_id = clean_text((id_node.get("title") if id_node else "") or user_id)
        status_node = row.select_one("#dt-status")
        status_filter = status_node.get("data-filter", "") if status_node else ""
        status = _status_from_filter(status_filter)
        events = _events_from_dashboard_row(row, base_url)
        students.append(
            StudentRecord(
                id=safe_slug(source_id or user_id, f"student-{len(students) + 1}"),
                name=name,
                status=status,
                source_url=base_url,
                smow=SmowData(status=status, events=events, source_url=base_url),
            )
        )
    return _dedupe_students(students)


def _students_from_links(soup: Any, base_url: str | None) -> list[StudentRecord]:
    students: list[StudentRecord] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        title = clean_text(anchor.get_text(" "))
        href = anchor.get("href")
        combined = f"{title} {href}"
        if not title or not looks_interesting(combined, ("student", "aluno", "user", "usuario")):
            continue
        url = absolute_url(href, base_url)
        if not url or url in seen:
            continue
        seen.add(url)
        students.append(
            StudentRecord(
                id=safe_slug(title, f"student-{len(students) + 1}"),
                name=title,
                source_url=url,
                smow=SmowData(source_url=url),
            )
        )
    return students


def _extract_student(page: Any, student: StudentRecord, storage: RunStorage) -> None:
    if student.source_url:
        page.goto(student.source_url)
        parse_student_detail_from_html(page.content(), student, page.url)
    else:
        parse_student_detail_from_html(page.content(), student, page.url)

    downloaded: list[ExtractedFile] = []
    for index, file_ref in enumerate(student.files, start=1):
        if not file_ref.original_url:
            downloaded.append(file_ref)
            continue
        try:
            response = page.request.get(file_ref.original_url)
            if not response.ok:
                downloaded.append(storage.failed_file(file_ref.original_url, f"HTTP {response.status}"))
                continue
            filename = _filename_from_url(file_ref.original_url, index)
            downloaded.append(
                storage.save_file(
                    student.id,
                    filename,
                    response.body(),
                    original_url=file_ref.original_url,
                    mime_type=response.headers.get("content-type") or file_ref.mime_type,
                )
            )
        except Exception as exc:
            downloaded.append(storage.failed_file(file_ref.original_url, str(exc)))
    student.files = downloaded


def _download_student_files(page: Any, student: StudentRecord, storage: RunStorage) -> None:
    downloaded: list[ExtractedFile] = []
    downloaded_by_url: dict[str, ExtractedFile] = {}
    for index, file_ref in enumerate(_download_candidates_for_student(student), start=1):
        if (
            file_ref.status == FileStatus.DOWNLOADED
            and file_ref.local_path
            and (storage.root / file_ref.local_path).exists()
        ):
            downloaded.append(file_ref)
            if file_ref.original_url:
                _register_downloaded_file(downloaded_by_url, file_ref.original_url, file_ref)
            continue
        if not file_ref.original_url:
            downloaded.append(file_ref)
            continue
        try:
            response = page.request.get(file_ref.original_url)
            if not response.ok:
                file = storage.failed_file(file_ref.original_url, f"HTTP {response.status}")
                downloaded.append(file)
                _register_downloaded_file(downloaded_by_url, file_ref.original_url, file)
                continue
            filename = _filename_from_url(file_ref.original_url, index)
            file = storage.save_file(
                student.id,
                filename,
                response.body(),
                original_url=file_ref.original_url,
                mime_type=response.headers.get("content-type") or file_ref.mime_type,
            )
            downloaded.append(file)
            _register_downloaded_file(downloaded_by_url, file_ref.original_url, file)
        except Exception as exc:
            file = storage.failed_file(file_ref.original_url, str(exc))
            downloaded.append(file)
            _register_downloaded_file(downloaded_by_url, file_ref.original_url, file)
    student.files = downloaded
    _link_downloaded_files_to_computer_monitoring(student, downloaded_by_url)


def _download_report_files(
    page: Any,
    students: list[StudentRecord],
    storage: RunStorage,
    *,
    download_workers: int,
    download_progress: DownloadProgressCallback | None = None,
) -> None:
    workers = max(1, download_workers)
    if workers == 1:
        total = sum(len(_download_candidates_for_student(student)) for student in students)
        done = 0
        succeeded = 0
        failed = 0
        if download_progress:
            download_progress(done, total, succeeded, failed)
        for student in students:
            _download_student_files(page, student, storage)
            done += len(student.files)
            succeeded += sum(1 for file in student.files if file.status == FileStatus.DOWNLOADED)
            failed += sum(1 for file in student.files if file.status == FileStatus.FAILED)
            if download_progress:
                download_progress(done, total, succeeded, failed)
        return

    total = sum(len(_download_candidates_for_student(student)) for student in students)
    done = 0
    succeeded = 0
    failed = 0
    if download_progress:
        download_progress(done, total, succeeded, failed)

    for student in students:
        stats = _download_one_student_files_parallel(student, storage, workers)
        done += stats["processed"]
        succeeded += stats["downloaded"]
        failed += stats["failed"]
        if download_progress:
            download_progress(done, total, succeeded, failed)

def _prefer_fresh_computer_monitoring(
    student: StudentRecord,
    fresh_responses: list[dict[str, Any]],
    activity_id: str | None,
) -> None:
    if not fresh_responses:
        return
    fresh_events = _computer_monitoring_by_user_from_responses(fresh_responses, activity_id).get(student.id)
    if fresh_events:
        student.smow.computer_monitoring = fresh_events
    fresh_files = _files_by_user_from_responses(fresh_responses, activity_id).get(student.id)
    if fresh_files:
        student.files = fresh_files


def _download_one_student_files_sequential(
    page: Any,
    student: StudentRecord,
    storage: RunStorage,
) -> dict[str, int]:
    _download_student_files(page, student, storage)
    return _download_stats(student.files)


def _download_one_student_files_parallel(
    student: StudentRecord,
    storage: RunStorage,
    workers: int,
) -> dict[str, int]:
    downloaded: list[tuple[int, ExtractedFile]] = []
    downloaded_by_url: dict[str, ExtractedFile] = {}
    tasks: list[FileDownloadTask] = []

    for index, file_ref in enumerate(_download_candidates_for_student(student), start=1):
        if (
            file_ref.status == FileStatus.DOWNLOADED
            and file_ref.local_path
            and (storage.root / file_ref.local_path).exists()
        ):
            downloaded.append((index, file_ref))
            if file_ref.original_url:
                _register_downloaded_file(downloaded_by_url, file_ref.original_url, file_ref)
            continue
        if not file_ref.original_url:
            downloaded.append((index, file_ref))
            continue
        tasks.append(FileDownloadTask(student_id=student.id, index=index, file_ref=file_ref))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_download_file_with_urllib, task, storage): task for task in tasks}
        for future in as_completed(futures):
            task = futures[future]
            try:
                file = future.result()
            except Exception as exc:
                file = storage.failed_file(task.file_ref.original_url or "", str(exc))
            downloaded.append((task.index, file))
            if task.file_ref.original_url:
                _register_downloaded_file(
                    downloaded_by_url,
                    task.file_ref.original_url,
                    file,
                )

    student.files = [file for _index, file in sorted(downloaded, key=lambda item: item[0])]
    _link_downloaded_files_to_computer_monitoring(student, downloaded_by_url)
    return _download_stats(student.files)


def _download_stats(files: list[ExtractedFile]) -> dict[str, int]:
    return {
        "processed": len(files),
        "downloaded": sum(1 for file in files if file.status == FileStatus.DOWNLOADED),
        "failed": sum(1 for file in files if file.status == FileStatus.FAILED),
    }


def _download_file_with_urllib(task: FileDownloadTask, storage: RunStorage) -> ExtractedFile:
    original_url = task.file_ref.original_url
    if not original_url:
        return task.file_ref
    try:
        request = Request(original_url, headers={"User-Agent": "smowl-analyser/1.0"})
        with urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS, context=_download_ssl_context()) as response:
            filename = _filename_from_url(original_url, task.index)
            return storage.save_file(
                task.student_id,
                filename,
                response.read(),
                original_url=original_url,
                mime_type=response.headers.get("content-type") or task.file_ref.mime_type,
            )
    except Exception as exc:
        return storage.failed_file(original_url, str(exc))


def _download_ssl_context() -> ssl.SSLContext:
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


def _download_candidates_for_student(student: StudentRecord) -> list[ExtractedFile]:
    computer_monitoring_files = _computer_monitoring_file_references(student)
    if computer_monitoring_files:
        return _dedupe_files(computer_monitoring_files)
    return _dedupe_files(student.files)


def _computer_monitoring_file_references(student: StudentRecord) -> list[ExtractedFile]:
    files: list[ExtractedFile] = []
    for event in student.smow.computer_monitoring:
        screenshots = event.get("screenshots")
        if not isinstance(screenshots, list):
            continue
        for screenshot in screenshots:
            if not isinstance(screenshot, dict):
                continue
            url = screenshot.get("original_url")
            if not isinstance(url, str) or not url:
                continue
            files.append(
                ExtractedFile(
                    local_path=screenshot.get("local_path"),
                    original_url=url,
                    mime_type=screenshot.get("mime_type") or mimetypes.guess_type(urlparse(url).path)[0],
                    size_bytes=screenshot.get("size_bytes"),
                    sha256=screenshot.get("sha256"),
                    status=_file_status_from_value(screenshot.get("status")),
                    error=screenshot.get("error"),
                )
            )
    return files


def _file_status_from_value(value: Any) -> FileStatus:
    try:
        return FileStatus(value)
    except (TypeError, ValueError):
        return FileStatus.PENDING


def _register_downloaded_file(
    downloaded_by_url: dict[str, ExtractedFile],
    original_url: str,
    file: ExtractedFile,
) -> None:
    downloaded_by_url[original_url] = file
    downloaded_by_url[_file_identity(original_url)] = file


def _best_smow_frame(page: Any) -> Any | None:
    for frame in getattr(page, "frames", []):
        frame_url = getattr(frame, "url", "")
        frame_name = getattr(frame, "name", "")
        if frame_name == "smowlresults" or "front-results.smowltech.net" in frame_url:
            return frame
    return None


def _best_smow_html(page: Any) -> tuple[str, str]:
    frame = _best_smow_frame(page)
    if frame is not None:
        try:
            return frame.content(), getattr(frame, "url", "")
        except Exception:
            pass
    return page.content(), page.url


def _click_student_service_control(frame: Any, student_id: str, service_name: str) -> bool:
    try:
        return bool(
            frame.evaluate(
                """
                ({ studentId, serviceName }) => {
                  const cssEscape = globalThis.CSS && CSS.escape
                    ? CSS.escape
                    : (value) => String(value).replace(/["\\\\]/g, "\\\\$&");
                  const row = document.querySelector(`tr[data-iduser="${cssEscape(studentId)}"]`);
                  if (!row) {
                    return false;
                  }
                  const cell = row.querySelector(`td[id="dt-${serviceName}"]`);
                  const target = cell && cell.querySelector('.toolDetail');
                  if (!target) {
                    return false;
                  }
                  target.scrollIntoView({ block: 'center', inline: 'center' });
                  target.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, view: window }));
                  target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                  target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                  target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                  return true;
                }
                """,
                {"studentId": student_id, "serviceName": service_name},
            )
        )
    except Exception:
        return False


def _quiet_wait(page: Any, timeout_ms: int) -> None:
    try:
        page.wait_for_timeout(timeout_ms)
    except Exception:
        return


def _quiet_escape(page: Any) -> None:
    try:
        page.keyboard.press("Escape")
    except Exception:
        return


def _student_names_from_responses(responses: list[dict[str, Any]]) -> dict[str, str]:
    for response in responses:
        if "/lti/ajax/students" not in response.get("url", ""):
            continue
        payload = response.get("payload")
        if isinstance(payload, dict):
            return {str(student_id): str(name) for student_id, name in payload.items()}
    return {}


def _results_by_user_from_responses(
    responses: list[dict[str, Any]],
    activity_id: str | None,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    activity_key = f"test{activity_id}" if activity_id and not activity_id.startswith("test") else activity_id
    for response in responses:
        if not _is_results_payload_response(response.get("url", "")):
            continue
        payload = response.get("payload")
        if not isinstance(payload, dict):
            continue
        for user in payload.get("users", []):
            if not isinstance(user, dict) or not user.get("userId"):
                continue
            activities = user.get("activities") if isinstance(user.get("activities"), dict) else {}
            selected_activity = None
            if activity_key and activity_key in activities:
                selected_activity = activities[activity_key]
            elif activities:
                selected_activity = next(iter(activities.values()))
            result[str(user["userId"])] = {
                "globalStatus": user.get("globalStatus"),
                "isBlocked": user.get("isBlocked"),
                "activity": selected_activity or {},
                "source_url": _results_source_url(response.get("url", "")),
            }
    return result


def _is_results_payload_response(url: str) -> bool:
    return "/V2/results/figures" in url or "/V2/results/reasons/allActiveServices" in url


def _results_source_url(url: str) -> str:
    if "/V2/results/reasons/allActiveServices" in url:
        return "smow-api:/results/reasons/allActiveServices"
    if "/V2/results/figures" in url:
        return "smow-api:/results/figures"
    return "smow-api"


def _activity_name_from_responses(
    responses: list[dict[str, Any]],
    activity_id: str | None,
) -> str | None:
    if activity_id:
        return f"test{activity_id}" if not activity_id.startswith("test") else activity_id
    for response in responses:
        if not _is_results_payload_response(response.get("url", "")):
            continue
        payload = response.get("payload")
        if not isinstance(payload, dict):
            continue
        for user in payload.get("users", []):
            if not isinstance(user, dict):
                continue
            activities = user.get("activities")
            if isinstance(activities, dict) and activities:
                return str(next(iter(activities.keys())))
    return None


def _files_by_user_from_responses(
    responses: list[dict[str, Any]],
    activity_id: str | None,
) -> dict[str, list[ExtractedFile]]:
    result: dict[str, list[ExtractedFile]] = {}
    for response in responses:
        url = response.get("url", "")
        payload = response.get("payload")
        if not isinstance(payload, dict):
            continue
        if "/V2/monitoring/evidence/" in url and not _payload_matches_activity(payload, activity_id):
            continue
        user_id = _payload_user_id(payload)
        if not user_id:
            continue
        files = _files_from_payload(payload)
        if files:
            result.setdefault(user_id, []).extend(files)
    return {student_id: _dedupe_files(files) for student_id, files in result.items()}


def _computer_monitoring_by_user_from_responses(
    responses: list[dict[str, Any]],
    activity_id: str | None,
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for response in responses:
        url = response.get("url", "")
        if (
            "/V2/monitoring/evidence/computerMonitoring" not in url
            and "/V2/resultsReasonsCaptures/computerMonitoring" not in url
        ):
            continue
        payload = response.get("payload")
        if not isinstance(payload, dict) or not _payload_matches_activity(payload, activity_id):
            continue
        user_id = _payload_user_id(payload)
        if not user_id:
            continue
        result.setdefault(user_id, []).extend(_computer_monitoring_events_from_payload(payload))
    return {student_id: _dedupe_computer_monitoring_events(events) for student_id, events in result.items()}


def _computer_monitoring_events_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        evidence = _computer_monitoring_captures_from_reasons_payload(payload)
    events: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
        screenshots = _computer_monitoring_screenshots(item)
        events.append(
            {
                "id": item.get("id"),
                "timestamp": item.get("date"),
                "incident": item.get("incident"),
                "issue": item.get("_issue"),
                "type": detail.get("type"),
                "program_name": detail.get("programName"),
                "window_title": detail.get("windowTitle"),
                "text_copied_pasted": detail.get("textCopiedPasted"),
                "detail": detail,
                "screenshots": screenshots,
                "source_url": "smow-api:/monitoring/evidence/computerMonitoring",
            }
        )
    return events


def _computer_monitoring_captures_from_reasons_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    captures: list[dict[str, Any]] = []
    activities = payload.get("activities")
    if not isinstance(activities, dict):
        return captures
    for activity_payload in activities.values():
        if not isinstance(activity_payload, dict):
            continue
        computer_monitoring = activity_payload.get("ComputerMonitoring")
        if not isinstance(computer_monitoring, dict):
            continue
        issues = computer_monitoring.get("issues")
        if not isinstance(issues, dict):
            continue
        for issue_name, issue_payload in issues.items():
            if not isinstance(issue_payload, dict):
                continue
            issue_captures = issue_payload.get("captures")
            if not isinstance(issue_captures, list):
                continue
            for capture in issue_captures:
                if isinstance(capture, dict):
                    captures.append({**capture, "_issue": issue_name})
    return captures


def _computer_monitoring_screenshots(item: dict[str, Any]) -> list[dict[str, Any]]:
    screenshots: list[dict[str, Any]] = []
    seen: set[str] = set()

    direct_src = item.get("src")
    if isinstance(direct_src, str) and direct_src:
        screenshots.append(_screenshot_record(direct_src, source="event.src"))
        seen.add(direct_src)

    detail = item.get("detail")
    for source_name, value in _walk_screenshot_sources(detail):
        if value in seen:
            continue
        seen.add(value)
        screenshots.append(_screenshot_record(value, source=source_name))
    return screenshots


def _walk_screenshot_sources(value: Any, path: str = "detail") -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key == "src" and isinstance(child, str) and child:
                sources.append((child_path, child))
            else:
                sources.extend(_walk_screenshot_sources(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            sources.extend(_walk_screenshot_sources(child, f"{path}[{index}]"))
    return sources


def _screenshot_record(url: str, *, source: str) -> dict[str, Any]:
    return {
        "source": source,
        "original_url": url,
        "local_path": None,
        "mime_type": mimetypes.guess_type(urlparse(url).path)[0],
        "size_bytes": None,
        "sha256": None,
        "status": FileStatus.PENDING.value,
        "error": None,
    }


def _link_downloaded_files_to_computer_monitoring(
    student: StudentRecord,
    downloaded_by_url: dict[str, ExtractedFile],
) -> None:
    for event in student.smow.computer_monitoring:
        screenshots = event.get("screenshots")
        if not isinstance(screenshots, list):
            continue
        for screenshot in screenshots:
            if not isinstance(screenshot, dict):
                continue
            url = screenshot.get("original_url")
            if not url:
                continue
            file = downloaded_by_url.get(url) or downloaded_by_url.get(_file_identity(url))
            if not file:
                continue
            screenshot.update(
                {
                    "local_path": file.local_path,
                    "mime_type": file.mime_type,
                    "size_bytes": file.size_bytes,
                    "sha256": file.sha256,
                    "status": file.status.value if hasattr(file.status, "value") else str(file.status),
                    "error": file.error,
                }
            )


def _payload_matches_activity(payload: dict[str, Any], activity_id: str | None) -> bool:
    if not activity_id:
        return True
    payload_activity = str(payload.get("activityName") or "")
    expected = f"test{activity_id}" if not activity_id.startswith("test") else activity_id
    return not payload_activity or payload_activity == expected


def _payload_user_id(payload: dict[str, Any]) -> str:
    user_id = payload.get("userId")
    if user_id:
        return str(user_id)
    user = payload.get("user")
    if isinstance(user, dict):
        user_id = user.get("userId") or user.get("id")
        if user_id:
            return str(user_id)
    return ""


def _files_from_evidence_payload(payload: dict[str, Any]) -> list[ExtractedFile]:
    return _files_from_payload({"evidence": payload.get("evidence")})


def _files_from_payload(payload: Any) -> list[ExtractedFile]:
    files: list[ExtractedFile] = []
    seen: set[str] = set()
    for url in _nested_file_urls(payload):
        key = _file_identity(url)
        if key in seen:
            continue
        seen.add(key)
        files.append(
            ExtractedFile(
                original_url=url,
                mime_type=mimetypes.guess_type(urlparse(url).path)[0],
                status=FileStatus.PENDING,
            )
        )
    return files


def _nested_sources(value: Any) -> list[str]:
    return _nested_file_urls(value, src_only=True)


def _nested_file_urls(value: Any, *, src_only: bool = False) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, str) and _is_file_url(child, key, src_only=src_only):
                urls.append(child)
            else:
                urls.extend(_nested_file_urls(child, src_only=src_only))
    elif isinstance(value, list):
        for child in value:
            urls.extend(_nested_file_urls(child, src_only=src_only))
    return urls


def _is_file_url(value: str, key: str, *, src_only: bool = False) -> bool:
    if not value:
        return False
    if src_only and key != "src":
        return False
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return False
    lowered = value.lower()
    path = parsed.path.lower()
    if key in {"src", "url", "href", "image", "photo", "file"} and any(
        marker in lowered
        for marker in (
            "smowlireland.s3",
            "smowl-prod-cm.s3",
            "front-results.smowltech.net/imagenes",
        )
    ):
        return True
    return path.endswith((".jpg", ".jpeg", ".png", ".webp", ".pdf", ".zip"))


def _events_from_figures(student_id: str, figures: dict[str, Any]) -> list[SmowEvent]:
    events: list[SmowEvent] = []
    activity = figures.get("activity")
    if not isinstance(activity, dict):
        return events
    for service_name, service_payload in activity.items():
        if not isinstance(service_payload, dict):
            continue
        status = service_payload.get("status")
        events.append(
            SmowEvent(
                label=str(service_name),
                value=str(status) if status is not None else None,
                source_url="smow-api:/results/figures",
            )
        )
        service_figures = service_payload.get("figures")
        if isinstance(service_figures, dict):
            for key, value in service_figures.items():
                if _is_empty_figure(value):
                    continue
                events.append(
                    SmowEvent(
                        label=f"{service_name}.{key}",
                        value=str(value),
                        source_url="smow-api:/results/figures",
                    )
                )
        service_issues = service_payload.get("issues")
        if isinstance(service_issues, dict):
            for key, value in service_issues.items():
                if not isinstance(value, dict):
                    if _is_empty_figure(value):
                        continue
                    events.append(
                        SmowEvent(
                            label=f"{service_name}.{key}",
                            value=str(value),
                            source_url="smow-api:/results/reasons/allActiveServices",
                        )
                    )
                    continue
                for metric, metric_value in value.items():
                    if _is_empty_figure(metric_value):
                        continue
                    events.append(
                        SmowEvent(
                            label=f"{service_name}.{key}.{metric}",
                            value=str(metric_value),
                            source_url="smow-api:/results/reasons/allActiveServices",
                        )
                    )
    return events


def _service_statuses_from_figures(figures: dict[str, Any]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    activity = figures.get("activity")
    if not isinstance(activity, dict):
        return statuses
    for service_name, service_payload in activity.items():
        if not isinstance(service_payload, dict):
            continue
        status = service_payload.get("status")
        if status is not None:
            statuses[str(service_name)] = str(status)
    return statuses


def _events_from_dashboard_row(row: Any, base_url: str | None) -> list[SmowEvent]:
    events: list[SmowEvent] = []
    for cell in row.find_all("td"):
        cell_id = str(cell.get("id") or "")
        if not cell_id.startswith("dt-") or cell_id in {"dt-status", "dt-id", "dt-username", "dt-actions"}:
            continue
        service_name = cell_id.removeprefix("dt-")
        data_filter = clean_text(cell.get("data-filter") or "")
        data_order = clean_text(cell.get("data-order") or "")
        value = clean_text(cell.get_text(" "))
        status = _status_from_filter(data_filter)
        parts = []
        if status:
            parts.append(f"status={status}")
        if value:
            parts.append(f"count={value}")
        if data_order:
            parts.append(f"order={data_order}")
        events.append(
            SmowEvent(
                label=service_name,
                value="; ".join(parts) or None,
                source_url=base_url,
            )
        )
    return events


def _status_from_filter(value: str) -> str | None:
    normalized = value.lower()
    if "issue" in normalized or "unsuccessful" in normalized:
        return "UNSUCCESSFUL"
    if "pending" in normalized:
        return "PENDING"
    if "correct" in normalized or "successful" in normalized:
        return "SUCCESSFUL"
    return None


def _is_empty_figure(value: Any) -> bool:
    return value is None or value is False or value == 0 or value == ""


def _filename_from_url(url: str, index: int) -> str:
    path_name = Path(urlparse(url).path).name
    return f"{index:04d}-{path_name or 'file'}"


def _pick(data: dict[str, str], *keys: str) -> str | None:
    for wanted in keys:
        for key, value in data.items():
            if wanted in key and value:
                return value
    return None


def _dedupe_students(students: list[StudentRecord]) -> list[StudentRecord]:
    result: list[StudentRecord] = []
    seen: set[str] = set()
    for student in students:
        if student.id in seen:
            continue
        seen.add(student.id)
        result.append(student)
    return result


def _dedupe_flags(flags: list[SmowFlag]) -> list[SmowFlag]:
    result: list[SmowFlag] = []
    seen: set[str] = set()
    for flag in flags:
        if flag.label in seen:
            continue
        seen.add(flag.label)
        result.append(flag)
    return result


def _dedupe_files(files: list[ExtractedFile]) -> list[ExtractedFile]:
    result: list[ExtractedFile] = []
    seen: set[str] = set()
    for file in files:
        key = _file_identity(file.original_url) if file.original_url else file.local_path or ""
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(file)
    return result


def _file_identity(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _dedupe_computer_monitoring_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for event in events:
        key = (event.get("id"), event.get("timestamp"), event.get("type"))
        if key in seen:
            continue
        seen.add(key)
        result.append(event)
    return result
