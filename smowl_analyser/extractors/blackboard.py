from __future__ import annotations

from typing import Any

from .html import absolute_url, clean_text, looks_interesting, soup_from_html
from ..models import LinkOption


COURSE_KEYWORDS = (
    "course",
    "courses",
    "curso",
    "turma",
    "disciplina",
    "blackboard",
)
GLOBAL_NAV_TITLES = {
    "atividade",
    "calendário",
    "calendario",
    "cursos",
    "ferramentas",
    "mensagens",
    "notas",
    "organizações",
    "organizacoes",
    "página da instituição",
    "pagina da instituicao",
    "privacidade",
    "termos",
}
SMOW_ACTIVITY_KEYWORDS = (
    "smow",
    "smowl",
    "proctor",
    "proctoring",
    "monitor",
    "relatório",
    "relatorio",
    "report",
)


def parse_courses_from_html(html: str, base_url: str | None = None) -> list[LinkOption]:
    soup = soup_from_html(html)
    strict_courses = _parse_course_anchors(soup, base_url, strict=True)
    if strict_courses:
        return strict_courses
    return _parse_course_anchors(soup, base_url, strict=False)


def _parse_course_anchors(soup: Any, base_url: str | None, *, strict: bool) -> list[LinkOption]:
    courses: list[LinkOption] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        title = clean_text(anchor.get_text(" "))
        href = anchor.get("href")
        url = absolute_url(href, base_url)
        if not title or not url or url in seen:
            continue
        if _is_global_navigation_title(title):
            continue
        if strict and not _is_blackboard_course_url(url):
            continue
        combined = f"{title} {href}"
        if not strict and not looks_interesting(combined, COURSE_KEYWORDS):
            continue
        seen.add(url)
        courses.append(
            LinkOption(
                id=f"course-{len(courses) + 1}",
                title=title,
                url=url,
                metadata={"source": "anchor"},
            )
        )
    return courses


def _is_blackboard_course_url(url: str) -> bool:
    return "/ultra/courses/" in url and "/outline" in url


def _is_global_navigation_title(title: str) -> bool:
    return title.strip().lower() in GLOBAL_NAV_TITLES


def parse_smow_activities_from_html(html: str, base_url: str | None = None) -> list[LinkOption]:
    soup = soup_from_html(html)
    activities: list[LinkOption] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        title = clean_text(anchor.get_text(" "))
        href = anchor.get("href")
        combined = f"{title} {href}"
        if not title or not looks_interesting(combined, SMOW_ACTIVITY_KEYWORDS):
            continue
        url = absolute_url(href, base_url)
        if not url:
            key = f"click-text:{title}"
            if key in seen:
                continue
            seen.add(key)
            activities.append(
                LinkOption(
                    id=f"activity-{len(activities) + 1}",
                    title=title,
                    url=base_url or "",
                    metadata={
                        "source": "empty-anchor",
                        "action": "click",
                        "text": title,
                    },
                )
            )
            continue
        if url in seen:
            continue
        seen.add(url)
        activities.append(
            LinkOption(
                id=f"activity-{len(activities) + 1}",
                title=title,
                url=url,
                metadata={"source": "anchor"},
            )
        )
    for node in soup.find_all(["button", "div", "span"], id=True):
        title = clean_text(node.get_text(" ") or node.get("aria-label"))
        combined = f"{title} {node.get('id', '')} {' '.join(node.get('class', [])) if isinstance(node.get('class'), list) else ''}"
        if not title or not looks_interesting(combined, SMOW_ACTIVITY_KEYWORDS):
            continue
        key = f"click:{node.get('id')}"
        if key in seen:
            continue
        seen.add(key)
        activities.append(
            LinkOption(
                id=f"activity-{len(activities) + 1}",
                title=title,
                url=base_url or "",
                metadata={
                    "source": "clickable",
                    "action": "click",
                    "selector": f"[id='{node.get('id')}']",
                },
            )
        )
    return activities


def discover_courses(page: Any) -> list[LinkOption]:
    live_courses = discover_course_cards(page)
    if live_courses:
        return live_courses
    return parse_courses_from_html(page.content(), page.url)


def discover_course_cards(page: Any) -> list[LinkOption]:
    try:
        cards = page.evaluate(
            """
            () => {
              const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
              const courseLike = (line) =>
                /\\b\\d{4,}\\.[A-Z0-9_.-]+/.test(line) ||
                /\\b\\d{4,}\\/\\d{2}\\b/.test(line);
              const titleLike = (line) =>
                line.length >= 8 &&
                !/^Abrir$/i.test(line) &&
                !/^Mais informações/i.test(line) &&
                !/^Favoritos$/i.test(line);
              const containers = Array.from(document.querySelectorAll('li, article, section, div'));
              const candidates = [];
              const absoluteUrl = (href) => {
                if (!href) {
                  return '';
                }
                try {
                  return new URL(href, window.location.origin).toString();
                } catch {
                  return '';
                }
              };
              const courseUrlFrom = (el) => {
                const hrefs = Array.from(el.querySelectorAll('a[href]'))
                  .map((node) => node.getAttribute('href') || '');
                const strict = hrefs.find((href) =>
                  href.includes('/ultra/courses/') && href.includes('/outline')
                );
                const loose = hrefs.find((href) => href.includes('/ultra/courses/'));
                return absoluteUrl(strict || loose || '');
              };
              for (const el of containers) {
                if (!el.getClientRects || el.getClientRects().length === 0) {
                  continue;
                }
                const rawText = el.innerText || '';
                const text = normalize(rawText);
                if (!text || !text.includes('Abrir') || !text.includes('Mais informações')) {
                  continue;
                }
                if (text.length > 900) {
                  continue;
                }
                const lines = rawText.split('\\n').map(normalize).filter(Boolean);
                const code = lines.find(courseLike) || '';
                const title = lines.find((line) => titleLike(line) && line !== code) || code;
                if (!title || !code) {
                  continue;
                }
                if (el.querySelector('[data-smowl-course-card]')) {
                  continue;
                }
                candidates.push({ el, code, title, textLength: text.length });
              }
              candidates.sort((a, b) => a.textLength - b.textLength);
              const seen = new Set();
              const result = [];
              for (const candidate of candidates) {
                const key = `${candidate.code}|${candidate.title}`;
                if (seen.has(key)) {
                  continue;
                }
                seen.add(key);
                const index = result.length + 1;
                candidate.el.setAttribute('data-smowl-course-card', String(index));
                const open = Array.from(candidate.el.querySelectorAll('a, button'))
                  .find((node) => normalize(node.innerText || node.getAttribute('aria-label')) === 'Abrir');
                if (open) {
                  open.setAttribute('data-smowl-course-open', String(index));
                }
                result.push({
                  index,
                  code: candidate.code,
                  title: candidate.title,
                  url: courseUrlFrom(candidate.el),
                });
              }
              return result;
            }
            """
        )
    except Exception:
        return []
    courses: list[LinkOption] = []
    for item in cards or []:
        index = item.get("index")
        title = clean_text(item.get("title") or "")
        code = clean_text(item.get("code") or "")
        if not index or not title:
            continue
        display_title = f"{code} | {title}" if code and code != title else title
        course_url = clean_text(item.get("url") or "")
        metadata = {
            "source": "course-card-link" if course_url else "course-card",
            "course_code": code,
            "course_title": title,
        }
        if course_url:
            metadata["course_url"] = course_url
        else:
            metadata.update(
                {
                    "action": "click-course-card",
                    "selector": f"[data-smowl-course-open='{index}']",
                    "fallback_selector": f"[data-smowl-course-card='{index}']",
                }
            )
        courses.append(
            LinkOption(
                id=f"course-card-{index}",
                title=display_title,
                url=course_url or page.url,
                metadata=metadata,
            )
        )
    return courses


def discover_smow_activities(page: Any) -> list[LinkOption]:
    return parse_smow_activities_from_html(page.content(), page.url)
