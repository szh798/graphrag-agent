import { indexProgressPercent, type StructuredIndexProgress } from './index-progress.ts';

const ACTIVE_INDEX_STATUSES = new Set([
  'submitted',
  'queued',
  'parsing',
  'extracting',
  'indexing',
]);

interface IndexStateLike {
  status?: string | null;
  raw_status?: string | null;
  progress?: number | null;
}

interface DocumentIndexStateLike {
  status?: string | null;
  progress?: number | StructuredIndexProgress | null;
  indexes?: Record<string, IndexStateLike | undefined> | null;
}

export function isActiveIndexState(index: IndexStateLike | null | undefined): boolean {
  const rawStatus = String(index?.raw_status ?? index?.status ?? '').trim().toLowerCase();
  return ACTIVE_INDEX_STATUSES.has(rawStatus);
}

export function hasActiveDocumentIndex(document: DocumentIndexStateLike): boolean {
  if (ACTIVE_INDEX_STATUSES.has(String(document.status ?? '').trim().toLowerCase())) return true;
  return Object.values(document.indexes ?? {}).some(isActiveIndexState);
}

export function documentIndexProgress(document: DocumentIndexStateLike): number {
  if (document.progress != null) return indexProgressPercent(document.progress);
  const childProgress = Object.values(document.indexes ?? {})
    .filter(isActiveIndexState)
    .map(index => indexProgressPercent(index?.progress ?? 0));
  return childProgress.length > 0 ? Math.max(...childProgress) : 0;
}
