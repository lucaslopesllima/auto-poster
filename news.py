"""
news.py
=======

Busca notícias sobre um tópico em duas fontes (com fallback):

  1. NewsAPI (https://newsapi.org) — mais campos estruturados, requer chave.
  2. Google News RSS — gratuito, sem chave; usado se NewsAPI falhar.

Retorno: lista de dicts com chaves:
  title, url, description, source, published_at
"""

from __future__ import annotations

import os
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
NEWSAPI_URL = "https://newsapi.org/v2/everything"
GOOGLE_RSS = "https://news.google.com/rss/search"
IMAGE_DIR = BASE_DIR / "data" / "uploads"

# namespaces RSS comuns que carregam imagens
RSS_MEDIA_NS = "{http://search.yahoo.com/mrss/}"


@dataclass
class Article:
    title: str
    url: str
    description: str
    source: str
    published_at: str
    image_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def fetch_news(
    topic: str,
    max_results: int = 5,
    lang: str = "pt",
    country: str = "BR",
) -> list[dict]:
    """
    Busca notícias. Tenta NewsAPI primeiro; em qualquer falha cai para
    Google News RSS. Devolve lista (possivelmente vazia) de Article.to_dict().
    """
    load_dotenv(BASE_DIR / ".env", override=True)
    topic = topic.strip()
    if not topic:
        return []

    api_key = os.getenv("NEWSAPI_KEY", "").strip()
    if api_key:
        try:
            arts = _newsapi(topic, max_results, lang, api_key)
            if arts:
                return [a.to_dict() for a in arts]
        except Exception as exc:  # noqa: BLE001
            print(f"[news] NewsAPI falhou ({exc}); usando RSS.", flush=True)

    try:
        return [a.to_dict() for a in _google_rss(topic, max_results, lang, country)]
    except Exception as exc:  # noqa: BLE001
        print(f"[news] Google RSS falhou: {exc}", flush=True)
        return []


# ---------------------------------------------------------------------------
# NewsAPI
# ---------------------------------------------------------------------------

def _newsapi(topic: str, n: int, lang: str, api_key: str) -> list[Article]:
    resp = requests.get(
        NEWSAPI_URL,
        params={
            "q": topic,
            "language": lang,
            "sortBy": "publishedAt",
            "pageSize": n,
        },
        headers={"X-Api-Key": api_key},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(data.get("message") or "resposta inesperada")
    out: list[Article] = []
    for a in data.get("articles", [])[:n]:
        out.append(Article(
            title=(a.get("title") or "").strip(),
            url=(a.get("url") or "").strip(),
            description=(a.get("description") or "").strip(),
            source=(a.get("source") or {}).get("name") or "",
            published_at=(a.get("publishedAt") or "").strip(),
            image_url=(a.get("urlToImage") or "").strip(),
        ))
    return out


# ---------------------------------------------------------------------------
# Google News RSS (sem dependência: feedparser não é necessário)
# ---------------------------------------------------------------------------

def _google_rss(topic: str, n: int, lang: str, country: str) -> list[Article]:
    params = {
        "q": topic,
        "hl": f"{lang}-{country}",
        "gl": country,
        "ceid": f"{country}:{lang}",
    }
    url = f"{GOOGLE_RSS}?{urlencode(params)}"
    resp = requests.get(url, timeout=15, headers={"User-Agent": "linkedinAuto/1.0"})
    resp.raise_for_status()
    raw = resp.text
    root = ET.fromstring(raw)
    out: list[Article] = []
    for item in root.findall("./channel/item")[:n]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc_raw = (item.findtext("description") or "").strip()
        desc = _strip_html(desc_raw)
        source_el = item.find("source")
        source = source_el.text.strip() if (source_el is not None and source_el.text) else ""
        pub = (item.findtext("pubDate") or "").strip()
        image_url = _extract_image_from_item(item, desc_raw)
        out.append(Article(
            title=title, url=link, description=desc,
            source=source, published_at=pub, image_url=image_url,
        ))
    return out


def _extract_image_from_item(item: ET.Element, description_html: str) -> str:
    """Tenta extrair URL de imagem do item RSS.

    Ordem: media:content → media:thumbnail → enclosure → <img> em description.
    """
    for tag in (f"{RSS_MEDIA_NS}content", f"{RSS_MEDIA_NS}thumbnail"):
        el = item.find(tag)
        if el is not None and el.get("url"):
            return el.get("url", "").strip()
    enc = item.find("enclosure")
    if enc is not None:
        url = enc.get("url") or ""
        ctype = enc.get("type", "")
        if url and (ctype.startswith("image/") or _looks_like_image(url)):
            return url.strip()
    # último recurso: primeiro <img src="..."> dentro do HTML da description
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', description_html, re.I)
    if m:
        return m.group(1).strip()
    return ""


def _looks_like_image(url: str) -> bool:
    return bool(re.search(r"\.(jpe?g|png|gif|webp)(\?|$)", url, re.I))


def _strip_html(s: str) -> str:
    """Remoção rasa de tags HTML para descrição do Google News."""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# ---------------------------------------------------------------------------
# Download de imagem
# ---------------------------------------------------------------------------

_CTYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def download_image(url: str, dest_dir: Path = IMAGE_DIR) -> Optional[str]:
    """
    Baixa uma imagem para `dest_dir`. Retorna caminho absoluto ou None.
    """
    if not url:
        return None
    try:
        resp = requests.get(
            url, timeout=20,
            headers={"User-Agent": "linkedinAuto/1.0", "Accept": "image/*"},
            allow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[news] download falhou {url}: {exc}", flush=True)
        return None
    if resp.status_code != 200 or not resp.content:
        print(f"[news] download {url} status={resp.status_code}", flush=True)
        return None
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    ext = _CTYPE_EXT.get(ctype)
    if ext is None:
        # tenta deduzir pela URL
        m = re.search(r"\.(jpe?g|png|gif|webp)(?:\?|$)", url, re.I)
        ext = f".{m.group(1).lower()}" if m else ".jpg"
        if ext == ".jpeg":
            ext = ".jpg"
    # WebP é aceito: linkedin_client converte para PNG antes do upload.
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"news_{uuid.uuid4().hex}{ext}"
    path.write_bytes(resp.content)
    return str(path)


def pick_image(
    articles: list[dict],
    dest_dir: Path = IMAGE_DIR,
    scrape_fallback: bool = True,
) -> Optional[str]:
    """
    Itera as notícias e tenta baixar a 1ª imagem válida.

    Para cada artigo:
      1. usa `image_url` do feed (NewsAPI principal);
      2. se vazio e `scrape_fallback`, abre a URL do artigo e tenta
         `og:image` / `twitter:image` / 1º <img> da página.
    """
    for a in articles:
        url = (a.get("image_url") or "").strip()
        if not url and scrape_fallback and a.get("url"):
            url = scrape_article_image(a["url"])
            if url:
                a["image_url"] = url  # cache no dict pra próximas iterações
        if not url:
            continue
        path = download_image(url, dest_dir)
        if path:
            return path
    return None


# ---------------------------------------------------------------------------
# Scraping de imagem da página do artigo
# ---------------------------------------------------------------------------

_META_PATTERNS = [
    # og:image (em ambas as ordens de atributos)
    re.compile(
        r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)',
        re.I,
    ),
    re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::secure_url)?["\']',
        re.I,
    ),
    # twitter:image
    re.compile(
        r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)',
        re.I,
    ),
    re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image(?::src)?["\']',
        re.I,
    ),
]
_IMG_PATTERN = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)


def scrape_article_image(url: str, timeout: int = 15) -> str:
    """
    Abre a URL e devolve a 1ª imagem encontrada (og:image / twitter:image /
    <img>). String vazia em falha.

    Para URLs do Google News (`news.google.com/.../articles/...`), primeiro
    decodifica para a URL real do artigo (a página intermediária do Google
    expõe apenas a thumb genérica do GN, não a imagem real).
    """
    if not url:
        return ""

    real = resolve_google_news_url(url) or url
    try:
        resp = requests.get(
            real, timeout=timeout, allow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            },
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[news] scrape falhou {real[:60]}: {exc}", flush=True)
        return ""
    if resp.status_code != 200:
        print(f"[news] scrape {real[:60]} status={resp.status_code}", flush=True)
        return ""

    html = resp.text
    for pat in _META_PATTERNS:
        m = pat.search(html)
        if m:
            return _absolutize(m.group(1).strip(), resp.url)
    # fallback: 1ª <img> NÃO-tracker
    for m in _IMG_PATTERN.finditer(html):
        candidate = _absolutize(m.group(1).strip(), resp.url)
        if candidate and not _is_tracker(candidate):
            return candidate
    return ""


_TRACKER_HINTS = (
    "facebook.com/tr", "google-analytics", "googletagmanager",
    "doubleclick", "/pixel", "/beacon", "spacer.gif", "1x1.",
)


def _is_tracker(url: str) -> bool:
    u = url.lower()
    return any(h in u for h in _TRACKER_HINTS)


# ---------------------------------------------------------------------------
# Resolver URLs do Google News para a URL real do artigo
# ---------------------------------------------------------------------------

_GARTURLRES = re.compile(r'garturlres\\",\\"(https?://[^"\\]+)\\"')
_BATCHEXEC_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"


def resolve_google_news_url(url: str, timeout: int = 15) -> str:
    """
    Resolve URL `news.google.com/.../articles/<id>` para a URL canônica do
    artigo via API interna batchexecute (RPC `Fbv4je`).

    Retorna a URL original (input) se não for um link do Google News, ou
    string vazia em falha.
    """
    if "news.google.com" not in url or "/articles/" not in url:
        return url
    aid_m = re.search(r"/articles/([^?]+)", url)
    if not aid_m:
        return ""
    article_id = aid_m.group(1)

    try:
        page = requests.get(
            url, timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0"},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[news] resolve fetch falhou: {exc}", flush=True)
        return ""
    sig_m = re.search(r'data-n-a-sg="([^"]+)"', page.text)
    ts_m = re.search(r'data-n-a-ts="([^"]+)"', page.text)
    if not sig_m or not ts_m:
        return ""

    import json as _json
    inner = _json.dumps([
        "garturlreq",
        [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1],
         "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
        article_id, ts_m.group(1), sig_m.group(1),
    ])
    payload = _json.dumps([[["Fbv4je", inner, None, "generic"]]])
    try:
        resp = requests.post(
            _BATCHEXEC_URL,
            data={"f.req": payload},
            headers={
                "User-Agent": "Mozilla/5.0",
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            },
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[news] batchexecute falhou: {exc}", flush=True)
        return ""
    if resp.status_code != 200:
        return ""
    body = resp.text
    m = _GARTURLRES.search(body)
    return m.group(1) if m else ""


def _absolutize(img_url: str, base_url: str) -> str:
    """Resolve URL relativa contra base."""
    if not img_url:
        return ""
    if img_url.startswith("data:"):
        return ""
    if img_url.startswith("//"):
        return "https:" + img_url
    if img_url.startswith("http://") or img_url.startswith("https://"):
        return img_url
    if img_url.startswith("/"):
        from urllib.parse import urlparse
        p = urlparse(base_url)
        return f"{p.scheme}://{p.netloc}{img_url}"
    # relativa pura
    base = base_url.rsplit("/", 1)[0]
    return f"{base}/{img_url}"


if __name__ == "__main__":
    import sys
    import json
    topic = " ".join(sys.argv[1:]) or "inteligência artificial"
    arts = fetch_news(topic)
    print(json.dumps(arts, indent=2, ensure_ascii=False))
