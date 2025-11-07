import { AnalysisProfile } from '@/lib/types';

type Piece = AnalysisProfile['piece'];

export default function PieceCard({ piece }: { piece: Piece }) {
  const title = piece?.title ?? '—';
  const author = piece?.author ?? '';
  const publisher = piece?.publisher ?? '';
  const img = piece?.imageUrl ?? '';
  const url = piece?.url ?? '';
  const description = piece?.description ?? '';
  return (
    <div className="flex flex-col items-center gap-4 text-center">
      {url ? (
        <a href={url} target="_blank" rel="noopener noreferrer" className="bg-neutral-200 w-48 h-48 md:w-60 md:h-60 rounded-sm flex items-center justify-center text-neutral-500 text-sm overflow-hidden">
          {img ? <img src={img} alt={title || 'piece'} className="w-full h-full object-cover" /> : 'Image'}
        </a>
      ) : (
        <div className="bg-neutral-200 w-48 h-48 md:w-60 md:h-60 rounded-sm flex items-center justify-center text-neutral-500 text-sm">
          {img ? <img src={img} alt={title || 'piece'} className="w-full h-full object-cover" /> : 'Image'}
        </div>
      )}
      <div className="space-y-2">
        {url ? (
          <h3 className="h-title text-lg font-semibold">
            <a href={url} target="_blank" rel="noopener noreferrer" className="hover:underline">{title}</a>
          </h3>
        ) : (
          <h3 className="h-title text-lg font-semibold">{title}</h3>
        )}
        <p className="text-sm text-neutral-600">By {author}{publisher ? ` (${publisher})` : ''}</p>
        {description && (
          <p className="f-piece text-sm leading-relaxed max-w-prose mx-auto text-neutral-700">
            {description}
          </p>
        )}
      </div>
    </div>
  );
}
