from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .learning import (
    sanitize_headers,
    sanitize_html,
    sanitize_json,
    sanitize_post_data,
    sanitize_url,
    summarize_html,
)
from .storage import DEFAULT_DATA_DIR, safe_slug, utc_now_compact, write_json


DEFAULT_DIAGNOSTICS_DIR = DEFAULT_DATA_DIR / "diagnostics"

SMOW_ENDPOINTS = {
    "lti_ajax_activities": "/lti/ajax/activities",
    "lti_ajax_students": "/lti/ajax/students",
    "lti_ajax_course": "/lti/ajax/blackboard/course",
    "results_figures": "/V2/results/figures",
    "results_reasons_all_services": "/V2/results/reasons/allActiveServices",
    "results_reasons_captures": "/V2/resultsReasonsCaptures",
    "monitoring_evidence": "/V2/monitoring/evidence",
    "review_status": "/V2/Review/Status",
    "registers_status": "/V2/registers/status/user/includePhotos",
}


@dataclass
class DiagnosticSession:
    root: Path
    session_id: str
    responses: list[dict[str, Any]] = field(default_factory=list)
    json_responses: list[dict[str, Any]] = field(default_factory=list)
    pages: list[dict[str, Any]] = field(default_factory=list)
    frames: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        base_dir: Path = DEFAULT_DIAGNOSTICS_DIR,
        session_id: str | None = None,
    ) -> "DiagnosticSession":
        resolved_id = session_id or utc_now_compact()
        root = base_dir / resolved_id
        for child in ("pages", "frames", "network"):
            (root / child).mkdir(parents=True, exist_ok=True)
        return cls(root=root, session_id=resolved_id)

    def attach_context(self, context: Any) -> None:
        context.on("response", self._record_response)

    def capture_context(self, context: Any) -> dict[str, Any]:
        self.pages = []
        self.frames = []
        for page_index, page in enumerate(getattr(context, "pages", []), start=1):
            self._capture_page(page, page_index)
        self.flush()
        return self.summary()

    def summary(self) -> dict[str, Any]:
        endpoint_counts = endpoint_counter(self.responses)
        domain_counts = Counter(_domain(response.get("url", "")) for response in self.responses)
        return {
            "session_id": self.session_id,
            "extractor_version": __version__,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "root": self.root.as_posix(),
            "counts": {
                "pages": len(self.pages),
                "frames": len(self.frames),
                "responses": len(self.responses),
                "json_responses": len(self.json_responses),
            },
            "smow": {
                "endpoint_counts": endpoint_counts,
                "has_student_api": endpoint_counts["lti_ajax_students"] > 0,
                "has_results_api": (
                    endpoint_counts["results_figures"] > 0
                    or endpoint_counts["results_reasons_all_services"] > 0
                ),
                "has_figures_api": endpoint_counts["results_figures"] > 0,
                "has_evidence_api": endpoint_counts["monitoring_evidence"] > 0,
                "has_smowlresults_frame": any(frame.get("name") == "smowlresults" for frame in self.frames),
                "has_front_results_frame": any(
                    "front-results.smowltech.net" in frame.get("url", "") for frame in self.frames
                ),
            },
            "domains": dict(domain_counts.most_common(25)),
            "recent_smow_responses": [
                response
                for response in self.responses
                if _is_smowish(response.get("url", ""))
            ][-50:],
            "pages": self.pages,
            "frames": self.frames,
        }

    def flush(self) -> None:
        write_json(self.root / "summary.json", self.summary())
        write_json(
            self.root / "network" / "responses.json",
            {
                "session_id": self.session_id,
                "responses": self.responses,
            },
        )
        write_json(
            self.root / "network" / "json_responses.json",
            {
                "session_id": self.session_id,
                "responses": self.json_responses,
            },
        )
        write_json(self.root / "pages.json", {"pages": self.pages})
        write_json(self.root / "frames.json", {"frames": self.frames})
        self._write_notes()

    def _record_response(self, response: Any) -> None:
        content_type = response.headers.get("content-type", "")
        request = _safe_call(lambda: response.request)
        frame = _safe_call(lambda: response.frame)
        request_headers = _safe_call(lambda: request.headers) if request is not None else None
        post_data = _safe_call(lambda: request.post_data) if request is not None else None
        record = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "url": sanitize_url(getattr(response, "url", "")),
            "status": getattr(response, "status", None),
            "content_type": content_type,
            "method": getattr(request, "method", "") if request is not None else "",
            "resource_type": getattr(request, "resource_type", "") if request is not None else "",
            "frame_url": sanitize_url(getattr(frame, "url", "")) if frame is not None else "",
            "request_headers": sanitize_headers(request_headers or {}),
            "request_post_data": sanitize_post_data(post_data),
        }
        self.responses.append(record)
        if "json" not in content_type.lower():
            return
        try:
            payload = response.json()
        except Exception:
            return
        self.json_responses.append({**record, "payload": sanitize_json(payload)})

    def _capture_page(self, page: Any, page_index: int) -> None:
        title = _safe_call(lambda: page.title()) or ""
        url = sanitize_url(getattr(page, "url", ""))
        html = _safe_call(lambda: page.content()) or ""
        page_record = {
            "index": page_index,
            "title": title,
            "url": url,
            "frame_count": len(getattr(page, "frames", [])),
        }
        if html:
            stem = f"page-{page_index:02d}-{safe_slug(title or 'page')}"
            sanitized_html = sanitize_html(html, getattr(page, "url", ""))
            html_path = self.root / "pages" / f"{stem}.html"
            summary_path = self.root / "pages" / f"{stem}.summary.json"
            html_path.write_text(sanitized_html, encoding="utf-8")
            write_json(
                summary_path,
                {
                    **page_record,
                    "html_sha256": hashlib.sha256(sanitized_html.encode("utf-8")).hexdigest(),
                    "summary": summarize_html(html, getattr(page, "url", "")),
                },
            )
            page_record["html_path"] = html_path.relative_to(self.root).as_posix()
            page_record["summary_path"] = summary_path.relative_to(self.root).as_posix()
        self.pages.append(page_record)
        for frame_index, frame in enumerate(getattr(page, "frames", []), start=1):
            self._capture_frame(frame, page_index, frame_index)

    def _capture_frame(self, frame: Any, page_index: int, frame_index: int) -> None:
        name = getattr(frame, "name", "")
        url = sanitize_url(getattr(frame, "url", ""))
        frame_record = {
            "page_index": page_index,
            "index": frame_index,
            "name": name,
            "url": url,
            "is_smowish": _is_smowish(url) or name == "smowlresults",
        }
        html = _safe_call(lambda: frame.content()) or ""
        if html:
            stem = f"page-{page_index:02d}-frame-{frame_index:02d}-{safe_slug(name or 'frame')}"
            sanitized_html = sanitize_html(html, getattr(frame, "url", ""))
            html_path = self.root / "frames" / f"{stem}.html"
            summary_path = self.root / "frames" / f"{stem}.summary.json"
            html_path.write_text(sanitized_html, encoding="utf-8")
            write_json(
                summary_path,
                {
                    **frame_record,
                    "html_sha256": hashlib.sha256(sanitized_html.encode("utf-8")).hexdigest(),
                    "summary": summarize_html(html, getattr(frame, "url", "")),
                },
            )
            frame_record["html_path"] = html_path.relative_to(self.root).as_posix()
            frame_record["summary_path"] = summary_path.relative_to(self.root).as_posix()
        self.frames.append(frame_record)

    def _write_notes(self) -> None:
        summary = self.summary()
        notes = [
            "# SMOW Diagnostic",
            "",
            f"- Session: `{self.session_id}`",
            f"- Generated at: `{summary['generated_at']}`",
            f"- Extractor version: `{__version__}`",
            "",
            "## SMOW Signals",
            "",
        ]
        for key, value in summary["smow"]["endpoint_counts"].items():
            notes.append(f"- `{key}`: {value}")
        notes.extend(
            [
                f"- `has_smowlresults_frame`: {summary['smow']['has_smowlresults_frame']}",
                f"- `has_front_results_frame`: {summary['smow']['has_front_results_frame']}",
                "",
                "## Privacy",
                "",
                "HTML, URLs and JSON payloads were sanitized. No screenshots or images were saved.",
            ]
        )
        (self.root / "notes.md").write_text("\n".join(notes) + "\n", encoding="utf-8")


def endpoint_counter(responses: list[dict[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key in SMOW_ENDPOINTS}
    counts.update(
        {
            "front_results_requests": 0,
            "lti_smowl_requests": 0,
            "results_api_requests": 0,
            "blackboard_requests": 0,
        }
    )
    for response in responses:
        url = response.get("url", "")
        for key, needle in SMOW_ENDPOINTS.items():
            if needle in url:
                counts[key] += 1
        if "front-results.smowltech.net" in url:
            counts["front_results_requests"] += 1
        if "lti-smowl-global.smowltech.net" in url:
            counts["lti_smowl_requests"] += 1
        if "results-api.smowltech.net" in url:
            counts["results_api_requests"] += 1
        if "blackboard.com" in url or "insper.blackboard.com" in url:
            counts["blackboard_requests"] += 1
    return counts


def _domain(url: str) -> str:
    return urlsplit(url).netloc or "(unknown)"


def _is_smowish(url: str) -> bool:
    return any(
        value in url
        for value in (
            "smowl",
            "front-results.smowltech.net",
            "results-api.smowltech.net",
            "ActivityStatus",
        )
    )


def _safe_call(callback):
    try:
        return callback()
    except Exception:
        return None
