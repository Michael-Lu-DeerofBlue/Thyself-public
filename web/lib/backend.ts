export const ANALYZER_BASE = (process.env.NEXT_PUBLIC_ANALYZER_BASE || process.env.NEXT_PUBLIC_ANALYZER_URL || 'http://localhost:5050').replace(/\/$/, '');

export type AnalyzeResponse = {
  t0_ranked: [string, number][];
  t1_ranked: [string, number][]; // if IDs available, can be [id,count]
};

export async function postAnalyze(user_id: string, titles: string[]): Promise<AnalyzeResponse> {
  const res = await fetch(`${ANALYZER_BASE}/analyze`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id, titles })
  });
  if (!res.ok) throw new Error(`Analyze failed: ${res.status}`);
  return res.json();
}

export type RecommendPiece = { title: string; url: string; source?: string; author?: string; publisher?: string; description?: string; date?: string; imageUrl?: string };
export type RecommendResponse = { piece: RecommendPiece };

export async function postRecommend(user_id: string, tags: string[]): Promise<RecommendResponse> {
  // Route through Next.js API to get server-side logs in the dev terminal
  const res = await fetch(`/api/recommend`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id, tags, limit: 1, use_profile: true })
  });
  if (!res.ok) throw new Error(`Recommend failed: ${res.status}`);
  const data = await res.json();
  // Normalize shapes and map image_url -> imageUrl
  const mapImage = (p: any): RecommendPiece => ({
    title: p?.title || 'Untitled',
    url: p?.url || '',
    source: p?.source,
    author: p?.author,
    publisher: p?.publisher,
    description: p?.description,
    date: p?.date,
    imageUrl: p?.imageUrl || p?.image_url || '',
  });
  if (data && typeof data === 'object') {
    if ('piece' in data && data.piece) return { piece: mapImage(data.piece) } as RecommendResponse;
    if ('title' in data && 'url' in data) return { piece: mapImage(data) } as RecommendResponse;
  }
  // Fallback empty
  return { piece: { title: 'Untitled', url: '' } } as RecommendResponse;
}
