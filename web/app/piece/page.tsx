"use client";
import { useEffect, useRef, useState } from 'react';
import { getArchive, getProfile, appendPiece, extensionAvailable } from '@/lib/bridge';
import { postRecommend } from '@/lib/backend';
import SectionCard from '@/components/SectionCard';
import PieceCard from '@/components/PieceCard';

export default function PiecePage() {
  const [piece, setPiece] = useState<{ title: string; author?: string; publisher?: string; description?: string; imageUrl?: string; url?: string } | null>(null);
  const startedRef = useRef(false);
  useEffect(() => {
    if (startedRef.current) return; // Avoid double-invoke in React Strict Mode (dev)
    startedRef.current = true;
    (async () => {
      // Wait for the extension bridge to be ready to avoid premature fallback
      let connected = await extensionAvailable();
      if (!connected) {
        for (let i = 0; i < 5 && !connected; i++) {
          await new Promise(r => setTimeout(r, 300));
          connected = await extensionAvailable();
        }
      }

      // Try fetching archive with a couple retries in case storage is still initializing
      let arch = await getArchive();
      if (!arch || !Array.isArray(arch)) {
        for (let j = 0; j < 3 && (!arch || !Array.isArray(arch)); j++) {
          await new Promise(r => setTimeout(r, 200));
          arch = await getArchive();
        }
      }
      if (arch && arch.length) {
        const top = arch[0] as any;
        const titleStr = typeof top?.title === 'string' ? top.title.trim() : '';
        const isNoResult = titleStr.toLowerCase() === 'no result' || titleStr === '';
        const hasUrl = Boolean(top?.url);
        if (!isNoResult && hasUrl) {
          // Use archived piece as-is; do NOT require image to avoid unnecessary recommend calls
          try { console.info('[piece/page] using archive[0]', top); } catch {}
          setPiece({ title: top.title, publisher: top.source, imageUrl: top.imageUrl || top.image_url || '', url: top.url || '' });
          return;
        }
        // If latest is placeholder or missing critical URL, attempt a fresh recommend
        try { console.info('[piece/page] archive[0] requires refresh — reason=', { isNoResult, hasUrl }); } catch {}
      }
      // Fallback: if no archive yet but a profile exists, generate today's piece
      const prof = await getProfile();
      if (prof && prof.t1) {
        const t1Ranked = Object.entries(prof.t1 || {}).sort((a:any,b:any)=> (b[1]||0) - (a[1]||0));
        // Convert keys like "Parent > Child" → "Child" and dedupe
        const tags = Array.from(new Set(
          t1Ranked.map(([k]) => typeof k === 'string' && k.includes(' > ') ? k.split(' > ')[1] : (k as any))
        )).filter(Boolean).slice(0,3) as string[];
        if (tags.length === 3) {
          try {
            const rec = await postRecommend('local-user', tags);
            try { console.info('[piece/page] postRecommend response', { tags, response: rec }); } catch {}
            const p = rec.piece;
            if (p?.title) {
              const today = new Date().toISOString().slice(0,10).replaceAll('-', '/');
              await appendPiece({ date: today, title: p.title, source: p.publisher || p.source, url: p.url, image_url: (p as any).imageUrl || (p as any).image_url });
              setPiece({ title: p.title, author: p.author || '', publisher: p.publisher || p.source || '', imageUrl: p.imageUrl || '', url: p.url || '' });
            }
          } catch {}
        }
      }
    })();
  }, []);
  return (
    <main className="min-h-screen flex flex-col items-center justify-start gap-8 pt-24 px-6 text-center">
      <h1 className="h-title text-4xl font-bold">Piece</h1>
      <div className="w-full max-w-xl">
        <SectionCard title="Today">
          {piece ? <PieceCard piece={{ title: piece.title, author: piece.author || '', publisher: piece.publisher, imageUrl: piece.imageUrl, url: piece.url }} /> : <div className="text-sm text-neutral-600">No piece yet. Run Analyze first.</div>}
        </SectionCard>
      </div>
    </main>
  );
}
