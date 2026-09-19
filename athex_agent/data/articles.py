"""Fetch article bodies for the most relevant items (bounded, robots-checked, best effort)."""

from __future__ import annotations

import logging
import re

import requests

from athex_agent.data.news import RobotsCache

log = logging.getLogger(__name__)


def html_to_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        tag.decompose()
    node = soup.find("article") or soup.find("main") or soup.body or soup
    paras = [p.get_text(" ", strip=True) for p in node.find_all(["p", "h1", "h2", "li"])]
    text = "\n".join(p for p in paras if len(p) > 30)
    return re.sub(r"\n{2,}", "\n", text).strip()


def fetch_article(
    url: str, ua: str, robots: RobotsCache, timeout: float = 15.0, max_chars: int = 6000
) -> str | None:
    try:
        if not robots.allowed(url):
            return None
        r = requests.get(
            url, headers={"User-Agent": ua, "Accept-Language": "el,en"}, timeout=timeout
        )
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            return None
        text = html_to_text(r.text)
        return text[:max_chars] if text else None
    except requests.RequestException as exc:
        log.info("article fetch failed %s: %s", url, exc)
        return None
