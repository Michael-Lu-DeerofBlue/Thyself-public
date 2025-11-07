import { NextRequest } from 'next/server';

export const dynamic = 'force-dynamic';

const ANALYZER_BASE = (process.env.NEXT_PUBLIC_ANALYZER_BASE || process.env.NEXT_PUBLIC_ANALYZER_URL || 'http://localhost:5050').replace(/\/$/, '');

export async function POST(req: NextRequest) {
  try {
    const body = await req.json();
    const { user_id = '', tags = [], limit = 1, use_profile = true } = body || {};

    const res = await fetch(`${ANALYZER_BASE}/recommend`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id, tags, limit, use_profile }),
    });

    let payload: any = null;
    try {
      payload = await res.json();
    } catch {
      payload = { error: 'non-json response' };
    }

    // Server-side debug log visible in the Next.js terminal
    const piece = payload?.piece || payload || {};
    const summary = {
      status: res.status,
      tags,
      use_profile,
      piece: {
        title: piece?.title || '',
        source: piece?.source || piece?.publisher || '',
        url: piece?.url || '',
        date: piece?.date || '',
        image_url: piece?.image_url || '',
      },
    };
    console.info('[api/recommend] response', summary);

    return Response.json(payload, { status: res.status });
  } catch (e: any) {
    console.error('[api/recommend] error', e?.message || String(e));
    return Response.json({ error: e?.message || 'recommend proxy failed' }, { status: 500 });
  }
}
