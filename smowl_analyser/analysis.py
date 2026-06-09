from __future__ import annotations

import html
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .storage import RunStorage, read_json, write_json


HIGH_RISK_CLOSE_TYPES = {
    "CM_CLOSED_MANUALLY",
    "CM_CLOSED_BEFORE_ENDING",
    "CM_CLOSED_BEFORE_ACTIVITY_END",
}
PROGRAM_ALLOWLIST = {
    "python.exe",
}
PROGRAM_ALLOWLIST_FRAGMENTS = (
    "chrome",
    "edge",
    "firefox",
    "safari",
    "blackboard",
    "smow",
    "smowl",
    "prairielearn",
)
TITLE_ALLOWLIST_FRAGMENTS = (
    "prairielearn",
    "blackboard",
    "smow",
    "smowl",
    "avaliação protegida",
    "avaliacao protegida",
    "inicialização lti",
    "inicializacao lti",
)
CODE_MARKERS = (
    "def ",
    "class ",
    "import ",
    "from ",
    "for ",
    "while ",
    "return",
    "console.log",
    "print(",
    "function ",
    "=>",
    "==",
    "=",
    "{",
    "}",
    "[",
    "]",
    "(",
    ")",
    ";",
)


@dataclass
class StudentMonitoring:
    id: str
    name: str
    events: list[dict[str, Any]]
    source_path: Path


@dataclass
class Finding:
    category: str
    severity: str
    student_id: str
    student_name: str
    timestamp: str | None
    title: str
    detail: str
    evidence_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AnalysisResult:
    run_id: str
    student_count: int
    findings: list[Finding]
    rare_program_counts: dict[str, int]
    rare_title_counts: dict[str, int]

    def findings_by_category(self, category: str) -> list[Finding]:
        return [finding for finding in self.findings if finding.category == category]

    def findings_by_student(self) -> dict[str, list[Finding]]:
        grouped: dict[str, list[Finding]] = defaultdict(list)
        for finding in self.findings:
            grouped[finding.student_name].append(finding)
        return dict(sorted(grouped.items()))


def analyze_run(storage: RunStorage, rarity_ratio: float = 0.10) -> AnalysisResult:
    students = load_student_monitoring(storage)
    student_count = len(students)
    rare_programs = _rare_values_by_student(
        students,
        event_type="OPENED_PROGRAM",
        field="program_name",
        rarity_ratio=rarity_ratio,
        allowlist=_is_allowed_program,
        normalizer=normalize_program,
    )
    rare_titles = _rare_values_by_student(
        students,
        event_type="CM_WEB_NAVIGATION_OUTSIDE_EXAM",
        field="window_title",
        rarity_ratio=rarity_ratio,
        allowlist=_is_allowed_title,
        normalizer=normalize_title,
    )

    findings: list[Finding] = []
    for student in students:
        findings.extend(_monitoring_relaunch_findings(student))
        findings.extend(_rare_program_findings(student, rare_programs))
        findings.extend(_rare_title_findings(student, rare_titles))
        findings.extend(_multiline_code_clipboard_findings(student))

    findings.sort(key=lambda item: (_severity_order(item.severity), item.student_name, item.timestamp or ""))
    return AnalysisResult(
        run_id=storage.run_id,
        student_count=student_count,
        findings=findings,
        rare_program_counts={value: len(student_ids) for value, student_ids in rare_programs.items()},
        rare_title_counts={value: len(student_ids) for value, student_ids in rare_titles.items()},
    )


def load_student_monitoring(storage: RunStorage) -> list[StudentMonitoring]:
    students: list[StudentMonitoring] = []
    for path in sorted((storage.root / "students").glob("*/computer_monitoring.json")):
        payload = read_json(path)
        student_payload = payload.get("student") if isinstance(payload.get("student"), dict) else {}
        events = payload.get("computer_monitoring")
        if not isinstance(events, list):
            events = []
        student_id = str(student_payload.get("id") or path.parent.name)
        students.append(
            StudentMonitoring(
                id=student_id,
                name=str(student_payload.get("name") or student_id),
                events=[event for event in events if isinstance(event, dict)],
                source_path=path,
            )
        )
    return students


def write_analysis_html(storage: RunStorage, result: AnalysisResult) -> Path:
    output_path = storage.root / "analysis" / "report.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_analysis_html(result), encoding="utf-8")
    write_json(
        storage.root / "analysis" / "summary.json",
        {
            "run_id": result.run_id,
            "student_count": result.student_count,
            "findings": [_finding_payload(finding) for finding in result.findings],
            "rare_program_counts": result.rare_program_counts,
            "rare_title_counts": result.rare_title_counts,
        },
    )
    return output_path


def _monitoring_relaunch_findings(student: StudentMonitoring) -> list[Finding]:
    events = sorted(student.events, key=lambda event: event.get("timestamp") or "")
    findings: list[Finding] = []
    for index, event in enumerate(events):
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type.startswith("CM_CLOSED"):
            continue
        launch = next(
            (
                candidate
                for candidate in events[index + 1 :]
                if candidate.get("type") == "CM_LAUNCHED"
                and (candidate.get("timestamp") or "") > (event.get("timestamp") or "")
            ),
            None,
        )
        if not launch:
            continue
        interval = _format_interval(event.get("timestamp"), launch.get("timestamp"))
        severity = "high" if event_type in HIGH_RISK_CLOSE_TYPES else "medium"
        findings.append(
            Finding(
                category="monitoring_relaunch",
                severity=severity,
                student_id=student.id,
                student_name=student.name,
                timestamp=event.get("timestamp"),
                title="Monitoramento fechado e relançado",
                detail=(
                    f"{event_type} em {event.get('timestamp') or '?'}; "
                    f"CM_LAUNCHED em {launch.get('timestamp') or '?'}; intervalo {interval}."
                ),
                evidence_path=_first_screenshot_path(event) or _first_screenshot_path(launch),
                metadata={
                    "closed_type": event_type,
                    "closed_at": event.get("timestamp"),
                    "relaunched_at": launch.get("timestamp"),
                    "interval": interval,
                },
            )
        )
    return findings


def _rare_program_findings(
    student: StudentMonitoring,
    rare_programs: dict[str, set[str]],
) -> list[Finding]:
    return _rare_value_findings(
        student,
        category="rare_program",
        event_type="OPENED_PROGRAM",
        field="program_name",
        title_prefix="Programa incomum",
        rare_values=rare_programs,
        normalizer=normalize_program,
    )


def _rare_title_findings(
    student: StudentMonitoring,
    rare_titles: dict[str, set[str]],
) -> list[Finding]:
    return _rare_value_findings(
        student,
        category="rare_window_title",
        event_type="CM_WEB_NAVIGATION_OUTSIDE_EXAM",
        field="window_title",
        title_prefix="Título/site incomum",
        rare_values=rare_titles,
        normalizer=normalize_title,
    )


def _rare_value_findings(
    student: StudentMonitoring,
    *,
    category: str,
    event_type: str,
    field: str,
    title_prefix: str,
    rare_values: dict[str, set[str]],
    normalizer,
) -> list[Finding]:
    by_value: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in student.events:
        if event.get("type") != event_type:
            continue
        normalized = normalizer(str(event.get(field) or ""))
        if normalized in rare_values:
            by_value[normalized].append(event)
    findings: list[Finding] = []
    for value, events in sorted(by_value.items()):
        first = sorted(events, key=lambda event: event.get("timestamp") or "")[0]
        findings.append(
            Finding(
                category=category,
                severity="medium",
                student_id=student.id,
                student_name=student.name,
                timestamp=first.get("timestamp"),
                title=f"{title_prefix}: {value}",
                detail=(
                    f"Encontrado {len(events)} vez(es) para este aluno; "
                    f"apareceu em {len(rare_values[value])} aluno(s) da turma."
                ),
                evidence_path=_first_screenshot_path(first),
                metadata={"value": value, "event_count": len(events), "cohort_student_count": len(rare_values[value])},
            )
        )
    return findings


def _multiline_code_clipboard_findings(student: StudentMonitoring) -> list[Finding]:
    findings: list[Finding] = []
    for event in student.events:
        if event.get("type") not in {"CM_TEXT_COPIED", "CM_TEXT_PASTED"}:
            continue
        text = event.get("text_copied_pasted")
        if not isinstance(text, str):
            detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
            text = detail.get("textCopiedPasted")
        if not isinstance(text, str) or not text.strip():
            continue
        non_empty_lines = [line for line in text.splitlines() if line.strip()]
        if len(non_empty_lines) < 2:
            continue
        looks_code = looks_like_code(text)
        if looks_code and len(non_empty_lines) >= 5:
            severity = "high"
        elif looks_code:
            severity = "medium"
        else:
            severity = "low"
        findings.append(
            Finding(
                category="multiline_code_clipboard",
                severity=severity,
                student_id=student.id,
                student_name=student.name,
                timestamp=event.get("timestamp"),
                title="Cópia/cola de múltiplas linhas",
                detail=f"{event.get('type')} com {len(non_empty_lines)} linha(s) não vazias.",
                evidence_path=_first_screenshot_path(event),
                metadata={
                    "event_type": event.get("type"),
                    "line_count": len(non_empty_lines),
                    "looks_like_code": looks_code,
                    "text_preview": preview_text(text),
                },
            )
        )
    return findings


def _rare_values_by_student(
    students: list[StudentMonitoring],
    *,
    event_type: str,
    field: str,
    rarity_ratio: float,
    allowlist,
    normalizer,
) -> dict[str, set[str]]:
    value_students: dict[str, set[str]] = defaultdict(set)
    for student in students:
        for event in student.events:
            if event.get("type") != event_type:
                continue
            value = normalizer(str(event.get(field) or ""))
            if not value or allowlist(value):
                continue
            value_students[value].add(student.id)
    threshold = max(1, math.floor(len(students) * rarity_ratio))
    return {
        value: student_ids
        for value, student_ids in value_students.items()
        if len(student_ids) <= threshold
    }


def normalize_program(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def normalize_title(value: str) -> str:
    normalized = value.strip().lower().replace("\u200b", "")
    normalized = re.sub(r"\s+e mais\s+\d+\s+p[aá]ginas.*$", "", normalized)
    normalized = re.sub(r"\s+-\s+perfil\s+\d+\s+—\s+.*$", "", normalized)
    normalized = re.sub(r"\s+-\s+pessoal\s+—\s+.*$", "", normalized)
    normalized = re.sub(r"\s+—\s+microsoft.*$", "", normalized)
    normalized = re.sub(r"\s+-\s+google chrome.*$", "", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def looks_like_code(text: str) -> bool:
    lowered = text.lower()
    marker_hits = sum(1 for marker in CODE_MARKERS if marker in lowered)
    has_indentation = any(line.startswith(("    ", "\t")) for line in text.splitlines())
    return marker_hits >= 2 or has_indentation


def preview_text(text: str, max_chars: int = 240) -> str:
    compact = re.sub(r"\s+", " ", text.strip())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + "…"


def render_analysis_html(result: AnalysisResult) -> str:
    category_titles = {
        "monitoring_relaunch": "Monitoramento fechado e relançado",
        "rare_program": "Programas incomuns",
        "rare_window_title": "Sites/títulos incomuns",
        "multiline_code_clipboard": "Cópia/cola de múltiplas linhas",
    }
    sections = "\n".join(
        _render_findings_table(category_titles[category], result.findings_by_category(category))
        for category in category_titles
    )
    by_student_sections = "\n".join(
        _render_student_section(student_name, findings)
        for student_name, findings in result.findings_by_student().items()
    )
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <title>SMOW Analysis {html.escape(result.run_id)}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #202124; }}
    h1, h2, h3 {{ color: #111827; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0 28px; font-size: 14px; }}
    th, td {{ border: 1px solid #d1d5db; padding: 8px; vertical-align: top; }}
    th {{ background: #f3f4f6; text-align: left; }}
    .severity-high {{ color: #991b1b; font-weight: 700; }}
    .severity-medium {{ color: #92400e; font-weight: 700; }}
    .severity-low {{ color: #1d4ed8; font-weight: 700; }}
    .empty {{ color: #6b7280; }}
    code {{ white-space: pre-wrap; }}
  </style>
</head>
<body>
  <h1>SMOW Analysis</h1>
  <p>Run: <strong>{html.escape(result.run_id)}</strong></p>
  <p>{result.student_count} aluno(s), {len(result.findings)} achado(s) para revisão humana.</p>
  {sections}
  <h2>Achados por aluno</h2>
  {by_student_sections or '<p class="empty">Nenhum achado por aluno.</p>'}
</body>
</html>
"""


def _render_findings_table(title: str, findings: list[Finding]) -> str:
    if not findings:
        return f"<h2>{html.escape(title)}</h2><p class=\"empty\">Nenhum achado.</p>"
    rows = "\n".join(_render_finding_row(finding) for finding in findings)
    return f"""<h2>{html.escape(title)}</h2>
<table>
  <thead>
    <tr><th>Severidade</th><th>Aluno</th><th>Horário</th><th>Achado</th><th>Detalhe</th><th>Evidência</th></tr>
  </thead>
  <tbody>{rows}</tbody>
</table>"""


def _render_student_section(student_name: str, findings: list[Finding]) -> str:
    rows = "\n".join(_render_finding_row(finding, include_student=False) for finding in findings)
    return f"""<h3>{html.escape(student_name)}</h3>
<table>
  <thead>
    <tr><th>Severidade</th><th>Horário</th><th>Categoria</th><th>Achado</th><th>Detalhe</th><th>Evidência</th></tr>
  </thead>
  <tbody>{rows}</tbody>
</table>"""


def _render_finding_row(finding: Finding, *, include_student: bool = True) -> str:
    severity = html.escape(finding.severity)
    evidence = _evidence_link(finding.evidence_path)
    detail = html.escape(finding.detail)
    if finding.metadata.get("text_preview"):
        detail += f"<br><code>{html.escape(str(finding.metadata['text_preview']))}</code>"
    cells = [
        f'<td class="severity-{severity}">{severity}</td>',
    ]
    if include_student:
        cells.append(f"<td>{html.escape(finding.student_name)}</td>")
    cells.append(f"<td>{html.escape(finding.timestamp or '')}</td>")
    if include_student:
        cells.append(f"<td>{html.escape(finding.title)}</td>")
    else:
        cells.append(f"<td>{html.escape(finding.category)}</td>")
        cells.append(f"<td>{html.escape(finding.title)}</td>")
    cells.extend([f"<td>{detail}</td>", f"<td>{evidence}</td>"])
    return "<tr>" + "".join(cells) + "</tr>"


def _evidence_link(path: str | None) -> str:
    if not path:
        return ""
    escaped = html.escape(path)
    return f'<a href="../{escaped}">{escaped}</a>'


def _is_allowed_program(value: str) -> bool:
    return value in PROGRAM_ALLOWLIST or any(fragment in value for fragment in PROGRAM_ALLOWLIST_FRAGMENTS)


def _is_allowed_title(value: str) -> bool:
    return any(fragment in value for fragment in TITLE_ALLOWLIST_FRAGMENTS)


def _first_screenshot_path(event: dict[str, Any]) -> str | None:
    screenshots = event.get("screenshots")
    if not isinstance(screenshots, list):
        return None
    for screenshot in screenshots:
        if isinstance(screenshot, dict) and screenshot.get("local_path"):
            return str(screenshot["local_path"])
    return None


def _format_interval(start: Any, end: Any) -> str:
    start_dt = _parse_timestamp(start)
    end_dt = _parse_timestamp(end)
    if not start_dt or not end_dt:
        return "desconhecido"
    seconds = int((end_dt - start_dt).total_seconds())
    if seconds < 0:
        return "desconhecido"
    minutes, remaining_seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}min {remaining_seconds}s"
    if minutes:
        return f"{minutes}min {remaining_seconds}s"
    return f"{remaining_seconds}s"


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt)
        except ValueError:
            continue
    return None


def _severity_order(severity: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(severity, 3)


def _finding_payload(finding: Finding) -> dict[str, Any]:
    return {
        "category": finding.category,
        "severity": finding.severity,
        "student_id": finding.student_id,
        "student_name": finding.student_name,
        "timestamp": finding.timestamp,
        "title": finding.title,
        "detail": finding.detail,
        "evidence_path": finding.evidence_path,
        "metadata": finding.metadata,
    }
