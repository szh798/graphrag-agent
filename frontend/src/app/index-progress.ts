export interface StructuredIndexProgress {
  parsed_pages: number;
  total_pages: number;
  extracted_entities?: number;
}


export function indexProgressPercent(progress: number | StructuredIndexProgress): number {
  if (typeof progress === 'number') {
    if (!Number.isFinite(progress)) return 0;
    const percent = progress <= 1 ? progress * 100 : progress;
    return Math.max(0, Math.min(100, Math.round(percent)));
  }

  const parsedPages = Number(progress.parsed_pages);
  const totalPages = Number(progress.total_pages);
  if (!Number.isFinite(parsedPages) || !Number.isFinite(totalPages) || totalPages <= 0) {
    return 0;
  }
  return Math.max(0, Math.min(100, Math.round((parsedPages / totalPages) * 100)));
}
