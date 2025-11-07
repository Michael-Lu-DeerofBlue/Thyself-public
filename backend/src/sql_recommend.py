from __future__ import annotations

import os
import json
import math
import datetime as dt
from typing import Any, Dict, List, Optional, Sequence, Tuple

import psycopg
from psycopg.rows import dict_row
import logging


# Simple cached connection helper (blocking; fine for dev)
_CONN: Optional[psycopg.Connection] = None
_LOGGER = logging.getLogger(__name__)
if os.getenv("RECO_DEBUG", "0").lower() in {"1", "true", "yes"}:
    _LOGGER.setLevel(logging.DEBUG)


def _connect() -> psycopg.Connection:
    global _CONN
    if _CONN and not _CONN.closed:
        return _CONN
    host = os.getenv("PGHOST")
    port = int(os.getenv("PGPORT", "5432"))
    dbname = os.getenv("PGDATABASE")
    user = os.getenv("PGUSER")
    password = os.getenv("PGPASSWORD", "")
    sslmode = os.getenv("PGSSLMODE", "require")
    if not host or not dbname or not user:
        raise RuntimeError("Postgres env vars missing (PGHOST, PGDATABASE, PGUSER)")
    dsn = f"host={host} port={port} dbname={dbname} user={user} sslmode={sslmode}"
    if password:
        dsn = dsn + f" password={password}"
    _LOGGER.debug("sql_recommend: connecting to PG host=%s db=%s user=%s sslmode=%s", host, dbname, user, sslmode)
    _CONN = psycopg.connect(dsn, row_factory=dict_row)
    return _CONN


def db_ready_info() -> Dict[str, Any]:
    """Lightweight readiness info for Postgres connectivity used by /readyz."""
    host = os.getenv("PGHOST")
    port = int(os.getenv("PGPORT", "5432"))
    dbname = os.getenv("PGDATABASE")
    user = os.getenv("PGUSER")
    password = os.getenv("PGPASSWORD", "")
    sslmode = os.getenv("PGSSLMODE", "require")
    configured = bool(host and dbname and user)
    info: Dict[str, Any] = {
        "ok": False,
        "configured": configured,
        "host": host or "",
        "port": port,
        "dbname": dbname or "",
        "user": user or "",
        "sslmode": sslmode,
        "connected": False,
    }
    if not configured:
        info["error"] = "Missing PGHOST/PGDATABASE/PGUSER"
        return info
    try:
        dsn = f"host={host} port={port} dbname={dbname} user={user} sslmode={sslmode}"
        if password:
            dsn = dsn + f" password={password}"
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                _ = cur.fetchone()
        info["ok"] = True
        info["connected"] = True
    except Exception as e:
        info["error"] = str(e)
    return info


def _load_profile_top_t1(profile_path: str, k: int = 3) -> List[str]:
    try:
        with open(profile_path, "r", encoding="utf-8") as f:
            prof = json.load(f)
    except Exception as e:
        raise RuntimeError(f"Failed to load profile at {profile_path}: {e}")
    ranked = prof.get("t1_ranked") or []
    # ranked is [["Parent > Child", count], ...]
    tags: List[str] = []
    for item in ranked:
        if not isinstance(item, list) or len(item) < 1:
            continue
        name = item[0]
        if isinstance(name, str) and " > " in name:
            child = name.split(" > ", 1)[1]
        elif isinstance(name, str):
            child = name
        else:
            continue
        if child not in tags:
            tags.append(child)
        if len(tags) >= k:
            break
    _LOGGER.debug("sql_recommend: profile top T1s → %s", tags)
    return tags


def _recency_score(pub_date: Optional[dt.datetime], decay_days: float = 90.0) -> float:
    if not pub_date:
        return 0.0
    if pub_date.tzinfo is None:
        pub_date = pub_date.replace(tzinfo=dt.timezone.utc)
    now = dt.datetime.now(dt.timezone.utc)
    days = max((now - pub_date).days, 0)
    return math.exp(-days / max(decay_days, 1e-6))


def _fetch_candidates(tags_lower: Sequence[str], limit: int = 200) -> List[Dict[str, Any]]:
    if not tags_lower:
        return []
    sql = """
    SELECT a.id, a.title, a.web_url, a.byline, a.section_name, a.news_desk, a.pub_date,
           COALESCE(a.image_url, '') AS image_url,
           json_agg(json_build_object('tag', l.tag, 'score', l.score, 'parent_t0', l.parent_t0)) AS labels
    FROM articles a
    JOIN article_labels l ON l.article_id = a.id AND l.level = 'T1'
    WHERE lower(l.tag) = ANY(%(tags)s)
    GROUP BY a.id
    ORDER BY a.pub_date DESC NULLS LAST
    LIMIT %(limit)s
    """
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute(sql, {"tags": list(tags_lower), "limit": limit})
        rows = cur.fetchall() or []
    _LOGGER.debug("sql_recommend: fetched %d candidates for tags=%s", len(rows), list(tags_lower))
    return rows


def recommend_from_db(
    tags: Sequence[str],
    *,
    use_profile: bool = False,
    profile_path: str = "backend/data/profile.json",
    decay_days: float = 90.0,
    w_tag: float = 0.7,
    w_recency: float = 0.3,
) -> Optional[Dict[str, Any]]:
    """
    Recommend one article from Postgres using simple scoring:
    score = w_tag * sum(label_score for matched tags) + w_recency * exp(-age_days / decay_days)

    Strategy:
      - If use_profile=True: try top-3 T1s from profile.json first; if no candidates, fall back to provided tags.
      - If use_profile=False: use provided tags only.

    Returns dict: { title, source, url, date } or None
    """
    # Build list of tag attempts in order
    attempts: List[List[str]] = []
    base = [t for t in tags if t and str(t).strip()]
    if use_profile:
        try:
            prof = _load_profile_top_t1(profile_path, k=3)
        except Exception as e:
            _LOGGER.warning("sql_recommend: failed to load profile: %s", e)
            prof = []
        if prof:
            attempts.append(prof[:3])
    if base:
        attempts.append(base[:3])

    if not attempts:
        _LOGGER.info("sql_recommend: no tags provided/derived; cannot recommend")
        return None

    last_error: Optional[str] = None
    for idx, ts in enumerate(attempts):
        tags_lower = [str(t).strip().lower() for t in ts if t and str(t).strip()]
        _LOGGER.info("sql_recommend: attempt %d using tags=%s (use_profile=%s)", idx + 1, tags_lower, use_profile)
        try:
            candidates = _fetch_candidates(tags_lower, limit=200)
        except Exception as e:
            last_error = str(e)
            _LOGGER.warning("sql_recommend: fetch failed on attempt %d: %s", idx + 1, e)
            continue

        _LOGGER.debug("sql_recommend: fetched %d candidates for tags=%s", len(candidates), tags_lower)
        if not candidates:
            continue

        best = None
        best_score = -1.0
        debug_rows: List[Tuple[float, str, float, float]] = []  # (score, title, tag_score, recency)
        for row in candidates:
            labels = row.get("labels") or []
            tag_score = 0.0
            for lab in labels:
                tag = str(lab.get("tag", "")).lower()
                if tag in tags_lower:
                    try:
                        tag_score += float(lab.get("score", 0.0))
                    except Exception:
                        tag_score += 0.0
            # recency
            pub_date = row.get("pub_date")
            if isinstance(pub_date, str):
                try:
                    pub_date_dt = dt.datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                except Exception:
                    pub_date_dt = None
            else:
                pub_date_dt = pub_date
            rec = _recency_score(pub_date_dt, decay_days=decay_days)
            score = w_tag * tag_score + w_recency * rec
            debug_rows.append((score, str(row.get("title") or ""), tag_score, rec))
            if score > best_score:
                best_score = score
                best = row

        if best is None:
            continue

        if _LOGGER.isEnabledFor(logging.DEBUG):
            topk = sorted(debug_rows, key=lambda x: -x[0])[:5]
            for s, title, ts_val, rec in topk:
                _LOGGER.debug("sql_recommend: cand title='%s' score=%.4f tag=%.4f rec=%.4f", title, s, ts_val, rec)

        # Map to API schema
        src = (best.get("byline") or "").replace("By ", "").strip()
        url = best.get("web_url") or ""
        title = best.get("title") or "Untitled"
        pub_date = best.get("pub_date")
        try:
            if isinstance(pub_date, str):
                d = dt.datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
            elif isinstance(pub_date, dt.datetime):
                d = pub_date
            else:
                d = dt.datetime.now(dt.timezone.utc)
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            date_str = d.strftime("%Y/%m/%d")
        except Exception:
            date_str = dt.datetime.now(dt.timezone.utc).strftime("%Y/%m/%d")

        return {
            "title": title,
            "source": src or "New York Times",
            "url": url,
            "date": date_str,
            "image_url": (best.get("image_url") or ""),
        }

    if last_error:
        _LOGGER.info("sql_recommend: no pick after %d attempts (last_error=%s)", len(attempts), last_error)
    else:
        _LOGGER.info("sql_recommend: 0 candidates across %d attempts", len(attempts))
    return None
