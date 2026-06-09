from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .storage import DEFAULT_SECRETS_DIR, DEFAULT_STATE_PATH


@dataclass(frozen=True)
class BrowserConfig:
    state_path: Path = DEFAULT_STATE_PATH
    headless: bool = False
    slow_mo_ms: int = 0
    timeout_ms: int = 30_000
    accept_downloads: bool = True


class BrowserSession:
    def __init__(self, config: BrowserConfig | None = None) -> None:
        self.config = config or BrowserConfig()

    def require_state(self) -> None:
        if not self.config.state_path.exists():
            raise RuntimeError(
                "No saved browser session found. Run `smowl-analyser login --url ...` first."
            )

    def run_with_page(
        self,
        callback: Callable[[Any], Any],
        *,
        use_saved_state: bool = True,
    ) -> Any:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run `pip install -e .` and "
                "`playwright install chromium`."
            ) from exc

        if use_saved_state:
            self.require_state()

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=self.config.headless,
                slow_mo=self.config.slow_mo_ms,
            )
            context_kwargs: dict[str, Any] = {}
            context_kwargs["accept_downloads"] = self.config.accept_downloads
            if use_saved_state and self.config.state_path.exists():
                context_kwargs["storage_state"] = self.config.state_path.as_posix()
            context = browser.new_context(**context_kwargs)
            context.set_default_timeout(self.config.timeout_ms)
            page = context.new_page()
            try:
                return callback(page)
            finally:
                context.close()
                browser.close()

    def run_with_context_page(
        self,
        callback: Callable[[Any, Any], Any],
        *,
        use_saved_state: bool = True,
    ) -> Any:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run `pip install -e .` and "
                "`playwright install chromium`."
            ) from exc

        if use_saved_state:
            self.require_state()

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=self.config.headless,
                slow_mo=self.config.slow_mo_ms,
            )
            context_kwargs: dict[str, Any] = {}
            context_kwargs["accept_downloads"] = self.config.accept_downloads
            if use_saved_state and self.config.state_path.exists():
                context_kwargs["storage_state"] = self.config.state_path.as_posix()
            context = browser.new_context(**context_kwargs)
            context.set_default_timeout(self.config.timeout_ms)
            page = context.new_page()
            try:
                return callback(page, context)
            finally:
                context.close()
                browser.close()

    def save_manual_login(self, url: str) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run `pip install -e .` and "
                "`playwright install chromium`."
            ) from exc

        self.config.state_path.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False, slow_mo=self.config.slow_mo_ms)
            context = browser.new_context(accept_downloads=self.config.accept_downloads)
            context.set_default_timeout(self.config.timeout_ms)
            page = context.new_page()
            page.goto(url)
            input("Log in manually in the browser, then press Enter here to save the session...")
            context.storage_state(path=self.config.state_path.as_posix())
            context.close()
            browser.close()


class JsonResponseCapture:
    def __init__(self) -> None:
        self.responses: list[dict[str, Any]] = []

    def attach(self, page: Any) -> None:
        page.on("response", self._capture_response)

    def attach_context(self, context: Any) -> None:
        context.on("response", self._capture_response)

    def urls(self) -> list[str]:
        return [response["url"] for response in self.responses]

    def containing(self, value: str) -> list[dict[str, Any]]:
        return [response for response in self.responses if value in response["url"]]

    def _capture_response(self, response: Any) -> None:
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            return
        request = response.request
        try:
            payload = response.json()
        except Exception:
            return
        self.responses.append(
            {
                "url": response.url,
                "status": response.status,
                "method": getattr(request, "method", ""),
                "request_headers": getattr(request, "headers", {}),
                "request_post_data": getattr(request, "post_data", None),
                "payload": payload,
            }
        )


def ensure_local_state_dirs() -> None:
    DEFAULT_SECRETS_DIR.mkdir(parents=True, exist_ok=True)
