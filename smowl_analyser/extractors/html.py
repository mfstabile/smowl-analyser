from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup


def soup_from_html(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def absolute_url(href: str | None, base_url: str | None = None) -> str | None:
    if not href:
        return None
    if base_url:
        return urljoin(base_url, href)
    return href


def clean_text(value: str | None) -> str:
    return " ".join((value or "").split())


def looks_interesting(value: str, keywords: tuple[str, ...]) -> bool:
    normalized = value.lower()
    return any(keyword in normalized for keyword in keywords)
