/**
 * GraphRAG Studio — Backend API Client
 * Base: http://localhost:8000/api/v1
 * All functions return the `data` field; throw ApiError on code !== 0
 */

const BASE = import.meta.env.VITE_API_BASE_URL ?? (
  import.meta.env.PROD ? '/api/v1' : 'http://localhost:8000/api/v1'
);

export class ApiError extends Error {
  code: number;
  retryAfterSeconds?: number;
  constructor(code: number, msg: string, retryAfterSeconds?: number) {
    super(msg);
    this.code = code;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

function retryAfterSeconds(response: Response): number | undefined {
  const value = response.headers.get('Retry-After');
  if (!value) return undefined;
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds) : undefined;
}

async function request<T>(
  method: string,
  path: string,
  options: {
    body?: unknown;
    formData?: FormData;
    params?: Record<string, string | number | boolean | undefined | null>;
  } = {}
): Promise<T> {
  let url = BASE + path;

  if (options.params) {
    const parts = Object.entries(options.params)
      .filter(([, v]) => v !== undefined && v !== null && v !== '')
      .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
    if (parts.length) url += '?' + parts.join('&');
  }

  const init: RequestInit = { method };
  if (options.formData) {
    init.body = options.formData;
  } else if (options.body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(options.body);
  }

  const res = await fetch(url, init);
  const json = await res.json();
  if (json.code !== 0) {
    throw new ApiError(json.code, json.msg ?? 'Unknown error', retryAfterSeconds(res));
  }
  return json.data as T;
}

const get = <T>(path: string, params?: Record<string, string | number | boolean | undefined | null>) =>
  request<T>('GET', path, { params });
const post = <T>(path: string, body?: unknown) => request<T>('POST', path, { body });
const postForm = <T>(path: string, fd: FormData) => request<T>('POST', path, { formData: fd });
const del = <T>(path: string) => request<T>('DELETE', path);

// ─── Response Types ───────────────────────────────────────────────────────────

export interface ApiDoc {
  doc_id: string;
  filename: string;
  format: string;
  pages: number | null;
  status: 'uploaded' | 'indexing' | 'indexed' | 'failed';
  uploaded_at?: string;
  upload_date?: string;
  job_id?: string | null;
  file_size?: number;
  size_bytes?: number;
  error_msg?: string | null;
}

export interface ApiJobStatus {
  job_id: string;
  doc_id: string;
  status: 'submitted' | 'queued' | 'parsing' | 'extracting' | 'indexing' | 'done' | 'failed' | 'cancelled';
  stage: string;
  progress: number; // 0.0–1.0
  started_at?: string;
  updated_at?: string;
  error_msg?: string | null;
}

export interface ApiIndexSummary {
  nodes: number;
  edges: number;
  pages: number;
  extractions: number;
  duration_seconds: number;
}

export interface ApiExtractionRecord {
  text: string;
  type: string;
  char_start?: number | null;
  char_end?: number | null;
  alignment?: string | null;
  page: number;
  doc_id: string;
}

export interface ApiIndexResult {
  job_id: string;
  doc_id: string;
  status: string;
  stage?: string;
  created_at?: string;
  elapsed_seconds?: number;
  summary?: ApiIndexSummary;
  stats?: {
    nodes?: number;
    edges?: number;
    pages?: number;
    raw_extractions?: number;
    elapsed_seconds?: number;
  };
  extractions?: ApiExtractionRecord[];
  nodes?: ApiKGNode[];
  edges?: ApiKGEdge[];
  nodes_added?: number;
  edges_added?: number;
  total_nodes?: number;
  total_edges?: number;
  pages_processed?: number;
  extractions_count?: number;
  duration_seconds?: number;
}

export interface ApiDocumentExtractions {
  doc_id: string;
  job_id: string;
  total: number;
  page: number;
  page_size: number;
  items: ApiExtractionRecord[];
  summary: ApiIndexSummary;
}

export interface ApiKGNode {
  id: string;
  name: string;
  type: string;
  page: number;
  confidence: string;
  degree: number;
  source_doc: string;
  // Only present in detail endpoint:
  degree_centrality?: number;
  neighbor_count?: number;
}

export interface ApiKGEdge {
  id: string;
  source: string;
  target: string;
  relation: string;
  doc_id: string;
  page: number;
}

export interface ApiComponentHealth {
  status: string;
  backend?: string;
  mode?: string;
  active_parser?: string;
  mineru_configured?: boolean;
  local_supported_formats?: string[];
  path?: string;
  exists?: boolean;
  base_url?: string;
  key_configured?: boolean;
  provider?: string;
  model?: string;
  index_model?: string;
  data_dir?: string;
  persistence?: string;
  persistent?: boolean;
  warning?: string | null;
  kg_nodes_exists?: boolean;
  kg_edges_exists?: boolean;
  uploads_dir_exists?: boolean;
  database?: string;
  vector_dimensions?: number;
  uri_configured?: boolean;
  url_configured?: boolean;
  token_configured?: boolean;
  durable?: boolean;
  documents?: number;
  jobs?: number;
  chat_sessions?: number;
  batches?: number;
  error?: string;
}

export interface ApiHealthData {
  status: string;
  version: string;
  uptime_seconds: number;
  production_ready?: boolean;
  components: {
    document_parser?: ApiComponentHealth;
    mineru_venv: ApiComponentHealth;
    mineru_api?: ApiComponentHealth;
    langextract_venv: ApiComponentHealth;
    llm_api?: ApiComponentHealth;
    llm_index_api?: ApiComponentHealth;
    deepseek_api?: ApiComponentHealth;
    storage: ApiComponentHealth;
    graph_database?: ApiComponentHealth;
    app_database?: ApiComponentHealth;
    blob_storage?: ApiComponentHealth;
    task_queue?: ApiComponentHealth;
  };
}

export interface ApiStats {
  total_documents: number;
  indexed_documents: number;
  failed_documents: number;
  total_nodes: number;
  total_edges: number;
  total_queries: number;
  active_jobs: number;
  storage_used_mb: number;
}

export interface ApiToolCall {
  step: number;
  tool_name: string;
  tool_input: string;
  tool_output: string;
}

export interface ApiQueryResult {
  id: string;
  session_id?: string;
  question: string;
  answer: string;
  tool_calls: ApiToolCall[];
  cited_nodes: string[]; // node IDs
  cited_chunks?: string[];
  duration_seconds: number;
  timestamp: string;
  session?: ApiChatSessionSummary;
}

export type ApiQueryStreamEvent =
  | { event: 'status'; data: { message: string } }
  | { event: 'tool_call'; data: ApiToolCall }
  | { event: 'answer_delta'; data: { text: string } }
  | { event: 'done'; data: ApiQueryResult }
  | { event: 'error'; data: { code: number; message: string } };

export interface ApiChatSessionMessage {
  id: string;
  role: 'human' | 'ai';
  content: string;
  timestamp: string;
  query_id?: string;
  tool_calls?: ApiToolCall[];
  cited_nodes?: string[];
  cited_chunks?: string[];
  duration_seconds?: number;
}

export interface ApiChatSessionSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  last_question: string;
  last_answer: string;
}

export interface ApiChatSession extends ApiChatSessionSummary {
  messages: ApiChatSessionMessage[];
}

export interface ApiBatchItemResult {
  id?: string;
  question: string;
  answer?: string;
  error?: string;
  tool_calls?: ApiToolCall[];
  cited_nodes?: string[];
  cited_chunks?: string[];
  duration_seconds?: number;
  timestamp?: string;
}

export interface ApiBatchResult {
  batch_id: string;
  total: number;
  completed: number;
  failed: number;
  status: 'submitted' | 'running' | 'done' | 'cancelled';
  created_at: string;
  updated_at?: string;
  cancel_requested?: boolean;
  results: ApiBatchItemResult[];
}

export interface ApiBatchSummary {
  batch_id: string;
  total: number;
  completed: number;
  failed: number;
  status: 'submitted' | 'running' | 'done' | 'cancelled';
  created_at: string;
  updated_at?: string;
  cancel_requested?: boolean;
}

export interface ApiSearchResult {
  query: string;
  total: number;
  items: ApiKGNode[];
}

export interface ApiPathResult {
  from: { id: string; name: string; type: string };
  to: { id: string; name: string; type: string };
  max_hops: number;
  total_paths: number;
  paths: Array<{
    length: number;
    nodes: Array<{ id: string; name: string; type: string }>;
    edges?: Array<{ source: string; target: string; relation: string }>;
  }>;
}

export interface ApiGraphSearchResult {
  query: string;
  matched_nodes: ApiKGNode[];
  subgraph_edges: ApiKGEdge[];
  total_nodes: number;
}

function toChatHistory(history: { question: string; answer: string }[] = []) {
  return history.flatMap(h => [
    { role: 'human' as const, content: h.question },
    { role: 'ai' as const, content: h.answer },
  ]);
}

function parseSSEBlock(block: string): ApiQueryStreamEvent | null {
  const lines = block.split(/\r?\n/);
  let event = 'message';
  const dataLines: string[] = [];

  for (const line of lines) {
    if (line.startsWith('event:')) {
      event = line.slice(6).trim();
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trimStart());
    }
  }

  if (!dataLines.length) return null;

  return {
    event,
    data: JSON.parse(dataLines.join('\n')),
  } as ApiQueryStreamEvent;
}

// ─── API Functions ────────────────────────────────────────────────────────────

export const api = {
  // A: Documents
  listDocuments: (page = 1, pageSize = 100) =>
    get<{ total: number; page: number; page_size: number; items: ApiDoc[] }>(
      '/documents', { page, page_size: pageSize }
    ),

  getDocument: (docId: string) => get<ApiDoc>(`/documents/${docId}`),

  getDocumentIndexResult: (docId: string) =>
    get<ApiIndexResult>(`/documents/${docId}/index-result`),

  getDocumentExtractions: (docId: string, page = 1, pageSize = 100) =>
    get<ApiDocumentExtractions>(`/documents/${docId}/extractions`, { page, page_size: pageSize }),

  uploadDocument: (file: File) => {
    const fd = new FormData();
    fd.append('file', file);
    return postForm<{ doc_id: string; filename: string; format: string; status: string }>(
      '/documents/upload', fd
    );
  },

  deleteDocument: (docId: string) =>
    del<{ doc_id: string; removed_nodes: number; removed_edges: number }>(`/documents/${docId}`),

  // B: Indexing
  startIndexing: (docId: string) =>
    post<{ job_id: string; doc_id: string; status: string }>('/index/start', { doc_id: docId }),

  getJobStatus: (jobId: string) => get<ApiJobStatus>(`/index/status/${jobId}`),

  getJobResult: (jobId: string) => get<ApiIndexResult>(`/index/result/${jobId}`),

  cancelJob: (jobId: string) => del<{ job_id: string }>(`/index/jobs/${jobId}`),

  // C: Knowledge Graph
  getNodes: (params?: { page?: number; pageSize?: number; type?: string; docId?: string }) =>
    get<{ total: number; page: number; page_size: number; items: ApiKGNode[] }>('/kg/nodes', {
      page: params?.page,
      page_size: params?.pageSize ?? 500,
      type: params?.type,
      doc_id: params?.docId,
    }),

  getEdges: (params?: { page?: number; pageSize?: number; docId?: string }) =>
    get<{ total: number; page: number; page_size: number; items: ApiKGEdge[] }>('/kg/edges', {
      page: params?.page,
      page_size: params?.pageSize ?? 2000,
      doc_id: params?.docId,
    }),

  getNodeDetail: (nodeId: string) => get<ApiKGNode>(`/kg/nodes/${nodeId}`),

  getNodeNeighbors: (nodeId: string, hops = 1) =>
    get<{
      center: ApiKGNode;
      hops: number;
      neighbors_by_hop: Record<string, ApiKGNode[]>;
      total_neighbors: number;
    }>(`/kg/nodes/${nodeId}/neighbors`, { hops }),

  getKGStats: () =>
    get<{ total_nodes: number; total_edges: number; type_distribution: Record<string, number> }>('/kg/stats'),

  exportKG: () => get<{ nodes: ApiKGNode[]; edges: ApiKGEdge[] }>('/kg/export'),

  // D: QA Query
  query: (question: string, history: { question: string; answer: string }[] = [], sessionId?: string | null) => {
    const chatHistory = toChatHistory(history);
    return post<ApiQueryResult>('/query', { question, history: chatHistory, session_id: sessionId ?? undefined });
  },

  streamQuery: async (
    question: string,
    history: { question: string; answer: string }[] = [],
    sessionId: string | null | undefined,
    onEvent: (event: ApiQueryStreamEvent) => void
  ) => {
    const res = await fetch(`${BASE}/query/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, history: toChatHistory(history), session_id: sessionId ?? undefined }),
    });

    if (!res.ok || !res.body) {
      throw new ApiError(
        res.status,
        `Stream request failed (${res.status})`,
        retryAfterSeconds(res),
      );
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    const dispatch = (block: string) => {
      const parsed = parseSSEBlock(block);
      if (!parsed) return;
      onEvent(parsed);
      if (parsed.event === 'error') {
        throw new ApiError(parsed.data.code, parsed.data.message);
      }
    };

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop() ?? '';
      for (const block of blocks) {
        if (block.trim()) dispatch(block);
      }
    }

    buffer += decoder.decode();
    if (buffer.trim()) dispatch(buffer);
  },

  getQueryHistory: (page = 1, pageSize = 50) =>
    get<{ total: number; page: number; page_size: number; items: ApiQueryResult[] }>(
      '/query/history', { page, page_size: pageSize }
    ),

  createQuerySession: () => post<ApiChatSession>('/query/sessions'),

  getQuerySessions: (page = 1, pageSize = 50) =>
    get<{ total: number; page: number; page_size: number; items: ApiChatSessionSummary[] }>(
      '/query/sessions', { page, page_size: pageSize }
    ),

  getQuerySession: (sessionId: string) => get<ApiChatSession>(`/query/sessions/${sessionId}`),

  startBatch: (questions: string[]) =>
    post<{ batch_id: string; total: number; status: string; created_at: string }>('/query/batch', { questions }),

  listBatches: (page = 1, pageSize = 20) =>
    get<{ total: number; page: number; page_size: number; items: ApiBatchSummary[] }>(
      '/query/batch', { page, page_size: pageSize }
    ),

  getBatchResult: (batchId: string) => get<ApiBatchResult>(`/query/batch/${batchId}`),

  cancelBatch: (batchId: string) =>
    del<{ batch_id: string; previous_status: string; status: string; cancel_requested: boolean }>(`/query/batch/${batchId}`),

  // E: Search
  searchEntities: (q: string, type?: string, limit = 15) =>
    get<ApiSearchResult>('/search/entities', {
      q,
      type: type && type !== '全部类型' ? type : undefined,
      limit,
    }),

  searchPath: (fromId: string, toId: string, maxHops = 3) =>
    get<ApiPathResult>('/search/path', { from: fromId, to: toId, max_hops: maxHops }),

  searchGraph: (q: string, includeNeighbors = false) =>
    get<ApiGraphSearchResult>('/search/graph', { q, include_neighbors: includeNeighbors }),

  // F: System
  getHealth: () => get<ApiHealthData>('/health'),

  getSystemStats: () => get<ApiStats>('/system/stats'),

  getDemoData: () =>
    get<{ nodes: ApiKGNode[]; edges: ApiKGEdge[]; stats: Record<string, unknown> }>('/system/demo'),
};
