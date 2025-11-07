"use client";

import { useCallback, useState, useEffect } from 'react';
import SectionCard from '@/components/SectionCard';
import RadarHexChart from '@/components/RadarHexChart';
import TagsBarChart from '@/components/TagsBarChart';
import PieceCard from '@/components/PieceCard';
import { getTitles, ackTitles, setProfile, appendPiece, extensionAvailable, getProfile, getArchive } from '@/lib/bridge';
import { postAnalyze, postRecommend } from '@/lib/backend';
import { AnalysisProfile } from '@/lib/types';

function toAnalysisProfile(resp: { t0_ranked: [string, number][]; t1_ranked: [string, number][] }): AnalysisProfile {
  const t0Ranked = resp.t0_ranked || [];
  const max = Math.max(1, ...t0Ranked.map(([, c]) => c));
  const t0Hex = Array.from({ length: 6 }, (_, i) => {
    const e = t0Ranked[i];
    return e ? { label: e[0], score: Math.round((e[1] / max) * 100) } : { label: '', score: 0 };
  });
  const t1Tags = (resp.t1_ranked || []).map(([tag, count]) => ({ tag, count }));
  // piece stub; actual piece will be fetched in recommend step
  const piece = { title: '—', author: '', description: '' };
  return { t0Hex, t1Tags, piece };
}

function mergeProfiles(base: { t0: Record<string, number>; t1: Record<string, number> }, inc: { t0: Record<string, number>; t1: Record<string, number> }) {
  const outT0: Record<string, number> = { ...(base.t0 || {}) };
  const outT1: Record<string, number> = { ...(base.t1 || {}) };
  for (const [k, v] of Object.entries(inc.t0 || {})) outT0[k] = (outT0[k] || 0) + (v || 0);
  for (const [k, v] of Object.entries(inc.t1 || {})) outT1[k] = (outT1[k] || 0) + (v || 0);
  return { t0: outT0, t1: outT1 };
}

// Helper: take deterministic top-3 T1 tags from ranked entries, mapping "Parent > Child" -> "Child"
function topThreeT1(tagsRanked: [string, number][]): string[] {
  const mapped = tagsRanked
    .map(([k]) => (typeof k === 'string' && k.includes(' > ')) ? k.split(' > ')[1] : k)
    .filter(Boolean) as string[];
  const uniq: string[] = [];
  for (const t of mapped) { if (!uniq.includes(t)) uniq.push(t); if (uniq.length >= 3) break; }
  return uniq.slice(0, 3);
}

export default function AnalysisClient() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [profile, setProfileState] = useState<AnalysisProfile | null>(null);
  const [extConnected, setExtConnected] = useState<boolean>(false);

  // On mount: connect, load existing profile (if any), then analyze if new titles exist
  useEffect(() => {
    (async () => {
      setBusy(true);
      setError(null);
      try {
        const connected = await extensionAvailable();
        setExtConnected(connected);
        if (!connected) {
          setError('Extension not connected. Install/enable the extension.');
          return;
        }

        // 1) bootstrap from stored profile if present
        const existing = await getProfile();
        if (existing && existing.t0 && existing.t1) {
          const t0Ranked = Object.entries(existing.t0).sort((a, b) => b[1] - a[1]);
          const t1Ranked = Object.entries(existing.t1).sort((a, b) => b[1] - a[1]);
          const ap = toAnalysisProfile({ t0_ranked: t0Ranked as any, t1_ranked: t1Ranked as any });
          setProfileState(ap);
        }

        // 2) check for new titles and analyze if present
        const titlesBatch = await getTitles();
        if (titlesBatch && titlesBatch.length > 0) {
          try {
            const titles = titlesBatch.map(t => t.title);
            const analyze = await postAnalyze('local-user', titles);
            const incoming = { t0: Object.fromEntries(analyze.t0_ranked), t1: Object.fromEntries(analyze.t1_ranked) } as any;
            let merged: { t0: Record<string, number>; t1: Record<string, number> } = incoming as any;
            if (existing && existing.t0 && existing.t1) merged = mergeProfiles(existing as any, incoming);
            await setProfile(merged);
            // ACK processed titles
            await ackTitles(titlesBatch.map(t => t.id));
            // Update visible charts
            const t0Ranked2 = Object.entries(merged.t0 || {}).sort((a: any, b: any) => b[1] - a[1]);
            const t1Ranked2 = Object.entries(merged.t1 || {}).sort((a: any, b: any) => b[1] - a[1]);
            setProfileState(toAnalysisProfile({ t0_ranked: t0Ranked2 as any, t1_ranked: t1Ranked2 as any }));

            // Recommend piece only when there are new titles; use deterministic top-3 T1 tags
            const tags = topThreeT1(t1Ranked2 as any);
            if (tags.length === 3) {
              let rec: any = null; try { rec = await postRecommend('local-user', tags); } catch {}
              const piece = (rec && rec.piece) ? rec.piece : null;
              if (piece?.title) {
                await appendPiece({ date: new Date().toISOString().slice(0, 10).replaceAll('-', '/'), title: piece.title, source: piece.publisher || piece.source, url: piece.url, image_url: (piece as any).imageUrl || (piece as any).image_url });
                setProfileState(prev => prev ? { ...prev, piece: { title: piece.title || '—', author: piece.author || '', publisher: piece.publisher || piece.source || '', imageUrl: piece.imageUrl || '', url: piece.url || '', description: piece.description || '' } } : prev);
              }
            }
          } catch (e: any) {
            setError(e?.message || 'Analyze failed');
          }
        } else {
          // No new titles; per request, skip recommend/append flow
          // Use archived latest piece (if any) for display
          try {
            const arch = await getArchive();
            if (Array.isArray(arch) && arch.length > 0) {
              const top = arch[0] as any;
              setProfileState(prev => prev ? {
                ...prev,
                piece: {
                  title: top.title || '—',
                  author: '',
                  publisher: top.source || '',
                  imageUrl: top.imageUrl || top.image_url || '',
                  url: top.url || '',
                  description: ''
                }
              } : prev);
            }
          } catch {}
        }
      } finally {
        setBusy(false);
      }
    })();
  }, []);

  return (
    <div className="flex flex-col gap-6">
      {error && (
        <div className="text-sm text-red-600">{error}</div>
      )}
      {profile ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          {/* Left column: Radar graph */}
          <div>
            <SectionCard title="Profile">
              <div className="min-h-[280px] flex items-center justify-center">
                <RadarHexChart data={profile.t0Hex} />
              </div>
            </SectionCard>
          </div>

          {/* Right column: Subtopic (histogram) and Recommended Piece stacked */}
          <div className="flex flex-col gap-6">
            <SectionCard title="Subtopic">
              <div className="min-h-[280px]">
                <TagsBarChart data={profile.t1Tags} />
              </div>
            </SectionCard>
            <SectionCard title="Recommended Piece">
              <PieceCard piece={profile.piece} />
            </SectionCard>
          </div>
        </div>
      ) : (
        <div className="text-sm text-neutral-600">{extConnected ? 'No profile yet. Browse YouTube to collect titles.' : 'Install/enable the extension.'}</div>
      )}
      {busy && (
        <div className="text-xs text-neutral-500">Working…</div>
      )}
    </div>
  );
}
