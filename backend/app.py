from __future__ import annotations

import os
import time
from typing import List, Optional, Dict, Any
import json
import logging

import numpy as np
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict, field_validator

from backend.src.models import Embedder
from backend.src.analysis import (
    load_taxonomy_hier,
    get_cached_label_embeddings,
)
from backend.src.recommend import recommend_piece, SEARCH_PROVIDER as R_PROVIDER, SERPAPI_KEY as R_KEY
from backend.src.recommend import ALLOWLIST as R_ALLOWLIST
from backend.src.sql_recommend import recommend_from_db as sql_recommend, db_ready_info


# ---------- Config ----------
APP_NAME = os.getenv("APP_NAME", "thyself-backend")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5050"))

# Fixed to MiniLM per requirement
MODEL_KEY = os.getenv("MODEL_KEY", "minilm").lower()
if MODEL_KEY != "minilm":
    # Enforce MiniLM only for now
    MODEL_KEY = "minilm"

TAXONOMY_PATH = os.getenv("TAXONOMY_PATH", "backend/taxonomies/t0.yaml")
CACHE_DIR = os.getenv("CACHE_DIR", "backend/out")
PROFILE_PATH = os.getenv("PROFILE_PATH", "backend/data/profile.json")

# Scoring defaults
ALPHA_DEFAULT = float(os.getenv("ALPHA", "0.30"))
TOPK_PARENT_DEFAULT = int(os.getenv("TOPK_PARENT", "8"))


# ---------- App ----------
app = FastAPI(title=APP_NAME)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logger = logging.getLogger(__name__)
# Ensure application logs are visible even when running under Uvicorn's default config
APP_LOG_LEVEL = os.getenv("APP_LOG_LEVEL", "INFO").upper()
try:
    level_value = getattr(logging, APP_LOG_LEVEL, logging.INFO)
except Exception:
    level_value = logging.INFO
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_h)
logger.setLevel(level_value)
logger.propagate = True


# ---------- Models ----------
class AnalyzeOptions(BaseModel):
    alpha: Optional[float] = Field(default=ALPHA_DEFAULT, ge=0.0, le=1.0)
    topk_parent: Optional[int] = Field(default=TOPK_PARENT_DEFAULT, ge=1)


class AnalyzeRequest(BaseModel):
    user_id: Optional[str] = ""
    titles: List[str]
    options: Optional[AnalyzeOptions] = None

    @field_validator("titles")
    @classmethod
    def validate_titles(cls, v: List[str]):
        if not v or len(v) == 0:
            raise ValueError("titles must be a non-empty array")
        if len(v) > 500:
            raise ValueError("titles length must be ≤ 500")
        # Clamp title length to 300 chars
        return [t[:300] for t in v]


class AnalyzeResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    user_id: str
    total_titles: int
    tags_histogram: Dict[str, Dict[str, Any]]
    flat_subfield_histogram: Dict[str, int]
    t0_ranked: List[List[Any]]
    t1_ranked: List[List[Any]]


class RecommendRequest(BaseModel):
    user_id: Optional[str] = ""
    tags: List[str]
    limit: int = 1
    need_backups: bool = False
    use_profile: bool = False  # when true, read top-3 T1 from backend/data/profile.json

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v: List[str]):
        if len(v) != 3:
            raise ValueError("tags must contain exactly three T1 IDs (strings)")
        return v


class Piece(BaseModel):
    user_id: Optional[str] = ""
    title: str
    source: str
    url: str
    date: str
    image_url: Optional[str] = ""


# ---------- Startup: load models and label caches ----------
_EMBEDDER: Embedder | None = None
_PARENTS: list[dict] | None = None
_CHILDREN: list[dict] | None = None
_P_EMB: np.ndarray | None = None
_C_EMB: np.ndarray | None = None


def _ensure_ready():
    global _EMBEDDER, _PARENTS, _CHILDREN, _P_EMB, _C_EMB
    if _EMBEDDER is None:
        _EMBEDDER = Embedder(MODEL_KEY)
    if _PARENTS is None or _P_EMB is None:
        # Build caches on first use
        parents, children = load_taxonomy_hier(TAXONOMY_PATH)
        p, c, p_emb, c_emb = get_cached_label_embeddings(
            taxonomy_path=TAXONOMY_PATH,
            model_key=MODEL_KEY,
            embedder=_EMBEDDER,
            cache_dir=CACHE_DIR,
            parents=parents,
            children=children,
        )
        _PARENTS, _CHILDREN, _P_EMB, _C_EMB = p, c, p_emb, c_emb


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/readyz")
def readyz():
    try:
        _ensure_ready()
        sql = {}
        try:
            sql = db_ready_info()
        except Exception as e:
            sql = {"ok": False, "error": str(e)}
        return {
            "ok": True,
            "model": MODEL_KEY,
            "parents": len(_PARENTS or []),
            "children": len(_CHILDREN or []),
            "search": {
                "provider": R_PROVIDER,
                "serpapi_key": bool(R_KEY),
                "allowlist_size": len(R_ALLOWLIST),
            },
            "sql": sql,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/")
def root():
    # Redirect base URL to interactive docs for convenience
    return RedirectResponse(url="/docs")


@app.get("/favicon.ico")
def favicon():
    # Quiet 404s from browsers asking for a favicon
    return Response(status_code=204)


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest):
    _ensure_ready()
    assert _EMBEDDER is not None and _PARENTS is not None and _P_EMB is not None

    titles = req.titles
    alpha = req.options.alpha if req.options and req.options.alpha is not None else ALPHA_DEFAULT
    topk_parent = req.options.topk_parent if req.options else TOPK_PARENT_DEFAULT

    # Prepare title inputs (no E5 query/passage prefixes since we use MiniLM)
    V = _EMBEDDER.encode(titles)
    P = _P_EMB
    p_scores = V @ P.T  # [N, P]

    # If we have T1 children, compute joint scoring; else fallback to T0 only
    if _CHILDREN is not None and len(_CHILDREN) > 0 and _C_EMB is not None and _C_EMB.shape[0] > 0:
        C = _C_EMB
        child_pidx = np.array([c["p_index"] for c in _CHILDREN], dtype=np.int64)

        # Optionally mask to top-k parents
        mask = None
        if topk_parent is not None and 0 < topk_parent < len(_PARENTS):
            topP = np.argpartition(-p_scores, kth=topk_parent - 1, axis=1)[:, :topk_parent]
            mask = np.zeros((V.shape[0], len(_CHILDREN)), dtype=bool)
            for n in range(V.shape[0]):
                allowed = set(topP[n].tolist())
                mask[n] = np.isin(child_pidx, list(allowed))

        c_scores = V @ C.T
        combined = (1.0 - alpha) * c_scores + alpha * p_scores[:, child_pidx]

        # Pick best child per title
        picks: List[Dict[str, Optional[str]]] = []
        for i in range(V.shape[0]):
            if mask is not None:
                valid_idx = np.where(mask[i])[0]
                if valid_idx.size == 0:
                    j = int(np.argmax(p_scores[i]))
                    picks.append({"t0": _PARENTS[j]["en"], "t1": None})
                    continue
                local_scores = combined[i, valid_idx]
                best_local = valid_idx[int(np.argmax(local_scores))]
            else:
                best_local = int(np.argmax(combined[i]))
            ch = _CHILDREN[best_local]
            picks.append({"t0": ch["p_en"], "t1": ch["en"]})
    else:
        # Parent-only fallback
        picks = []
        sims = p_scores
        for i in range(V.shape[0]):
            j = int(np.argmax(sims[i]))
            picks.append({"t0": _PARENTS[j]["en"], "t1": None})

    # Aggregate histograms
    t0_counts: Dict[str, int] = {}
    t1_counts: Dict[str, int] = {}
    for p in picks:
        t0 = p["t0"]
        t1 = p.get("t1")
        t0_counts[t0] = t0_counts.get(t0, 0) + 1
        if t1:
            key = f"{t0} > {t1}"
            t1_counts[key] = t1_counts.get(key, 0) + 1

    # Build nested tags_histogram: { Parent: { total, children: { Child: count } } }
    tags_histogram: Dict[str, Dict[str, Any]] = {}
    for parent_name, total in t0_counts.items():
        tags_histogram[parent_name] = {"total": total, "children": {}}
    for key, c in t1_counts.items():
        parent_name, child_name = key.split(" > ", 1)
        tags_histogram[parent_name]["children"][child_name] = c

    # Ranked lists
    t0_ranked = sorted([[k, v] for k, v in t0_counts.items()], key=lambda x: (-x[1], x[0]))
    t1_ranked = sorted([[k, v] for k, v in t1_counts.items()], key=lambda x: (-x[1], x[0]))

    resp = AnalyzeResponse(
        user_id=req.user_id or "",
        total_titles=len(titles),
        tags_histogram=tags_histogram,
        flat_subfield_histogram=t1_counts,
        t0_ranked=t0_ranked,
        t1_ranked=t1_ranked,
    )
    return resp


@app.get("/profile/histogram")
def profile_histogram():
    """Return histogram data from the local profile.json file.
    Fields: user_id, total_titles, tags_histogram, flat_subfield_histogram, t0_ranked, t1_ranked.
    """
    try:
        with open(PROFILE_PATH, "r", encoding="utf-8") as f:
            prof = json.load(f)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Profile not found at {PROFILE_PATH}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load profile: {e}")

    return {
        "user_id": prof.get("user_id", ""),
        "total_titles": prof.get("total_titles", 0),
        "tags_histogram": prof.get("tags_histogram", {}),
        "flat_subfield_histogram": prof.get("flat_subfield_histogram", {}),
        "t0_ranked": prof.get("t0_ranked", []),
        "t1_ranked": prof.get("t1_ranked", []),
    }


@app.post("/recommend")
async def recommend(req: RecommendRequest):
    # Try SQL-backed recommender first when enabled; fallback to search or curated
    _ensure_ready()
    today = time.strftime("%Y/%m/%d")

    # Enable SQL recommender when explicitly requested via env, or implicitly when DB is configured and reachable
    use_sql_env = os.getenv("USE_SQL_RECO")
    use_sql_flag = (use_sql_env or "0").lower() in {"1", "true", "yes"}
    if use_sql_env is None:
        # No explicit env set; try auto-enable if DB is ready
        try:
            info = db_ready_info()
            use_sql = bool(info.get("ok") and info.get("connected"))
        except Exception:
            use_sql = False
    else:
        use_sql = use_sql_flag
    item: Optional[dict] = None

    logger.info("/recommend: use_sql=%s use_profile=%s tags=%s", use_sql, req.use_profile, req.tags)

    # Log the three tags that will be attempted first
    try:
        if use_sql and req.use_profile:
            # Profile-first attempt (top-3 from profile), then fallback to request tags
            prof_tags: List[str] = []
            try:
                with open(PROFILE_PATH, "r", encoding="utf-8") as f:
                    prof = json.load(f)
                ranked = prof.get("t1_ranked") or []
                for item_tag in ranked:
                    if isinstance(item_tag, list) and item_tag:
                        name = item_tag[0]
                        if isinstance(name, str) and " > " in name:
                            child = name.split(" > ", 1)[1]
                        elif isinstance(name, str):
                            child = name
                        else:
                            continue
                        if child not in prof_tags:
                            prof_tags.append(child)
                        if len(prof_tags) >= 3:
                            break
            except Exception as e:
                logger.info("/recommend: failed to read profile for tags: %s", e)
            logger.info(
                "/recommend: picked tags — first attempt (profile)=%s; fallback (request)=%s",
                prof_tags, req.tags[:3]
            )
        else:
            # Non-profile first attempt uses request tags
            logger.info(
                "/recommend: picked tags — first attempt=%s",
                req.tags[:3]
            )
    except Exception as e:
        logger.warning("/recommend: tag logging failed: %s", e)

    if use_sql:
        try:
            item = sql_recommend(req.tags, use_profile=req.use_profile)
            if item is not None:
                logger.info("/recommend: used SQL recommender")
        except Exception as e:
            logger.warning("/recommend: SQL recommender failed: %s", e)

    if item is None:
        # Fall back to existing search-based pipeline
        item = await recommend_piece(req.tags, _EMBEDDER)  # type: ignore[arg-type]
        if item is not None:
            logger.info("/recommend: used search pipeline (provider=%s)", R_PROVIDER)

    if item is None:
        logger.info("/recommend: using curated fallback (provider=%s, has_key=%s)", R_PROVIDER, bool(R_KEY))
        curated = [
            {
                "title": "No Result",
                "source": "No Result",
                "url": "No Result",
            }
        ]
        idx = abs(hash("|".join(req.tags))) % len(curated)
        item = curated[idx]

    # Build Piece explicitly to avoid duplicate kwargs and ignore unknown keys
    title = item.get("title") if isinstance(item, dict) else None
    source = item.get("source") if isinstance(item, dict) else None
    url = item.get("url") if isinstance(item, dict) else None
    date_str = item.get("date") if isinstance(item, dict) else None
    image_url = item.get("image_url") if isinstance(item, dict) else None
    return Piece(
        user_id=req.user_id or "",
        title=title or "Untitled",
        source=source or "",
        url=url or "",
        date=date_str or today,
        image_url=image_url or "",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.app:app", host=HOST, port=PORT, reload=True)
