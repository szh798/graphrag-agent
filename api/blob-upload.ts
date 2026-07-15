import { timingSafeEqual } from 'node:crypto'

import { head } from '@vercel/blob'
import { handleUpload, type HandleUploadBody } from '@vercel/blob/client'

const MAX_UPLOAD_BYTES = 200 * 1024 * 1024
const ALLOWED_EXTENSIONS = new Set([
  'pdf', 'doc', 'docx', 'ppt', 'pptx', 'png', 'jpg', 'jpeg', 'html', 'txt',
  'md', 'markdown',
])
const ALLOWED_CONTENT_TYPES = [
  'application/pdf',
  'application/msword',
  'application/vnd.ms-powerpoint',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  'application/octet-stream',
  'application/zip',
  'image/jpeg',
  'image/png',
  'text/html',
  'text/markdown',
  'text/plain',
]

type UploadMetadata = {
  filename: string
  sizeBytes: number
  language?: string
  enableFormula?: boolean
  enableTable?: boolean
}

function secureEqual(left: string, right: string): boolean {
  const a = Buffer.from(left)
  const b = Buffer.from(right)
  return a.length === b.length && timingSafeEqual(a, b)
}

function requireTrustedProxy(request: Request): void {
  const expected = process.env.BACKEND_PROXY_SECRET?.trim() ?? ''
  const supplied = request.headers.get('x-graphrag-proxy-secret')?.trim() ?? ''
  if (!expected || !supplied || !secureEqual(expected, supplied)) {
    throw new Error('Unauthorized upload request')
  }
}

function parseMetadata(value: string | null | undefined): UploadMetadata {
  let parsed: Partial<UploadMetadata>
  try {
    parsed = JSON.parse(value || '{}')
  } catch {
    throw new Error('Invalid upload metadata')
  }

  const filename = typeof parsed.filename === 'string' ? parsed.filename.trim() : ''
  const sizeBytes = Number(parsed.sizeBytes)
  const extension = filename.split('.').pop()?.toLowerCase() ?? ''
  if (!filename || filename.includes('/') || filename.includes('\\')) {
    throw new Error('Invalid filename')
  }
  if (!ALLOWED_EXTENSIONS.has(extension)) throw new Error('Unsupported file format')
  if (!Number.isFinite(sizeBytes) || sizeBytes <= 0 || sizeBytes > MAX_UPLOAD_BYTES) {
    throw new Error('File exceeds the 200MB upload limit')
  }
  return {
    filename,
    sizeBytes,
    language: typeof parsed.language === 'string' ? parsed.language : 'ch',
    enableFormula: parsed.enableFormula !== false,
    enableTable: parsed.enableTable !== false,
  }
}

function callbackOrigin(request: Request): string {
  return new URL(request.url).origin
}

export default async function handler(request: Request): Promise<Response> {
  if (request.method !== 'POST') {
    return Response.json({ code: 405, msg: 'Method not allowed', data: null }, { status: 405 })
  }

  try {
    const body = (await request.json()) as HandleUploadBody
    if (body.type === 'blob.generate-client-token') requireTrustedProxy(request)

    const result = await handleUpload({
      body,
      request,
      token: process.env.BLOB_READ_WRITE_TOKEN,
      onBeforeGenerateToken: async (pathname, clientPayload, multipart) => {
        requireTrustedProxy(request)
        const metadata = parseMetadata(clientPayload)
        const expectedPath = `uploads/${metadata.filename}`
        if (pathname !== expectedPath) throw new Error('Upload pathname does not match filename')
        if (metadata.sizeBytes > 100 * 1024 * 1024 && !multipart) {
          throw new Error('Files larger than 100MB require multipart upload')
        }
        return {
          allowedContentTypes: ALLOWED_CONTENT_TYPES,
          maximumSizeInBytes: MAX_UPLOAD_BYTES,
          addRandomSuffix: true,
          allowOverwrite: false,
          validUntil: Date.now() + 15 * 60 * 1000,
          tokenPayload: JSON.stringify(metadata),
          callbackUrl: `${callbackOrigin(request)}/api/v1/documents/upload/direct`,
        }
      },
      onUploadCompleted: async ({ blob, tokenPayload }) => {
        const metadata = parseMetadata(tokenPayload)
        const details = await head(blob.url, { token: process.env.BLOB_READ_WRITE_TOKEN })
        if (details.size > MAX_UPLOAD_BYTES) throw new Error('Uploaded file exceeds 200MB')

        const response = await fetch(
          `${callbackOrigin(request)}/api/v1/documents/upload/complete`,
          {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-GraphRAG-Proxy-Secret': process.env.BACKEND_PROXY_SECRET ?? '',
              'X-GraphRAG-Internal-Upload': '1',
              'X-Request-ID': crypto.randomUUID(),
            },
            body: JSON.stringify({
              ...metadata,
              sizeBytes: details.size,
              contentType: details.contentType,
              blob,
            }),
          },
        )
        if (!response.ok) throw new Error('Could not register completed upload')
        const payload = await response.json() as { code?: number }
        if (payload.code !== 0) throw new Error('Could not register completed upload')
      },
    })

    return Response.json(result)
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Upload request failed'
    const status = message.startsWith('Unauthorized') ? 401 : 400
    return Response.json({ code: status, msg: message, data: null }, { status })
  }
}
