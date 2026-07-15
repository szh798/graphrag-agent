import type { Document } from './store';

export const documentStatusLabel: Record<Document['status'], string> = {
  uploaded: '已上传',
  indexing: '索引中',
  indexed: '已索引',
  failed: '失败',
};
