from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from . import __version__
from .extractors.html import clean_text
from .storage import DEFAULT_DATA_DIR, safe_slug, utc_now_compact, write_json


DEFAULT_LEARNING_DIR = DEFAULT_DATA_DIR / "learning"

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
LONG_NUMBER_RE = re.compile(r"\b\d{5,}\b")
TOKEN_KEYWORDS = (
    "access_token",
    "auth",
    "authorization",
    "code",
    "jwt",
    "key",
    "password",
    "refresh_token",
    "secret",
    "session",
    "sid",
    "sig",
    "signature",
    "sso",
    "ticket",
    "token",
)
CHECKPOINTS = [
    ("course-page", "Chegue na página da turma e pressione Enter."),
    ("smow-students-list", "Abra o SMOW e chegue na lista de alunos monitorados; depois pressione Enter."),
    (
        "student-row-action",
        "Clique em um aluno ou em um ícone de detalhe/ação de um aluno e pressione Enter.",
    ),
    (
        "front-camera-detail",
        "Abra detalhes/evidências de FrontCamera para esse aluno e pressione Enter.",
    ),
    (
        "computer-monitoring-detail",
        "Abra detalhes/evidências de ComputerMonitoring para esse aluno e pressione Enter.",
    ),
    (
        "student-download-action",
        "Clique no botão de download do aluno ou de relatório individual e pressione Enter.",
    ),
    (
        "evidence-preview",
        "Abra uma evidência/imagem específica, se existir, e pressione Enter.",
    ),
]


@dataclass
class LearningSession:
    root: Path
    session_id: str
    route: list[dict[str, Any]] = field(default_factory=list)
    responses: list[dict[str, Any]] = field(default_factory=list)
    json_responses: list[dict[str, Any]] = field(default_factory=list)
    downloads: list[dict[str, Any]] = field(default_factory=list)
    _checkpoint_response_index: int = 0
    _checkpoint_json_response_index: int = 0
    _checkpoint_download_index: int = 0

    @classmethod
    def create(cls, base_dir: Path = DEFAULT_LEARNING_DIR, session_id: str | None = None) -> "LearningSession":
        resolved_id = session_id or utc_now_compact()
        root = base_dir / resolved_id
        (root / "pages").mkdir(parents=True, exist_ok=True)
        (root / "frames").mkdir(parents=True, exist_ok=True)
        (root / "page_summaries").mkdir(parents=True, exist_ok=True)
        (root / "network").mkdir(parents=True, exist_ok=True)
        return cls(root=root, session_id=resolved_id)

    def attach(self, page: Any) -> None:
        page.on("framenavigated", self._record_navigation)
        page.on("download", self._record_download)

    def attach_context(self, context: Any) -> None:
        context.on("response", self._record_response)
        context.on("page", self.attach)

    def capture_checkpoint(self, page: Any, index: int, name: str) -> dict[str, Any]:
        html = page.content()
        sanitized_html = sanitize_html(html, page.url)
        summary = summarize_html(html, page.url)
        stem = f"{index:03d}-{safe_slug(name)}"
        frame_metadata = self._capture_frames(page, stem)
        response_delta = self.responses[self._checkpoint_response_index :]
        json_response_delta = self.json_responses[self._checkpoint_json_response_index :]
        download_delta = self.downloads[self._checkpoint_download_index :]
        metadata = {
            "index": index,
            "name": name,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "title": _safe_call(lambda: page.title()) or "",
            "url": sanitize_url(page.url),
            "frames": frame_metadata,
            "html_sha256": hashlib.sha256(sanitized_html.encode("utf-8")).hexdigest(),
            "network_delta": {
                "responses": len(response_delta),
                "json_responses": len(json_response_delta),
                "downloads": len(download_delta),
                "smow_endpoint_counts": _smow_endpoint_counts(response_delta),
                "recent_smow_urls": [
                    response["url"] for response in response_delta if _is_smowish(response["url"])
                ][-50:],
            },
            "summary": summary,
        }
        (self.root / "pages" / f"{stem}.html").write_text(sanitized_html, encoding="utf-8")
        write_json(self.root / "page_summaries" / f"{stem}.json", metadata)
        write_json(
            self.root / "network" / f"{stem}-responses.json",
            {"responses": response_delta},
        )
        write_json(
            self.root / "network" / f"{stem}-json_responses.json",
            {"responses": json_response_delta},
        )
        write_json(
            self.root / "network" / f"{stem}-downloads.json",
            {"downloads": download_delta},
        )
        self.route.append(
            {
                "type": "checkpoint",
                "index": index,
                "name": name,
                "url": metadata["url"],
                "title": metadata["title"],
                "captured_at": metadata["captured_at"],
                "summary_path": f"page_summaries/{stem}.json",
                "html_path": f"pages/{stem}.html",
                "network_delta": metadata["network_delta"],
            }
        )
        self._checkpoint_response_index = len(self.responses)
        self._checkpoint_json_response_index = len(self.json_responses)
        self._checkpoint_download_index = len(self.downloads)
        self.flush()
        return metadata

    def flush(self) -> None:
        write_json(
            self.root / "route.json",
            {
                "session_id": self.session_id,
                "extractor_version": __version__,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "route": self.route,
            },
        )
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
        write_json(
            self.root / "network" / "downloads.json",
            {
                "session_id": self.session_id,
                "downloads": self.downloads,
            },
        )
        self._write_notes()

    def _record_navigation(self, frame: Any) -> None:
        if getattr(frame, "parent_frame", None):
            return
        self.route.append(
            {
                "type": "navigation",
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "url": sanitize_url(frame.url),
            }
        )

    def _record_response(self, response: Any) -> None:
        content_type = response.headers.get("content-type", "")
        request = _safe_call(lambda: response.request)
        frame = _safe_call(lambda: response.frame)
        request_headers = _safe_call(lambda: request.headers) if request is not None else None
        post_data = _safe_call(lambda: request.post_data) if request is not None else None
        record = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "url": sanitize_url(response.url),
            "status": response.status,
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

    def _record_download(self, download: Any) -> None:
        self.downloads.append(
            {
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "url": sanitize_url(getattr(download, "url", "")),
                "suggested_filename": redact_text(getattr(download, "suggested_filename", "")),
            }
        )

    def _capture_frames(self, page: Any, stem: str) -> list[dict[str, Any]]:
        frames = []
        for frame_index, frame in enumerate(page.frames, start=1):
            frame_url = getattr(frame, "url", "")
            frame_name = getattr(frame, "name", "")
            frame_record = {
                "index": frame_index,
                "name": frame_name,
                "url": sanitize_url(frame_url),
                "is_smowish": _is_smowish(frame_url) or frame_name == "smowlresults",
            }
            html = _safe_call(lambda: frame.content()) or ""
            if html:
                frame_stem = f"{stem}-frame-{frame_index:02d}-{safe_slug(frame_name or 'frame')}"
                sanitized_html = sanitize_html(html, frame_url)
                frame_path = self.root / "frames" / f"{frame_stem}.html"
                frame_summary_path = self.root / "frames" / f"{frame_stem}.summary.json"
                frame_path.write_text(sanitized_html, encoding="utf-8")
                write_json(
                    frame_summary_path,
                    {
                        **frame_record,
                        "html_sha256": hashlib.sha256(sanitized_html.encode("utf-8")).hexdigest(),
                        "summary": summarize_html(html, frame_url),
                    },
                )
                frame_record["html_path"] = frame_path.relative_to(self.root).as_posix()
                frame_record["summary_path"] = frame_summary_path.relative_to(self.root).as_posix()
            frames.append(frame_record)
        return frames

    def _write_notes(self) -> None:
        notes = [
            "# Learning Session",
            "",
            f"- Session: `{self.session_id}`",
            f"- Generated at: `{datetime.now(timezone.utc).isoformat()}`",
            f"- Extractor version: `{__version__}`",
            "",
            "## Checkpoints",
            "",
        ]
        for item in self.route:
            if item.get("type") != "checkpoint":
                continue
            notes.append(
                f"{item['index']}. `{item['name']}` - `{item['title']}` - `{item['url']}`"
            )
        notes.extend(
            [
                "",
                "## Privacy",
                "",
                "HTML, URLs, forms and JSON responses were sanitized. No screenshots or images were saved.",
                "Network delta files show which requests appeared after each checkpoint.",
            ]
        )
        (self.root / "notes.md").write_text("\n".join(notes) + "\n", encoding="utf-8")


def sanitize_url(url: str | None) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    safe_query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if _is_sensitive_key(key):
            safe_query.append((key, "[REDACTED]"))
        else:
            safe_query.append((key, redact_text(value)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(safe_query), ""))


def sanitize_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _is_sensitive_key(str(key)) else sanitize_json(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [sanitize_json(item) for item in value[:200]]
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            return sanitize_url(value)
        return redact_text(value)
    return value


def sanitize_headers(headers: dict[str, Any]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key, value in headers.items():
        text = str(value)
        safe[str(key)] = "[REDACTED]" if _is_sensitive_key(str(key)) else redact_text(text)
    return safe


def sanitize_post_data(value: str | None) -> Any:
    if not value:
        return None
    try:
        return sanitize_json(json.loads(value))
    except Exception:
        return redact_text(value)


def sanitize_html(html: str, base_url: str | None = None) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(["script", "style", "noscript"]):
        node.decompose()
    for node in soup.find_all(True):
        for attr, value in list(node.attrs.items()):
            if attr.lower() in {"src", "href", "action"}:
                node[attr] = sanitize_url(str(value))
            elif attr.lower() in {"value", "placeholder"} or _is_sensitive_key(attr):
                node[attr] = "[REDACTED]"
            elif isinstance(value, str):
                node[attr] = redact_text(value)
            elif isinstance(value, list):
                node[attr] = [redact_text(str(item)) for item in value]
    for text_node in soup.find_all(string=True):
        redacted = redact_text(str(text_node))
        if redacted != str(text_node):
            text_node.replace_with(redacted)
    return str(soup)


def summarize_html(html: str, base_url: str | None = None) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    return {
        "counts": {
            "links": len(soup.find_all("a")),
            "buttons": len(soup.find_all("button")),
            "forms": len(soup.find_all("form")),
            "iframes": len(soup.find_all("iframe")),
            "tables": len(soup.find_all("table")),
            "images": len(soup.find_all("img")),
        },
        "links": _summarize_links(soup, base_url),
        "buttons": _summarize_buttons(soup),
        "forms": _summarize_forms(soup, base_url),
        "tables": _summarize_tables(soup),
        "iframes": _summarize_iframes(soup, base_url),
    }


def redact_text(value: str) -> str:
    value = EMAIL_RE.sub("[EMAIL]", value)
    value = LONG_NUMBER_RE.sub("[NUMBER]", value)
    return value


def _summarize_links(soup: BeautifulSoup, base_url: str | None) -> list[dict[str, str]]:
    links = []
    for anchor in soup.find_all("a", href=True)[:200]:
        links.append(
            {
                "text": redact_text(clean_text(anchor.get_text(" ")))[:200],
                "href": sanitize_url(anchor.get("href")),
                "id": redact_text(anchor.get("id", "")),
                "class": " ".join(anchor.get("class", [])) if isinstance(anchor.get("class"), list) else "",
                "role": anchor.get("role", ""),
            }
        )
    return links


def _summarize_buttons(soup: BeautifulSoup) -> list[dict[str, str]]:
    buttons = []
    for button in soup.find_all(["button", "input"])[:200]:
        if button.name == "input" and button.get("type") not in {"button", "submit", "reset"}:
            continue
        buttons.append(
            {
                "text": redact_text(clean_text(button.get_text(" ") or button.get("value", "")))[:200],
                "type": button.get("type", ""),
                "id": redact_text(button.get("id", "")),
                "class": " ".join(button.get("class", [])) if isinstance(button.get("class"), list) else "",
                "aria_label": redact_text(button.get("aria-label", "")),
            }
        )
    return buttons


def _summarize_forms(soup: BeautifulSoup, base_url: str | None) -> list[dict[str, Any]]:
    forms = []
    for form in soup.find_all("form")[:50]:
        fields = []
        for field_node in form.find_all(["input", "select", "textarea"])[:200]:
            fields.append(
                {
                    "tag": field_node.name,
                    "type": field_node.get("type", ""),
                    "name": redact_text(field_node.get("name", "")),
                    "id": redact_text(field_node.get("id", "")),
                    "aria_label": redact_text(field_node.get("aria-label", "")),
                }
            )
        forms.append(
            {
                "method": form.get("method", ""),
                "action": sanitize_url(form.get("action")),
                "id": redact_text(form.get("id", "")),
                "fields": fields,
            }
        )
    return forms


def _summarize_tables(soup: BeautifulSoup) -> list[dict[str, Any]]:
    tables = []
    for table in soup.find_all("table")[:50]:
        header_cells = table.find_all("th")
        if header_cells:
            headers = [redact_text(clean_text(cell.get_text(" "))) for cell in header_cells]
        else:
            first_row = table.find("tr")
            headers = (
                [redact_text(clean_text(cell.get_text(" "))) for cell in first_row.find_all("td")]
                if first_row
                else []
            )
        rows = table.find_all("tr")
        tables.append(
            {
                "id": redact_text(table.get("id", "")),
                "class": " ".join(table.get("class", [])) if isinstance(table.get("class"), list) else "",
                "headers": headers[:80],
                "row_count": max(len(rows) - 1, 0),
            }
        )
    return tables


def _summarize_iframes(soup: BeautifulSoup, base_url: str | None) -> list[dict[str, str]]:
    frames = []
    for frame in soup.find_all("iframe")[:100]:
        frames.append(
            {
                "title": redact_text(frame.get("title", "")),
                "name": redact_text(frame.get("name", "")),
                "id": redact_text(frame.get("id", "")),
                "src": sanitize_url(frame.get("src")),
            }
        )
    return frames


def _smow_endpoint_counts(responses: list[dict[str, Any]]) -> dict[str, int]:
    endpoints = {
        "lti_ajax_activities": "/lti/ajax/activities",
        "lti_ajax_students": "/lti/ajax/students",
        "results_figures": "/V2/results/figures",
        "results_reasons_all_services": "/V2/results/reasons/allActiveServices",
        "results_reasons_captures": "/V2/resultsReasonsCaptures",
        "monitoring_evidence": "/V2/monitoring/evidence",
        "review_status": "/V2/Review/Status",
        "download_requests": "/download",
    }
    counts = {key: 0 for key in endpoints}
    counts.update(
        {
            "front_results_requests": 0,
            "lti_smowl_requests": 0,
            "results_api_requests": 0,
        }
    )
    for response in responses:
        url = response.get("url", "")
        for key, needle in endpoints.items():
            if needle in url:
                counts[key] += 1
        if "front-results.smowltech.net" in url:
            counts["front_results_requests"] += 1
        if "lti-smowl-global.smowltech.net" in url:
            counts["lti_smowl_requests"] += 1
        if "results-api.smowltech.net" in url:
            counts["results_api_requests"] += 1
    return counts


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


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return any(keyword in normalized for keyword in TOKEN_KEYWORDS)


def _safe_call(callback):
    try:
        return callback()
    except Exception:
        return None
