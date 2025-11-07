export type AnalysisProfile = {
  t0Hex: { label: string; score: number }[]; // top 6 parents by count, normalized 0–100
  t1Tags: { tag: string; count: number }[]; // descending
  piece: {
    title: string;
    author: string;
    publisher?: string;
    imageUrl?: string;
    url?: string;
    description?: string;
  };
};
