"""HTML -> Page: main text (trafilatura), title/description, headings, dates, links."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

import trafilatura
from pydantic import BaseModel, Field
from selectolax.parser import HTMLParser


class Page(BaseModel):
    url: str
    title: str = ""
    description: str = ""
    text: str = ""
    headings: list[str] = Field(default_factory=list)
    times: list[str] = Field(default_factory=list)
    links: list[tuple[str, str]] = Field(default_factory=list)  # (absolute url, anchor text)


def parse_page(url: str, html: str, max_chars: int = 6000) -> Page:
    tree = HTMLParser(html)
    title_node = tree.css_first("title")
    title = title_node.text(strip=True) if title_node else ""
    desc = ""
    for sel in ('meta[name="description"]', 'meta[property="og:description"]'):
        node = tree.css_first(sel)
        if node and node.attributes.get("content"):
            desc = (node.attributes.get("content") or "").strip()
            break
    headings = [h.text(strip=True) for h in tree.css("h1, h2, h3") if h.text(strip=True)][:40]
    times = [(t.attributes.get("datetime") or t.text(strip=True)) or "" for t in tree.css("time")]
    links: list[tuple[str, str]] = []
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        if href.startswith(("mailto:", "javascript:", "#")):
            continue
        links.append((urljoin(url, href), a.text(strip=True)))
    text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    if not text:
        body = tree.body
        text = body.text(separator="\n", strip=True) if body else ""
    text = re.sub(r"\n{3,}", "\n\n", text)[:max_chars]
    return Page(
        url=url,
        title=title,
        description=desc,
        text=text,
        headings=headings,
        times=[t for t in times if t],
        links=links,
    )


_SECTION_HINTS: dict[str, tuple[str, ...]] = {
    "about": ("about", "company", "team", "who-we-are", "our-story"),
    "blog": ("blog", "news", "press", "changelog", "updates", "announcements"),
    "careers": ("careers", "jobs", "join", "hiring", "work-with-us"),
}


def discover_sections(page: Page, max_per_section: int = 1) -> dict[str, list[str]]:
    """Find same-site about / blog / careers URLs linked from a homepage."""
    host = urlsplit(page.url).netloc.lower().removeprefix("www.")
    found: dict[str, list[str]] = {k: [] for k in _SECTION_HINTS}
    for href, anchor in page.links:
        parts = urlsplit(href)
        if parts.netloc.lower().removeprefix("www.") != host:
            continue
        path = parts.path.lower().strip("/")
        if not path or path.count("/") > 1:
            continue
        hay = f"{path} {anchor.lower()}"
        for section, hints in _SECTION_HINTS.items():
            if any(h in hay for h in hints) and href not in found[section]:
                if len(found[section]) < max_per_section:
                    found[section].append(href.split("#")[0])
    return found


def blog_post_links(page: Page, limit: int = 2) -> list[str]:
    """Links from a blog index that look like individual posts (one level deeper)."""
    base = urlsplit(page.url)
    base_path = base.path.rstrip("/")
    out: list[str] = []
    for href, _ in page.links:
        parts = urlsplit(href)
        if parts.netloc != base.netloc or not parts.path.startswith(base_path + "/"):
            continue
        if parts.path.rstrip("/") == base_path or href in out:
            continue
        out.append(href.split("#")[0])
        if len(out) >= limit:
            break
    return out
