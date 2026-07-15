import { upload } from '@vercel/blob/client';
import { getAuthorizationHeaders } from './api';

export const MAX_DIRECT_UPLOAD_BYTES = 200 * 1024 * 1024;

export interface DirectUploadOptions {
  language?: string;
  enableFormula?: boolean;
  enableTable?: boolean;
  signal?: AbortSignal;
  onProgress?: (event: { loaded: number; total: number; percentage: number }) => void;
}

/**
 * Upload a document without sending its bytes through a Vercel Function.
 *
 * The trusted proxy authorizes the token exchange. Public visitors may use the
 * route, while document ownership remains scoped to their visitor or account.
 */
export async function uploadDocumentDirect(file: File, options: DirectUploadOptions = {}) {
  if (file.size <= 0) throw new Error('文件为空');
  if (file.size > MAX_DIRECT_UPLOAD_BYTES) throw new Error('文件超过 200MB 上限');

  const headers = await getAuthorizationHeaders();

  return upload(`uploads/${file.name}`, file, {
    access: 'private',
    handleUploadUrl: '/api/v1/documents/upload/direct',
    multipart: file.size > 100 * 1024 * 1024,
    abortSignal: options.signal,
    onUploadProgress: options.onProgress,
    headers,
    clientPayload: JSON.stringify({
      filename: file.name,
      sizeBytes: file.size,
      language: options.language ?? 'ch',
      enableFormula: options.enableFormula !== false,
      enableTable: options.enableTable !== false,
    }),
  });
}
