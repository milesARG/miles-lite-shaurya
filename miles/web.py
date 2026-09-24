"""Live web knowledge: search results, reading pages, news headlines."""
import html
import re
import urllib.parse
import xml.etree.ElementTree as ET

import requests

import config
from .util import log

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0 Safari/537.36", "Accept-Language": "en-IN,en;q=0.9"}


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def search(query: str, n: int = 6):
    """-> list of (title, url, snippet)."""
    results = []
    try:
        r = requests.post("https://html.duckduckgo.com/html/", data={"q": query, "kl": config.SEARCH_REGION},
                          headers=UA, timeout=8)
        for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)'
                             r'(?:<a[^>]+class="result__snippet"[^>]*>(.*?)</a>|</div>\s*</div>)', r.text, re.S):
            href = html.unescape(m.group(1))
            u = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0]
            if "duckduckgo.com/y.js" in u:          # ads
                continue
            results.append((_text(m.group(2)), u, _text(m.group(4) or "")))
            if len(results) >= n:
                break
    except Exception as e:
        log.warning("ddg search failed: %s", e)
    if not results:                                 # fallback: Bing
        try:
            r = requests.get("https://www.bing.com/search", params={"q": query}, headers=UA, timeout=8)
            for m in re.finditer(r'<li class="b_algo".*?<h2[^>]*><a[^>]+href="([^"]+)"[^>]*>(.*?)</a></h2>(.*?)</li>',
                                 r.text, re.S):
                snip = re.search(r"<p[^>]*>(.*?)</p>", m.group(3), re.S)
                results.append((_text(m.group(2)), html.unescape(m.group(1)), _text(snip.group(1)) if snip else ""))
                if len(results) >= n:
                    break
        except Exception as e:
            log.warning("bing search failed: %s", e)
    return results


def read_page(url: str, max_chars: int = 6000) -> str:
    r = requests.get(url, headers=UA, timeout=12)
    r.raise_for_status()
    page = r.text
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    page = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header|form|aside)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?i)<(br|/p|/div|/h[1-6]|/li|/tr)[^>]*>", "\n", page)
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    lines = [" ".join(l.split()) for l in text.splitlines()]
    lines = [l for l in lines if len(l) > 30 or (l and l[-1] in ".:?!")]
    body = "\n".join(lines)
    head = _text(title.group(1)) + "\n" if title else ""
    return (head + body)[:max_chars]


def news(topic: str = "", n: int = 5):
    region = config.NEWS_REGION
    lang = f"hl=en-{region}&gl={region}&ceid={region}:en"
    url = (f"https://news.google.com/rss/search?q={urllib.parse.quote(topic)}&{lang}" if topic
           else f"https://news.google.com/rss?{lang}")
    r = requests.get(url, headers=UA, timeout=8)
    root = ET.fromstring(r.content)
    out = []
    for item in root.iter("item"):
        title = item.findtext("title") or ""
        source = item.findtext("source") or ""
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        out.append((title.strip(), source.strip()))
        if len(out) >= n:
            break
    return out
