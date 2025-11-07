#!/usr/bin/env python3
"""
NYT Article Search → PostgreSQL Ingest

Usage (PowerShell):
  python backend/tools/nyt_ingest.py --t0 Technology --min-word-count 1200 --begin 20240101 --end 20251022 --max-pages 10

Environment:
  NYT_API_KEY         Required for API calls
  PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD, PGSSLMODE (optional overrides)

Defaults (from prompt):
  host=thyself-db-1.ctagwuuqi8o3.us-east-2.rds.amazonaws.com port=5432 db=postgres user=deerofblue password=123456789 sslmode=require
"""
from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
import psycopg
from psycopg.rows import dict_row
import getpass
import yaml
import numpy as np
from math import isfinite

# Reuse project embedder when available; otherwise fall back to local SentenceTransformer
try:
    from backend.src.models import Embedder  # type: ignore
except Exception:
    Embedder = None  # Will fallback to local embedder below


NYT_ENDPOINT = "https://api.nytimes.com/svc/search/v2/articlesearch.json"

# T1 tagging config
ALPHA_PARENT_SMOOTH = float(os.getenv("T1_ALPHA", "0.08"))
TEMP_SOFTMAX = float(os.getenv("T1_TEMP", "0.08"))
TOPK_INSIDE = int(os.getenv("T1_TOPK_INSIDE", "3"))
TOPK_OUTSIDE = int(os.getenv("T1_TOPK_OUTSIDE", "2"))
MODEL_KEY = os.getenv("MODEL_KEY", "minilm")
TAXONOMY_PATH = os.getenv("TAXONOMY_PATH", os.path.join("backend", "taxonomies", "taxonomy.json"))

_T1_BANK: List[Dict[str, Any]] = []
_T1_EMBS: Optional[np.ndarray] = None
_EMBEDDER: Optional[Any] = None


def build_fq(section: str, min_word_count: int) -> str:
    """
    Build a filter query using the updated (2025-04-08) NYT Article Search fields.
    - Use section.name and desk for section/desk filters
    - Use Article.wordCount for word count threshold
    """
    section_escaped = section.replace('"', '\\"')
    return (
        f'(section.name:("{section_escaped}") OR desk:("{section_escaped}")) '
        f'AND Article.wordCount:[{min_word_count} TO *]'
    )


def fetch_page(api_key: str, page: int, fq: str, begin: Optional[str], end: Optional[str]) -> Dict[str, Any]:
    params = {
        "api-key": api_key,
        "fq": fq,
        "page": page,
        # NYT accepts YYYYMMDD
    }
    if begin:
        params["begin_date"] = begin
    if end:
        params["end_date"] = end

    backoff = 2.0
    while True:
        r = requests.get(NYT_ENDPOINT, params=params, timeout=20)
        if r.status_code == 429:
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 64.0)
            continue
        r.raise_for_status()
        return r.json()


def _pick_image_url(multimedia: Any) -> Optional[str]:
    if not multimedia:
        return None
    # Normalize: API may return an array of media entries, or a dict with 'default'/'thumbnail'
    if isinstance(multimedia, dict):
        items = [multimedia]
    elif isinstance(multimedia, list):
        items = multimedia
    else:
        return None
    # Choose the largest width image; handle multiple shapes, including string URLs
    best: Optional[str] = None
    best_w = -1
    for m in items:
        candidates: List[Tuple[Optional[str], int]] = []
        if isinstance(m, str):
            # Direct URL string (no width info)
            candidates.append((m, 0))
        elif isinstance(m, dict):
            # Old shape
            url = m.get("url")
            width = int(m.get("width") or 0)
            if url:
                candidates.append((url, width))
            # New 2025 shape with crops
            default_img = m.get("default") or {}
            if isinstance(default_img, dict):
                candidates.append((default_img.get("url"), int(default_img.get("width") or 0)))
            thumb_img = m.get("thumbnail") or m.get("thumbail") or {}
            if isinstance(thumb_img, dict):
                candidates.append((thumb_img.get("url"), int(thumb_img.get("width") or 0)))
        else:
            continue

        for cu, cw in candidates:
            if not cu:
                continue
            if cw > best_w:
                best_w = cw
                best = cu
            # If widths are equal or unknown, keep the first seen

    if not best:
        return None
    if best.startswith("http"):
        return best
    # relative path
    return f"https://www.nytimes.com/{best.lstrip('/')}"


def parse_doc(doc: Dict[str, Any], t0: str) -> Dict[str, Any]:
    headline = doc.get("headline") or {}
    byline = doc.get("byline") or {}
    persons = byline.get("person") or []
    author_list = []
    for p in persons:
        author_list.append({
            "firstname": p.get("firstname"),
            "middlename": p.get("middlename"),
            "lastname": p.get("lastname"),
            "role": p.get("role"),
            "organization": p.get("organization"),
        })
    keywords = doc.get("keywords") or []
    kw_clean = []
    for k in keywords:
        if isinstance(k, dict):
            kw_clean.append({"name": k.get("name"), "value": k.get("value")})
        else:
            kw_clean.append({"value": str(k)})

    # Map section/news_desk with fallbacks for updated field names
    section_name = doc.get("section_name")
    if not section_name:
        section = doc.get("section") or {}
        if isinstance(section, dict):
            section_name = section.get("name") or section.get("displayName")

    news_desk = doc.get("news_desk") or doc.get("desk")

    out = {
        "nyt_id": doc.get("_id") or doc.get("uri"),
        "web_url": doc.get("web_url"),
        "title": headline.get("main"),
        "abstract": doc.get("abstract"),
        "byline": byline.get("original"),
        "author_list": author_list,
        "pub_date": doc.get("pub_date"),
        "section_name": section_name,
        "news_desk": news_desk,
        "word_count": doc.get("word_count"),
        "image_url": _pick_image_url(doc.get("multimedia") or []),
        "source_tags": kw_clean,
        "t0_tag": t0,
        "raw": doc,
    }
    return out


def _connect() -> psycopg.Connection:
    host = os.getenv("PGHOST", "thyself-db-1.ctagwuuqi8o3.us-east-2.rds.amazonaws.com")
    port = int(os.getenv("PGPORT", "5432"))
    dbname = os.getenv("PGDATABASE", "postgres")
    user = os.getenv("PGUSER", "deerofblue")
    env_pwd = os.getenv("PGPASSWORD")
    password = env_pwd if env_pwd is not None else "123456789"
    sslmode = os.getenv("PGSSLMODE", "require")

    # If no PGPASSWORD is provided and we're still on the placeholder, prompt securely.
    if env_pwd is None or env_pwd == "":
        if password == "123456789":
            prompt_user = user or "postgres"
            try:
                password = getpass.getpass(f"Enter Postgres password for user '{prompt_user}': ")
            except Exception:
                # Fallback to empty; connection will fail with clear error
                password = ""

    dsn = f"host={host} port={port} dbname={dbname} user={user} password={password} sslmode={sslmode}"
    return psycopg.connect(dsn, row_factory=dict_row)


def _ensure_extensions_and_table(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        # Try to ensure UUID function exists
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
        except Exception:
            pass
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";")
        except Exception:
            pass
        conn.commit()

    create_sql_gen_random = """
    CREATE TABLE IF NOT EXISTS articles (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      nyt_id TEXT UNIQUE,
      web_url TEXT UNIQUE,
      title TEXT,
      abstract TEXT,
      byline TEXT,
      author_list JSONB,
      pub_date TIMESTAMPTZ,
      section_name TEXT,
      news_desk TEXT,
      word_count INTEGER,
      image_url TEXT,
      source_tags JSONB,
      t0_tag TEXT,
      raw JSONB,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """

    create_sql_uuid_v4 = create_sql_gen_random.replace("gen_random_uuid()", "uuid_generate_v4()")

    with conn.cursor() as cur:
        try:
            cur.execute(create_sql_gen_random)
        except Exception:
            cur.execute(create_sql_uuid_v4)
        conn.commit()

    # Labels table
    create_labels_sql = """
    CREATE TABLE IF NOT EXISTS article_labels (
      article_id UUID REFERENCES articles(id) ON DELETE CASCADE,
      level TEXT CHECK (level IN ('T0','T1')),
      tag TEXT NOT NULL,
      parent_t0 TEXT,
      score DOUBLE PRECISION NOT NULL,
      method TEXT NOT NULL,
      created_at TIMESTAMPTZ DEFAULT NOW(),
      PRIMARY KEY (article_id, level, tag)
    );
    """
    with conn.cursor() as cur:
        cur.execute(create_labels_sql)
        conn.commit()


def upsert_article(conn: psycopg.Connection, a: Dict[str, Any]) -> str:
    sql = """
    INSERT INTO articles (
      nyt_id, web_url, title, abstract, byline, author_list, pub_date, section_name,
      news_desk, word_count, image_url, source_tags, t0_tag, raw
    ) VALUES (
      %(nyt_id)s, %(web_url)s, %(title)s, %(abstract)s, %(byline)s, %(author_list)s::jsonb,
      %(pub_date)s, %(section_name)s, %(news_desk)s, %(word_count)s, %(image_url)s,
      %(source_tags)s::jsonb, %(t0_tag)s, %(raw)s::jsonb
    )
    ON CONFLICT (nyt_id) DO UPDATE SET
      web_url = EXCLUDED.web_url,
      title = EXCLUDED.title,
      abstract = EXCLUDED.abstract,
      byline = EXCLUDED.byline,
      author_list = EXCLUDED.author_list,
      pub_date = EXCLUDED.pub_date,
      section_name = EXCLUDED.section_name,
      news_desk = EXCLUDED.news_desk,
      word_count = EXCLUDED.word_count,
      image_url = EXCLUDED.image_url,
      source_tags = EXCLUDED.source_tags,
      t0_tag = EXCLUDED.t0_tag,
      raw = EXCLUDED.raw,
      updated_at = NOW()
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, a)
        row = cur.fetchone()
        return str(row["id"]) if isinstance(row, dict) else str(row[0])


def _load_taxonomy(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        if path.lower().endswith(".json"):
            data = json.load(f)
        else:
            data = yaml.safe_load(f)
    return data or {}


def _build_t1_bank(tax: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Return T1 bank and T0 maps by id/en (casefold)."""
    t1_bank: List[Dict[str, Any]] = []
    t0_by_key: Dict[str, Dict[str, Any]] = {}
    for t0 in (tax.get("t0") or []):
        t0_id = t0.get("id")
        t0_en = t0.get("en")
        t0_desc = t0.get("desc")
        if not t0_id or not t0_en:
            continue
        t0_by_key[t0_id.casefold()] = t0
        t0_by_key[t0_en.casefold()] = t0
        for t1 in (t0.get("t1") or []):
            t1_id = t1.get("id")
            t1_en = t1.get("en")
            t1_desc = t1.get("desc")
            if not t1_id or not t1_en:
                continue
            label_text = f"{t0_en} > {t1_en} — {t1_desc or ''}".strip()
            t1_bank.append({
                "t1_id": t1_id,
                "t1_en": t1_en,
                "parent_t0_id": t0_id,
                "parent_t0_en": t0_en,
                "definition": t1_desc or "",
                "label_text": label_text,
            })
    return t1_bank, t0_by_key


def _ensure_embedder() -> Any:
    global _EMBEDDER
    if _EMBEDDER is None:
        if Embedder is not None:
            _EMBEDDER = Embedder(name=MODEL_KEY)
        else:
            # Local fallback without importing backend module
            try:
                from sentence_transformers import SentenceTransformer
            except Exception as e:
                raise RuntimeError(
                    "SentenceTransformer not available. Run 'pip install -r requirements.txt' from repo root."
                ) from e

            EMBEDDER_MAP = {
                "minilm": "sentence-transformers/all-MiniLM-L6-v2",
                "mpnet": "sentence-transformers/all-mpnet-base-v2",
                "e5": "intfloat/e5-base-v2",
                "me5": "intfloat/multilingual-e5-base",
                "me5large": "intfloat/multilingual-e5-large",
                "multiminilm": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                "muse": "sentence-transformers/distiluse-base-multilingual-cased-v2",
            }
            model_name = EMBEDDER_MAP.get(MODEL_KEY, MODEL_KEY)

            class _LocalEmbedder:
                def __init__(self, model_name: str):
                    self.model = SentenceTransformer(model_name)

                def encode(self, texts: List[str]):
                    return self.model.encode(
                        texts,
                        normalize_embeddings=True,
                        convert_to_numpy=True,
                        show_progress_bar=False,
                    )

            _EMBEDDER = _LocalEmbedder(model_name)
    return _EMBEDDER


def _ensure_t1_embeddings() -> None:
    global _T1_BANK, _T1_EMBS
    if _T1_EMBS is not None and _T1_BANK:
        return
    tax = _load_taxonomy(TAXONOMY_PATH)
    _T1_BANK, _ = _build_t1_bank(tax)
    emb = _ensure_embedder()
    texts = [t["label_text"] for t in _T1_BANK]
    if not texts:
        _T1_EMBS = np.zeros((0, 384), dtype=np.float32)
        return
    embs = emb.encode(texts)
    _T1_EMBS = embs.astype(np.float32)


def _normalize(v: np.ndarray, eps: float = 1e-9) -> np.ndarray:
    n = float(np.linalg.norm(v))
    if not isfinite(n) or n <= eps:
        return v
    return v / n


def _article_vec(title: Optional[str], abstract: Optional[str]) -> np.ndarray:
    emb = _ensure_embedder()
    title = (title or "").strip()
    abstract = (abstract or "").strip()
    if title and abstract:
        tv, av = emb.encode([title, abstract])
        vec = 0.7 * tv + 0.3 * av
    elif title:
        vec = emb.encode([title])[0]
    elif abstract:
        vec = emb.encode([abstract])[0]
    else:
        vec = np.zeros((_T1_EMBS.shape[1] if _T1_EMBS is not None and _T1_EMBS.size else 384,), dtype=np.float32)
    return _normalize(vec.astype(np.float32))


def _softmax(x: np.ndarray, temp: float) -> np.ndarray:
    if x.size == 0:
        return x
    z = (x / max(temp, 1e-6)).astype(np.float64)
    z -= z.max()
    exp = np.exp(z)
    return (exp / (exp.sum() + 1e-12)).astype(np.float32)


def _compute_t1_scores_for_article(title: str, abstract: Optional[str], chosen_t0: Dict[str, Any]) -> List[Dict[str, Any]]:
    _ensure_t1_embeddings()
    if _T1_EMBS is None or _T1_EMBS.size == 0:
        return []
    v = _article_vec(title, abstract)
    # cos sims since both normalized
    sims = _T1_EMBS @ v
    parents = np.array([t["parent_t0_en"] for t in _T1_BANK])
    chosen_t0_en = chosen_t0.get("en")
    sims = sims + ALPHA_PARENT_SMOOTH * (parents == chosen_t0_en)
    s = _softmax(sims, TEMP_SOFTMAX)
    # min-max to [0,1]
    s_min, s_max = float(s.min(initial=0.0)), float(s.max(initial=1.0))
    s = (s - s_min) / (max(s_max - s_min, 1e-6))

    # Rank indices
    idx_sorted = np.argsort(-s)
    inside_idxs = [i for i in idx_sorted if _T1_BANK[i]["parent_t0_en"] == chosen_t0_en]
    outside_idxs = [i for i in idx_sorted if _T1_BANK[i]["parent_t0_en"] != chosen_t0_en]

    # Pick top inside
    top_in = inside_idxs[:TOPK_INSIDE]

    # Pick top outside with distinct parents if possible
    top_out: List[int] = []
    used_parents: set[str] = set()
    for i in outside_idxs:
        p = _T1_BANK[i]["parent_t0_en"]
        if p in used_parents and len(used_parents) < TOPK_OUTSIDE:
            # try to diversify first
            continue
        top_out.append(i)
        used_parents.add(p)
        if len(top_out) >= TOPK_OUTSIDE:
            break
    if len(top_out) < TOPK_OUTSIDE:
        # fill remaining from outside regardless of parent diversity
        for i in outside_idxs:
            if i not in top_out:
                top_out.append(i)
                if len(top_out) >= TOPK_OUTSIDE:
                    break

    picks = []
    for i in top_in + top_out:
        t = _T1_BANK[i]
        picks.append({
            "level": "T1",
            "tag": t["t1_id"],
            "parent_t0": t["parent_t0_id"],
            "score": float(s[i]),
            "method": f"embed+smooth:{MODEL_KEY}:alpha={ALPHA_PARENT_SMOOTH}:temp={TEMP_SOFTMAX}",
        })
    return picks


def _match_t0(t0_key: str, t0_map: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not t0_key:
        return None
    return t0_map.get(t0_key.casefold())


def upsert_article_labels(conn: psycopg.Connection, article_id: str, chosen_t0: Dict[str, Any], t1_picks: List[Dict[str, Any]]) -> None:
    # Upsert T0 anchor
    sql_t0 = """
    INSERT INTO article_labels (article_id, level, tag, parent_t0, score, method)
    VALUES (%(article_id)s, 'T0', %(tag)s, NULL, %(score)s, %(method)s)
    ON CONFLICT (article_id, level, tag) DO UPDATE SET
      score = EXCLUDED.score,
      method = EXCLUDED.method;
    """
    sql_t1 = """
    INSERT INTO article_labels (article_id, level, tag, parent_t0, score, method)
    VALUES (%(article_id)s, 'T1', %(tag)s, %(parent_t0)s, %(score)s, %(method)s)
    ON CONFLICT (article_id, level, tag) DO UPDATE SET
      score = EXCLUDED.score,
      method = EXCLUDED.method;
    """
    with conn.cursor() as cur:
        cur.execute(sql_t0, {
            "article_id": article_id,
            "tag": chosen_t0["id"],
            "score": 1.0,
            "method": f"anchor:{MODEL_KEY}",
        })
        for p in t1_picks:
            params = {"article_id": article_id, **p}
            cur.execute(sql_t1, params)


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest NYT Article Search results into PostgreSQL")
    ap.add_argument("--t0", required=True, help="T0 category name (used for section/news_desk filter)")
    ap.add_argument("--min-word-count", type=int, default=1200, help="Minimum word count filter (default 1200)")
    ap.add_argument("--begin", type=str, default=None, help="Begin date YYYYMMDD (optional)")
    ap.add_argument("--end", type=str, default=None, help="End date YYYYMMDD (optional)")
    ap.add_argument("--max-pages", type=int, default=1, help="Max pages to fetch (NYT returns ~10 results/page)")
    args = ap.parse_args()

    api_key = os.getenv("NYT_API_KEY")
    if not api_key:
        raise SystemExit("NYT_API_KEY not set in environment")

    fq = build_fq(args.t0, args.min_word_count)
    conn = _connect()
    _ensure_extensions_and_table(conn)

    # Prepare taxonomy + T1 bank + embedder
    tax = _load_taxonomy(TAXONOMY_PATH)
    t1_bank, t0_map = _build_t1_bank(tax)
    # Assign globals if not set
    global _T1_BANK, _T1_EMBS
    if not _T1_BANK:
        _T1_BANK = t1_bank
        _T1_EMBS = None  # force compute below
    _ensure_t1_embeddings()

    chosen_t0 = _match_t0(args.t0, t0_map)
    if chosen_t0 is None:
        # Try id-style for robustness
        # If still None, pick first matching by prefix
        for k, v in t0_map.items():
            if k.startswith(args.t0.casefold()):
                chosen_t0 = v
                break
    if chosen_t0 is None:
        raise SystemExit(f"Unknown T0 '{args.t0}'. Check taxonomy at {TAXONOMY_PATH}.")

    total_upserts = 0
    try:
        for page in range(args.max_pages):
            data = fetch_page(api_key, page, fq, args.begin, args.end)
            resp = (data or {}).get("response") or {}
            docs: List[Dict[str, Any]] = resp.get("docs") or []
            if not isinstance(docs, list):
                docs = []
            upserts_this_page = 0
            for d in docs:
                parsed = parse_doc(d, args.t0)
                # Skip if no id or URL
                if not parsed.get("nyt_id") or not parsed.get("web_url"):
                    continue
                # JSONB fields must be dumped to JSON text for psycopg binding
                parsed["author_list"] = json.dumps(parsed.get("author_list") or [])
                parsed["source_tags"] = json.dumps(parsed.get("source_tags") or [])
                parsed["raw"] = json.dumps(parsed.get("raw") or {})
                article_id = upsert_article(conn, parsed)
                # Compute and upsert labels
                t1_picks = _compute_t1_scores_for_article(parsed.get("title") or "", parsed.get("abstract"), chosen_t0)
                upsert_article_labels(conn, article_id, chosen_t0, t1_picks)
                upserts_this_page += 1
            conn.commit()
            total_upserts += upserts_this_page
            print(f"[page {page}] upserted {upserts_this_page} rows")

            # Rate limit: <= 10 req/min → sleep ~6s between pages
            if page < args.max_pages - 1:
                time.sleep(6)
    finally:
        conn.close()

    print("[done] ingestion completed")


if __name__ == "__main__":
    main()
