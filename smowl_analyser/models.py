from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FileStatus(str, Enum):
    PENDING = "pending"
    DOWNLOADED = "downloaded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ProgressStatus(str, Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class LinkOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    url: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    institution: str | None = None
    executor: str | None = None
    course: LinkOption | None = None
    activity: LinkOption | None = None
    report: LinkOption | None = None


class SmowFlag(BaseModel):
    model_config = ConfigDict(extra="allow")

    label: str
    value: str | None = None
    timestamp: str | None = None
    source_url: str | None = None


class SmowEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    label: str
    value: str | None = None
    timestamp: str | None = None
    source_url: str | None = None


class ExtractedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_path: str | None = None
    original_url: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    status: FileStatus = FileStatus.PENDING
    error: str | None = None


class SmowData(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str | None = None
    score: str | float | int | None = None
    service_statuses: dict[str, str] = Field(default_factory=dict)
    flags: list[SmowFlag] = Field(default_factory=list)
    events: list[SmowEvent] = Field(default_factory=list)
    computer_monitoring: list[dict[str, Any]] = Field(default_factory=list)
    source_url: str | None = None


class StudentRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    email: str | None = None
    registration: str | None = None
    status: str | None = None
    source_url: str | None = None
    smow: SmowData = Field(default_factory=SmowData)
    files: list[ExtractedFile] = Field(default_factory=list)


class ExtractionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: RunInfo
    students: list[StudentRecord] = Field(default_factory=list)


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    extractor_version: str
    started_at: datetime
    finished_at: datetime | None = None
    urls: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class ProgressEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    student_id: str
    status: ProgressStatus = ProgressStatus.PENDING
    error: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class Progress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    entries: dict[str, ProgressEntry] = Field(default_factory=dict)

    def ensure_students(self, students: list[StudentRecord]) -> None:
        for student in students:
            self.entries.setdefault(student.id, ProgressEntry(student_id=student.id))

    def pending_student_ids(self) -> list[str]:
        return [
            student_id
            for student_id, entry in self.entries.items()
            if entry.status in {ProgressStatus.PENDING, ProgressStatus.FAILED}
        ]

    def mark_done(self, student_id: str) -> None:
        self.entries[student_id] = ProgressEntry(
            student_id=student_id,
            status=ProgressStatus.DONE,
        )

    def mark_failed(self, student_id: str, error: str) -> None:
        self.entries[student_id] = ProgressEntry(
            student_id=student_id,
            status=ProgressStatus.FAILED,
            error=error,
        )


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course: LinkOption | None = None
    activity: LinkOption | None = None
    report: LinkOption | None = None


def jsonable(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=False)


def path_to_json_string(path: Path) -> str:
    return path.as_posix()
