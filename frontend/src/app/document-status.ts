export type DocumentStatus = 'uploaded' | 'indexing' | 'indexed' | 'failed' | 'unknown';

const INDEXING_STATUSES = new Set(['submitted', 'queued', 'parsing', 'extracting', 'indexing']);

export function normalizeDocumentStatus(status: unknown): DocumentStatus {
  const value = String(status ?? '').trim().toLowerCase();
  if (INDEXING_STATUSES.has(value)) return 'indexing';
  if (value === 'done' || value === 'indexed') return 'indexed';
  if (value === 'cancelled' || value === 'uploaded') return 'uploaded';
  if (value === 'failed') return 'failed';
  return 'unknown';
}

export const documentStatusLabel: Record<DocumentStatus, string> = {
  uploaded: '已上传',
  indexing: '索引中',
  indexed: '已索引',
  failed: '失败',
  unknown: '状态未知',
};

export const documentStatusStyles: Record<DocumentStatus, { bg: string; color: string }> = {
  indexed:  { bg: '#1a3a22', color: '#3fb950' },
  indexing: { bg: '#2d2a16', color: '#d29922' },
  uploaded: { bg: '#1c2128', color: '#8b949e' },
  failed:   { bg: '#3b1a1a', color: '#f85149' },
  unknown:  { bg: '#252b33', color: '#a8b3c2' },
};
