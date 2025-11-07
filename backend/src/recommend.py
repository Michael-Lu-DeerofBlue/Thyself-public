from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from .models import Embedder


# ---------------- Config ----------------
SEARCH_PROVIDER = os.getenv("SEARCH_PROVIDER", "serpapi").lower()  # 'serpapi' only for now
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "8.0"))
ALLOWLIST = set((
    # a small seed allowlist; extend via env DOMAIN_ALLOWLIST comma-separated
    "www.theatlantic.com",
    "www.noahpinion.blog",
    "hbr.org",
    "aeon.co",
    "www.nytimes.com",
    "www.newyorker.com",
    "www.bbc.com",
    "www.ft.com",
    "www.economist.com",
))
_extra_allow = os.getenv("DOMAIN_ALLOWLIST", "").strip()
if _extra_allow:
    ALLOWLIST |= set(x.strip() for x in _extra_allow.split(",") if x.strip())

DOMAIN_QUALITY = {
    "www.theatlantic.com": 0.9,
    "www.noahpinion.blog": 0.7,
    "hbr.org": 0.85,
    "aeon.co": 0.75,
    "www.nytimes.com": 0.9,
    "www.newyorker.com": 0.9,
    "www.bbc.com": 0.85,
    "www.ft.com": 0.9,
    "www.economist.com": 0.9,
}


# ---------------- Data ----------------
@dataclass
class Candidate:
    url: str
    domain: str
    title: str = ""
    source: str = ""
    author: str = ""
    description: str = ""
    published: Optional[datetime] = None
    paywall: Optional[bool] = None
    image_url: str = ""
    score: float = 0.0


# ---------------- Utils ----------------
def normalize_url(u: str) -> str:
    try:
        p = urlparse(u)
        # Drop tracking params
        qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if not k.lower().startswith(("utm_", "fbclid"))]
        new_q = urlencode(qs)
        p2 = p._replace(query=new_q, fragment="")
        return urlunparse(p2)
    except Exception:
        return u


def expand_queries(tags: List[str]) -> List[str]:
    # Simple rule-based expansion; replace later with LLM if desired.
    base = [
        f"longform deep dive {t}" for t in tags
    ]
    mix = [
        f"{tags[0]} {tags[1]} analysis essay",
        f"{tags[1]} {tags[2]} investigative piece",
        f"best article {tags[0]} {tags[2]}",
    ]
    return list(dict.fromkeys(base + mix))[:6]


async def search_serpapi(client: httpx.AsyncClient, query: str, topk: int = 5) -> List[str]:
    if not SERPAPI_KEY:
        return []
    # Use SerpAPI Google search endpoint
    url = "https://serpapi.com/search.json"
    params = {"q": query, "num": topk, "api_key": SERPAPI_KEY}
    r = await client.get(url, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    links: List[str] = []
    for item in data.get("organic_results", [])[: topk]:
        link = item.get("link")
        if link:
            links.append(link)
    return links


async def resolve_metadata(client: httpx.AsyncClient, url: str) -> Optional[Candidate]:
    norm = normalize_url(url)
    domain = urlparse(norm).netloc
    if ALLOWLIST and domain not in ALLOWLIST:
        return None
    try:
        # HEAD then GET fallback
        r = await client.head(norm, follow_redirects=True, timeout=REQUEST_TIMEOUT)
        if r.status_code >= 400:
            r = await client.get(norm, follow_redirects=True, timeout=REQUEST_TIMEOUT)
        else:
            # still fetch body for metadata
            r = await client.get(norm, follow_redirects=True, timeout=REQUEST_TIMEOUT)
        if r.status_code >= 400 or not r.text:
            return None
    except Exception:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    def meta(name: str, prop: bool = False) -> Optional[str]:
        if prop:
            el = soup.find("meta", attrs={"property": name})
        else:
            el = soup.find("meta", attrs={"name": name})
        return el.get("content") if el and el.has_attr("content") else None

    title = meta("og:title", prop=True) or meta("twitter:title") or (soup.title.string.strip() if soup.title and soup.title.string else "")
    site = meta("og:site_name", prop=True) or meta("twitter:site") or domain
    author = meta("author") or meta("article:author", prop=True) or ""
    desc = meta("og:description", prop=True) or meta("description") or ""
    img = meta("og:image", prop=True) or meta("twitter:image") or ""
    pub_str = meta("article:published_time", prop=True) or meta("date")
    published = None
    if pub_str:
        try:
            # Try multiple formats quickly
            published = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
        except Exception:
            published = None
    paywall = None
    # simple heuristic for paywalls by domain
    if domain in {"www.ft.com", "www.economist.com", "www.nytimes.com"}:
        paywall = True

    return Candidate(url=norm, domain=domain, title=title or site, source=site, author=author or "", description=desc or "", published=published, paywall=paywall, image_url=img or "")


def tag_vector(embedder: Embedder, tags: List[str]) -> Any:
    txt = ", ".join(tags)
    return embedder.encode([txt])[0]


def clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def score_candidate(emb: Embedder, tag_vec, cand: Candidate, tags: List[str]) -> float:
    # tag overlap: count of tag tokens in title lowercased / len(tags)
    title_l = (cand.title or "").lower()
    overlap = sum(1 for t in tags if t.lower() in title_l) / max(1, len(tags))
    # embed sim between tags vector and candidate title+desc
    txt = f"{cand.title}. {cand.description}".strip()
    vec = emb.encode([txt])[0]
    embed_sim = float((tag_vec @ vec))  # both normalized by Embedder
    embed_sim = (embed_sim + 1.0) / 2.0  # normalize cosine [-1,1] → [0,1]
    # domain quality
    dq = DOMAIN_QUALITY.get(cand.domain, 0.6)
    # recency (if available): within 3 years → linearly decay
    rec = 0.5
    if cand.published:
        days = (datetime.now(timezone.utc).date() - cand.published.date()).days
        if days <= 0:
            rec = 1.0
        elif days >= 365 * 3:
            rec = 0.2
        else:
            rec = 1.0 - (days / (365 * 3)) * 0.8  # 1.0 → 0.2 over 3 years
        rec = clip01(rec)
    # weighted sum (design doc): 0.5 overlap + 0.2 embed + 0.2 domain + 0.1 recency
    score = 0.5 * overlap + 0.2 * embed_sim + 0.2 * dq + 0.1 * rec
    return float(score)


# ---------------- Cache ----------------
_MEM_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_DIR = os.getenv("CACHE_DIR", "backend/out")
os.makedirs(CACHE_DIR, exist_ok=True)
DISK_CACHE = os.path.join(CACHE_DIR, "reco_cache.json")


def cache_key(tags: List[str]) -> str:
    key = ",".join(sorted(tags)) + ":" + time.strftime("%Y-%m-%d")
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def cache_get(key: str) -> Optional[Dict[str, Any]]:
    if key in _MEM_CACHE:
        return _MEM_CACHE[key]
    try:
        if os.path.exists(DISK_CACHE):
            with open(DISK_CACHE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if key in data:
                _MEM_CACHE[key] = data[key]
                return data[key]
    except Exception:
        pass
    return None


def cache_put(key: str, val: Dict[str, Any]) -> None:
    _MEM_CACHE[key] = val
    try:
        data = {}
        if os.path.exists(DISK_CACHE):
            with open(DISK_CACHE, "r", encoding="utf-8") as f:
                data = json.load(f)
        data[key] = val
        with open(DISK_CACHE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------- Orchestrator ----------------
async def recommend_piece(tags: List[str], embedder: Embedder) -> Optional[Dict[str, Any]]:
    key = cache_key(tags)
    cached = cache_get(key)
    if cached:
        return cached

    if not SERPAPI_KEY or SEARCH_PROVIDER != "serpapi":
        return None

    # Expand queries and search
    queries = expand_queries(tags)
    async with httpx.AsyncClient(headers={"User-Agent": "thyself/1.0"}) as client:
        links: List[str] = []
        for q in queries:
            try:
                lst = await search_serpapi(client, q, topk=5)
                links.extend(lst)
            except Exception:
                continue
        # Dedupe links
        seen = set()
        uniq_links = []
        for u in links:
            nu = normalize_url(u)
            if nu not in seen:
                uniq_links.append(nu)
                seen.add(nu)

        # Resolve metadata and score
        tag_vec = tag_vector(embedder, tags)
        cands: List[Candidate] = []
        for u in uniq_links:
            cand = await resolve_metadata(client, u)
            if not cand:
                continue
            cand.score = score_candidate(embedder, tag_vec, cand, tags)
            cands.append(cand)

    if not cands:
        return None

    # Pick best
    cands.sort(key=lambda x: (-x.score, x.domain, x.title))
    best = cands[0]
    item = {
        "title": best.title,
        "source": best.source or best.domain,
        "url": best.url,
        "image_url": best.image_url or "",
    }
    cache_put(key, item)
    return item
