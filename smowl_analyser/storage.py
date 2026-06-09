from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .models import (
    ExtractedFile,
    ExtractionReport,
    FileStatus,
    Manifest,
    Progress,
    Selection,
    StudentRecord,
    jsonable,
)


PROJECT_ROOT = Path.cwd()
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_RUNS_DIR = DEFAULT_DATA_DIR / "runs"
DEFAULT_SECRETS_DIR = PROJECT_ROOT / ".secrets"
DEFAULT_STATE_PATH = DEFAULT_SECRETS_DIR / "playwright-state.json"


def utc_now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def safe_slug(value: str, fallback: str = "item") -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-._")
    return slug[:120] or fallback


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class RunStorage:
    def __init__(self, runs_dir: Path = DEFAULT_RUNS_DIR, run_id: str | None = None) -> None:
        self.run_id = run_id or utc_now_compact()
        self.root = runs_dir / self.run_id
        self.files_dir = self.root / "files"

    @property
    def report_path(self) -> Path:
        return self.root / "report.json"

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def selection_path(self) -> Path:
        return self.root / "selection.json"

    @property
    def progress_path(self) -> Path:
        return self.root / "progress.json"

    def prepare(self) -> None:
        self.files_dir.mkdir(parents=True, exist_ok=True)

    def student_dir(self, student_id: str) -> Path:
        return self.root / "students" / safe_slug(student_id, "student")

    def student_computer_monitoring_path(self, student_id: str) -> Path:
        return self.student_dir(student_id) / "computer_monitoring.json"

    def save_student_computer_monitoring(self, student: StudentRecord) -> None:
        write_json(
            self.student_computer_monitoring_path(student.id),
            {
                "student": {
                    "id": student.id,
                    "name": student.name,
                    "email": student.email,
                    "registration": student.registration,
                },
                "smow_summary": {
                    "global_status": student.status,
                    "service_statuses": student.smow.service_statuses,
                },
                "computer_monitoring": student.smow.computer_monitoring,
            },
        )

    def load_student_computer_monitoring(self, student_id: str) -> dict[str, Any] | None:
        path = self.student_computer_monitoring_path(student_id)
        if not path.exists():
            return None
        return read_json(path)

    def save_selection(self, selection: Selection) -> None:
        write_json(self.selection_path, jsonable(selection))

    def load_selection(self) -> Selection:
        return Selection.model_validate(read_json(self.selection_path))

    def save_report(self, report: ExtractionReport) -> None:
        write_json(self.report_path, compact_report_payload(jsonable(report)))

    def load_report(self) -> ExtractionReport:
        return ExtractionReport.model_validate(read_json(self.report_path))

    def save_manifest(self, manifest: Manifest) -> None:
        write_json(self.manifest_path, jsonable(manifest))

    def load_manifest(self) -> Manifest:
        return Manifest.model_validate(read_json(self.manifest_path))

    def save_progress(self, progress: Progress) -> None:
        write_json(self.progress_path, jsonable(progress))

    def load_progress(self) -> Progress:
        return Progress.model_validate(read_json(self.progress_path))

    def create_manifest(self, urls: list[str] | None = None) -> Manifest:
        return Manifest(
            run_id=self.run_id,
            extractor_version=__version__,
            started_at=datetime.now(timezone.utc),
            urls=urls or [],
        )

    def student_file_path(self, student_id: str, filename: str) -> Path:
        return self.files_dir / safe_slug(student_id, "student") / safe_slug(filename, "file")

    def save_file(
        self,
        student_id: str,
        filename: str,
        content: bytes,
        original_url: str | None = None,
        mime_type: str | None = None,
    ) -> ExtractedFile:
        path = self.student_file_path(student_id, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        return ExtractedFile(
            local_path=path.relative_to(self.root).as_posix(),
            original_url=original_url,
            mime_type=mime_type,
            size_bytes=len(content),
            sha256=digest,
            status=FileStatus.DOWNLOADED,
        )

    def failed_file(self, original_url: str, error: str) -> ExtractedFile:
        return ExtractedFile(
            original_url=original_url,
            status=FileStatus.FAILED,
            error=error,
        )


def compact_report_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for student in payload.get("students", []):
        if not isinstance(student, dict):
            continue
        student.pop("files", None)
        smow = student.get("smow")
        if isinstance(smow, dict):
            smow.pop("computer_monitoring", None)
    return payload
