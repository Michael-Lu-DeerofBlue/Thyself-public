This is the start of Thyself
# Thyself: Extension ↔ Web App ↔ Backend (Privacy‑First)

Integrated, local‑first workflow:
1) A minimal Chrome (MV3) extension collects only titles from YouTube feed/shorts and keeps everything in the browser.
2) A web app orchestrates end‑to‑end: pulls titles from the extension, calls the backend for analysis/recommendation, and writes results back to the extension.
3) A zero‑shot hierarchical taxonomy classifier (T0 → T1) powers the analysis; an experimental recommender returns one daily “piece”.

# Example query: latest 10 labeled articles with scores
SELECT a.id, a.title, a.t0_tag, a.pub_date, l.level, l.tag, l.parent_t0, l.score, l.method
FROM article_labels l
JOIN articles a ON a.id = l.article_id
WHERE l.article_id IN (
	SELECT l2.article_id
	FROM article_labels l2
	ORDER BY l2.created_at DESC
	LIMIT 10
)
ORDER BY a.pub_date DESC, l.level, l.score DESC;


## Architecture at a Glance

- Browser Extension
	- Scope: runs on youtube.com/m.youtube.com; reads document.title; no navigation/dwell tracking.
	- Storage (IndexedDB):
		- titles_batch – rolling collection of captured titles for analysis
		- profile_histogram – latest T0/T1 histogram written by the web app
		- pieces_archive – append‑only archive of recommended pieces
	- Popup UI: inspect recent titles, export, clear, show current profile/piece.

- Web App (Next.js)
	- Orchestrates: GET_TITLES → POST /analyze → SET_PROFILE → sample 3 T1 → POST /recommend → APPEND_PIECE → render Analysis/Piece/Archive.
	- Fallback: if a local profile file exists, uses it to render without calling the backend.

- Backend (Python)
	- POST /analyze — zero‑shot hierarchical classification over your taxonomy.
	- POST /recommend — prototype path using query expansion + search + verification, returns one high‑quality link.
	- SQL‑backed recommend (optional): uses Postgres `articles` + `article_labels`. With `use_profile=true`, tries profile top‑3 T1s first, then falls back to provided tags.
	- GET /profile/histogram — returns local profile histograms.
	- Helper: NYT → Postgres ingestor that upserts articles and auto‑labels T0/T1; see `backend/tools/nyt_ingest.py` and `backend/README`.

See `DESIGN_DOCUMENT.md` for the detailed contract and algorithms.

## Privacy & Scope
* Extension scope restricted to YouTube only.
* Titles are the only analysis input. Extra fields captured by the extractor (e.g., href/videoId) are not transmitted off device.
* No browsing behavior, clicks, or timing tracked.

## Repository Layout (current)
```
backend/
	taxonomies/t0.yaml         # Editable hierarchical taxonomy (parents with optional children)
	taxonomies/taxonomy.json   # Canonical JSON (generated) used for caching fingerprint
	tools/nyt_ingest.py        # NYT Article Search → Postgres (UPSERT) + T1 tagging into article_labels
	src/models.py              # Embedder abstraction + short keys → HF models
	src/labels.py              # Label utilities
	data/titles.json           # Example exported titles placeholder
	data/titles_tagged*.json   # Classification outputs (examples)
	data/profile.json          # Profile output used by the web fallback
tools/
	apply_titles.py            # Zero-shot classifier (flat or joint) + diagnostics
	convert_taxonomy.py        # YAML → JSON converter
	build_user_profile.py      # titles_tagged.json → profile.json aggregator
extension/                   # MV3 extension (YouTube only)
web/                         # Next.js app (App Router)
Makefile                     # Helper targets (apply-titles, convert-taxonomy, clean-cache)
requirements.txt             # Python dependencies
README.md                    # This document
```

## Extension: Event Schema (current persisted shape)
```jsonc
{
	"ts": 1720000000000,          // epoch ms
	"type": "feed_video" | "shorts_video",
	"title": "Video Title Text",
	"videoId": "abcDEF123",
	"href": "https://www.youtube.com/watch?v=abcDEF123",
	"page": "/",
	"platform": "youtube"
}
```
The analysis uses the title only; other fields are retained locally and never sent.

### Deduped Titles Batch (used for analysis)

```
titles_batch: [
	{ id: "hash(normalized title)", title: "…", ts: 1720000000000, platform: "youtube" }
]
```

## Web ↔ Extension Messaging (bridge)

| Type | Direction | Payload / Response |
| --- | --- | --- |
| `GET_TITLES` | Web → Ext | none → `{ ok, data: [{id,title,ts,platform}, …] }` |
| `ACK_TITLES` | Web → Ext | `{ ids: ["hash1", "hash2", …] }` → `{ ok: true }` (purges only these ids) |
| `CLEAR_TITLES` | Web → Ext | none → `{ ok: true }` (clears the entire batch; convenience) |
| `SET_PROFILE` | Web → Ext | `{ profile: profile_histogram }` → `{ ok: true }` |
| `GET_PROFILE` | Web → Ext | none → `{ ok, data: profile|null }` |
| `APPEND_PIECE` | Web → Ext | `{ piece: {date,title,source,url} }` → `{ ok: true }` |
| `GET_ARCHIVE` | Web → Ext | none → `{ ok, data: pieces_archive }` |
| `GET_STATUS` | Web → Ext | none → `{ ok, data: { last_webapp_sync_at } }` |

Refer to `DESIGN_DOCUMENT.md` for payload details and retention policy.

## Backend APIs

- POST `/analyze`: titles → histograms + ranked lists (`t0_ranked`, `t1_ranked`).
- POST `/recommend`: exactly three T1 IDs → one best piece (plus optional backups). When `USE_SQL_RECO=1`, it first tries the SQL path; if `use_profile=true`, it uses profile top‑3 first then request tags; otherwise it uses request tags only.
- GET `/profile/histogram`: returns the histogram fields from your local profile file.

## Zero‑Shot Hierarchical Classification
Scoring (joint mode):
```
combined = (1 - alpha) * child_similarity + alpha * parent_similarity
```

### Run via Local Tools (offline path)
This path does not require the backend; it generates the profile file that the web app can read directly.
1) Convert taxonomy (if you edited YAML):
```powershell
python tools/convert_taxonomy.py --input backend/taxonomies/t0.yaml --output backend/taxonomies/taxonomy.json
```
2) Classify (MiniLM default):
```powershell
python tools/apply_titles.py --mode zero-shot-joint `
	--taxonomy backend/taxonomies/t0.yaml `
	--input backend/data/titles.json `
	--output backend/data/titles_tagged.json `
	--alpha 0.30 --topk-parent 8 --embedder minilm
```
3) (Optional) Detailed ranking:
```powershell
python tools/apply_titles.py --mode zero-shot-joint --detail --topk-children 12 `
	--input backend/data/titles.json --output backend/data/titles_tagged_detail.json
```
4) Build profile for web fallback:
```powershell
python tools/build_user_profile.py --input backend/data/titles_tagged.json --json-output backend/data/profile.json
```
The web app will auto‑detect `backend/data/profile.json` and render it when present.

### Embedders & Automatic E5 Prompting
Short keys (from `backend/src/models.py`): `minilm`, `mpnet`, `e5`, `me5`, `me5large`, `multiminilm`, `muse`.
If the model name contains `e5`, label texts use `passage:` and title inputs use `query:` automatically (E5 asymmetric retrieval style).

### Caching
Per model + prompt style: label embedding caches are content‑addressed by taxonomy fingerprint + model key + prompt style. Invalidate by editing taxonomy YAML and reconverting.

## End‑to‑End Online Flow (recommended)
1) Load the extension (Developer Mode → Load unpacked → `extension/`).
2) Browse YouTube; titles accumulate locally.
3) Start the web app; it will:
	 - Read titles from the extension, call your backend `/analyze` and `/recommend` (see web README for env var),
	 - Write the latest profile and piece back into the extension,
	 - Render Analysis, Piece, and Archive.

## Setup & Run

### Python (tools and/or backend)
```powershell
pip install -r requirements.txt
```

### Extension
1. Open `chrome://extensions` (enable Developer Mode).
2. Load unpacked → select `extension/`.

### Web App (Next.js)
See `web/README.md` for details. Optionally set:
```
NEXT_PUBLIC_ANALYZER_URL=https://api.thyself.example
```
When set, the web app will call `${NEXT_PUBLIC_ANALYZER_URL}/analyze` and `/recommend`.

### NYT ingestion → RDS (details)
See `backend/README` for full instructions and tuning options. Defaults:
- Uses `backend/taxonomies/taxonomy.json` for T1 banks (for ingestion/labeling)
- MiniLM embeddings; stores one T0 label (score 1.0) and up to five T1 labels (3 inside chosen T0, 2 outside)
- Idempotent UPSERT on `nyt_id` so you can safely re-run for overlapping dates

SQL recommender env (optional):
- `USE_SQL_RECO=1` — enable SQL‑backed recommend
- `RECO_DEBUG=1` — print candidate scoring details
- `APP_LOG_LEVEL=INFO` — ensure app logs (picked tags, etc.) are visible under uvicorn

## Makefile Convenience
```powershell
make apply-titles input=backend/data/titles.json output=backend/data/titles_tagged.json alpha=0.25 embedder=me5 topk_parent=8 detail=1 topk_children=12
make convert-taxonomy
make clean-cache
```
Defaults (if variables omitted): mode=zero-shot-joint, embedder=minilm, alpha=0.3, topk_parent=8.

## Notes
* Zero‑shot only: keeps the system simple and reproducible without supervised training.
* Determinism: cached label embeddings + fixed taxonomy ensure stable rankings for unchanged configs.

## License / Disclaimer
Example data is illustrative only. Evaluate taxonomy quality and embedding appropriateness for your domain before production use.

---
Local titles, clear consent, portable analysis — stitched together by a small web app.
