/**
 * GraphRAG Studio — Backend API Client
 * Base: http://localhost:8000/api/v1
 * All functions return the `data` field; throw ApiError on code !== 0
 */

import type { StructuredIndexProgress } from './index-progress';

const BASE = import.meta.env.VITE_API_BASE_URL ?? (
  import.meta.env.PROD ? '/api/v1' : 'http://localhost:8000/api/v1'
);

type AuthTokenProvider = () => Promise<string | null>;
let authTokenProvider: AuthTokenProvider | null = null;
let inMemoryVisitorId: string | null = null;

const CLIENT_VISITOR_STORAGE_KEY = 'graphrag_client_visitor_id_v1';
const CLIENT_VISITOR_HEADER = 'X-GraphRAG-Client-Visitor-ID';
const CANONICAL_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

export function setAuthTokenProvider(provider: AuthTokenProvider | null) {
  authTokenProvider = provider;
}

function getClientVisitorId(): string | null {
  if (typeof window === 'undefined' || typeof globalThis.crypto?.randomUUID !== 'function') {
    return null;
  }
  if (inMemoryVisitorId) return inMemoryVisitorId;

  try {
    const existing = window.localStorage.getItem(CLIENT_VISITOR_STORAGE_KEY)?.trim().toLowerCase();
    if (existing && CANONICAL_UUID.test(existing)) {
      inMemoryVisitorId = existing;
      return existing;
    }
  } catch {
    // Storage can be unavailable in privacy modes. The in-memory fallback is
    // still stable for the lifetime of this tab.
  }

  inMemoryVisitorId = globalThis.crypto.randomUUID().toLowerCase();
  try {
    window.localStorage.setItem(CLIENT_VISITOR_STORAGE_KEY, inMemoryVisitorId);
  } catch {
    // Keep the in-memory identity when persistent storage is unavailable.
  }
  return inMemoryVisitorId;
}

export async function getAuthorizationHeaders(): Promise<Record<string, string>> {
  const token = await authTokenProvider?.();
  const visitorId = getClientVisitorId();
  return {
    ...(visitorId ? { [CLIENT_VISITOR_HEADER]: visitorId } : {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

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

  const init: RequestInit = { method, headers: await getAuthorizationHeaders() };
  if (options.formData) {
    init.body = options.formData;
  } else if (options.body !== undefined) {
    init.headers = { ...init.headers, 'Content-Type': 'application/json' };
    init.body = JSON.stringify(options.body);
  }

  const res = await fetch(url, init);
  const json = await res.json().catch(() => ({}));
  if (!res.ok || json.code !== 0) {
    throw new ApiError(
      Number(json.code ?? res.status),
      json.msg ?? json.detail ?? `Request failed (${res.status})`,
      retryAfterSeconds(res),
    );
  }
  return json.data as T;
}

const get = <T>(path: string, params?: Record<string, string | number | boolean | undefined | null>) =>
  request<T>('GET', path, { params });
const post = <T>(path: string, body?: unknown) => request<T>('POST', path, { body });
const postForm = <T>(path: string, fd: FormData) => request<T>('POST', path, { formData: fd });
const del = <T>(
  path: string,
  params?: Record<string, string | number | boolean | undefined | null>,
) => request<T>('DELETE', path, { params });

// ─── Response Types ───────────────────────────────────────────────────────────

export type Engine = 'legacy' | 'lightrag';
export type LightRAGMode = 'local' | 'global' | 'hybrid' | 'mix' | 'naive';

export const LIGHTRAG_MODES: ReadonlyArray<{ value: LightRAGMode; label: string }> = [
  { value: 'mix', label: 'Mix 综合' },
  { value: 'local', label: 'Local 局部' },
  { value: 'global', label: 'Global 全局' },
  { value: 'hybrid', label: 'Hybrid 混合' },
  { value: 'naive', label: 'Naive 向量' },
];

export interface ApiEngineIndexState {
  status: string;
  job_id?: string | null;
  stage?: string | null;
  progress?: number | StructuredIndexProgress | null;
  error_msg?: string | null;
  error?: string | null;
  stats?: {
    nodes?: number;
    edges?: number;
    pages?: number;
    [key: string]: unknown;
  };
  nodes?: number;
  edges?: number;
  pages?: number;
}

export interface ApiDoc {
  doc_id: string;
  filename: string;
  format: string;
  pages: number | null;
  status: string;
  uploaded_at?: string;
  upload_date?: string;
  job_id?: string | null;
  index_job_status?: string | null;
  index_stage?: string | null;
  progress?: number | StructuredIndexProgress | null;
  file_size?: number;
  size_bytes?: number;
  error_msg?: string | null;
  indexes?: Partial<Record<Engine, ApiEngineIndexState>>;
  available_engines?: Engine[];
}

export interface ApiJobStatus {
  job_id: string;
  doc_id: string;
  status: 'submitted' | 'queued' | 'parsing' | 'extracting' | 'indexing' | 'done' | 'partial' | 'failed' | 'cancelled';
  stage: string;
  progress: number | StructuredIndexProgress; // legacy 0.0-1.0 number or current backend progress
  started_at?: string;
  updated_at?: string;
  error_msg?: string | null;
  error?: string | null;
  engines?: Partial<Record<Engine, ApiEngineIndexState>>;
}

export interface ApiIndexSummary {
  nodes: number;
  edges: number;
  pages: number;
  extractions?: number;
  duration_seconds?: number;
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
  recovered?: boolean;
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
  engine?: Engine;
  description?: string | null;
  pages?: number[];
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
  engine?: Engine;
  description?: string | null;
  weight?: number;
  pages?: number[];
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
  version?: string;
  enabled?: boolean;
  configured?: boolean;
  ready?: boolean;
  worker_status?: string;
  queue_depth?: number;
  active_jobs?: number;
  pending_documents?: number;
  completed_documents?: number;
  reranker?: string;
  detail?: string;
  total?: number;
  done?: number;
  pending?: number;
  failed?: number;
  worker_id?: string;
  last_seen?: string;
  last_updated?: string;
  heartbeat_age_seconds?: number;
  heartbeat_ttl_seconds?: number;
  maintenance_status?: string;
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
    lightrag?: ApiComponentHealth;
    lightrag_worker?: ApiComponentHealth;
    lightrag_graph_database?: ApiComponentHealth;
    lightrag_vector_database?: ApiComponentHealth;
    lightrag_reranker?: ApiComponentHealth;
    lightrag_backfill?: ApiComponentHealth;
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

export interface ApiAccountIdentity {
  authenticated: boolean;
  user_id: string;
  tenant_id: string;
  organization_id?: string | null;
  organization_slug?: string | null;
  role: string;
  permissions: string[];
}

export interface ApiUsageSummary {
  days: number;
  scope: 'user' | 'tenant';
  pricing_configured: boolean;
  currency: 'CNY';
  total: {
    events: number;
    input_tokens: number;
    output_tokens: number;
    cost_cny: number;
  };
  breakdown: Array<{
    operation: string;
    provider: string;
    model: string;
    events: number;
    input_tokens: number;
    output_tokens: number;
    cost_cny: number;
  }>;
}

export interface ApiOpsSummary {
  hours: number;
  totals: { total: number; errors: number; warnings: number; unique_issues: number };
  issues: Array<{
    fingerprint: string;
    source: string;
    event_type: string;
    severity: string;
    message: string;
    occurrences: number;
    last_seen: string;
  }>;
  readiness?: {
    status: 'ready' | 'action_required';
    checks: Record<string, { ready: boolean; message: string; mode?: string; retention_hours?: number }>;
  };
}

export interface ApiToolCall {
  step: number;
  tool_name: string;
  tool_input: string;
  tool_output: string;
}

export interface ApiQueryReference {
  doc_id: string;
  filename: string;
  page: number | null;
  chunk_id: string;
  excerpt: string;
}

export interface ApiCitedEntity {
  id: string;
  name: string;
  type: string;
}

export interface ApiTokenUsage {
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
  model?: string;
  provider?: string;
  [key: string]: unknown;
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
  engine?: Engine;
  retrieval_mode?: LightRAGMode | null;
  references?: ApiQueryReference[];
  cited_entities?: Array<ApiCitedEntity | string>;
  model?: string;
  provider?: string;
  usage?: ApiTokenUsage;
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
  engine?: Engine;
  retrieval_mode?: LightRAGMode | null;
  references?: ApiQueryReference[];
  cited_entities?: Array<ApiCitedEntity | string>;
  model?: string;
  provider?: string;
  usage?: ApiTokenUsage;
}

export interface ApiChatSessionSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  last_question: string;
  last_answer: string;
  engine?: Engine;
  retrieval_mode?: LightRAGMode | null;
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
  engine?: Engine;
  retrieval_mode?: LightRAGMode | null;
  references?: ApiQueryReference[];
  cited_entities?: Array<ApiCitedEntity | string>;
  model?: string;
  provider?: string;
  usage?: ApiTokenUsage;
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
  engine?: Engine;
  retrieval_mode?: LightRAGMode | null;
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
  engine?: Engine;
  retrieval_mode?: LightRAGMode | null;
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
  startIndexing: (docId: string, engine?: Engine) => engine
    ? post<{ job_id: string; doc_id: string; status: string }>(`/index/${docId}/retry`, { engine })
    : post<{ job_id: string; doc_id: string; status: string }>('/index/start', { doc_id: docId }),

  getJobStatus: (jobId: string) => get<ApiJobStatus>(`/index/status/${jobId}`),

  getJobResult: (jobId: string) => get<ApiIndexResult>(`/index/result/${jobId}`),

  cancelJob: (jobId: string) => del<{ job_id: string }>(`/index/jobs/${jobId}`),

  // C: Knowledge Graph
  getNodes: (params?: { page?: number; pageSize?: number; type?: string; docId?: string; layout?: boolean; engine?: Engine }) =>
    get<{ total: number; page: number; page_size: number; items: ApiKGNode[] }>('/kg/nodes', {
      page: params?.page,
      page_size: params?.pageSize ?? 500,
      type: params?.type,
      doc_id: params?.docId,
      layout: params?.layout,
      engine: params?.engine,
    }),

  getEdges: (params?: { page?: number; pageSize?: number; docId?: string; layout?: boolean; engine?: Engine }) =>
    get<{ total: number; raw_total?: number; page: number; page_size: number; items: ApiKGEdge[] }>('/kg/edges', {
      page: params?.page,
      page_size: params?.pageSize ?? 2000,
      doc_id: params?.docId,
      layout: params?.layout,
      engine: params?.engine,
    }),

  getNodeDetail: (nodeId: string, engine: Engine = 'legacy') =>
    get<ApiKGNode>(`/kg/nodes/${nodeId}`, { engine }),

  getNodeNeighbors: (nodeId: string, hops = 1, engine: Engine = 'legacy') =>
    get<{
      center: ApiKGNode;
      hops: number;
      neighbors_by_hop: Record<string, ApiKGNode[]>;
      total_neighbors: number;
    }>(`/kg/nodes/${nodeId}/neighbors`, { hops, engine }),

  getKGStats: (engine: Engine = 'legacy') =>
    get<{ total_nodes: number; total_edges: number; type_distribution: Record<string, number> }>('/kg/stats', { engine }),

  exportKG: (docId?: string, engine: Engine = 'legacy') => get<{ nodes: ApiKGNode[]; edges: ApiKGEdge[] }>('/kg/export', {
    doc_id: docId,
    engine,
  }),

  // D: QA Query
  query: (
    question: string,
    history: { question: string; answer: string }[] = [],
    sessionId?: string | null,
    engine: Engine = 'legacy',
    retrievalMode: LightRAGMode = 'mix',
  ) => {
    const chatHistory = toChatHistory(history);
    return post<ApiQueryResult>('/query', {
      question,
      history: chatHistory,
      session_id: sessionId ?? undefined,
      engine,
      retrieval_mode: engine === 'lightrag' ? retrievalMode : undefined,
    });
  },

  streamQuery: async (
    question: string,
    history: { question: string; answer: string }[] = [],
    sessionId: string | null | undefined,
    onEvent: (event: ApiQueryStreamEvent) => void,
    options: { engine?: Engine; retrievalMode?: LightRAGMode } = {},
  ) => {
    const engine = options.engine ?? 'legacy';
    const res = await fetch(`${BASE}/query/stream`, {
      method: 'POST',
      headers: { ...(await getAuthorizationHeaders()), 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question,
        history: toChatHistory(history),
        session_id: sessionId ?? undefined,
        engine,
        retrieval_mode: engine === 'lightrag' ? (options.retrievalMode ?? 'mix') : undefined,
      }),
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

  createQuerySession: (engine: Engine = 'lightrag', retrievalMode: LightRAGMode = 'mix') =>
    post<ApiChatSession>('/query/sessions', {
      engine,
      retrieval_mode: engine === 'lightrag' ? retrievalMode : undefined,
    }),

  getQuerySessions: (page = 1, pageSize = 50) =>
    get<{ total: number; page: number; page_size: number; items: ApiChatSessionSummary[] }>(
      '/query/sessions', { page, page_size: pageSize }
    ),

  getQuerySession: (sessionId: string) => get<ApiChatSession>(`/query/sessions/${sessionId}`),

  startBatch: (questions: string[], engine: Engine = 'legacy', retrievalMode: LightRAGMode = 'mix') =>
    post<{ batch_id: string; total: number; status: string; created_at: string; engine?: Engine; retrieval_mode?: LightRAGMode | null }>('/query/batch', {
      questions,
      engine,
      retrieval_mode: engine === 'lightrag' ? retrievalMode : undefined,
    }),

  listBatches: (page = 1, pageSize = 20) =>
    get<{ total: number; page: number; page_size: number; items: ApiBatchSummary[] }>(
      '/query/batch', { page, page_size: pageSize }
    ),

  getBatchResult: (batchId: string) => get<ApiBatchResult>(`/query/batch/${batchId}`),

  cancelBatch: (batchId: string) =>
    del<{ batch_id: string; previous_status: string; status: string; cancel_requested: boolean }>(`/query/batch/${batchId}`),

  // E: Account, tenant, usage and operations
  claimVisitorData: () =>
    post<{ claimed: Record<string, number>; tenant_id: string }>('/account/claim-visitor-data'),

  getAccountMe: () => get<ApiAccountIdentity>('/account/me'),

  getAccountUsage: (days = 30, tenantTotal = false) =>
    get<ApiUsageSummary>('/account/usage', { days, tenant_total: tenantTotal }),

  exportAccountData: () => get<Record<string, unknown>>('/account/export'),

  deletePersonalData: (userId: string) =>
    del<{ scope: string; deleted: Record<string, number> }>('/account/data', { confirmation: userId }),

  deleteTenantData: (tenantId: string) =>
    del<{ scope: string; deleted: Record<string, number> }>('/account/tenant-data', { confirmation: tenantId }),

  getOpsSummary: (hours = 24) => get<ApiOpsSummary>('/ops/summary', { hours }),

  // F: Search
  searchEntities: (q: string, type?: string, limit = 15, engine: Engine = 'legacy') =>
    get<ApiSearchResult>('/search/entities', {
      q,
      type: type && type !== '全部类型' ? type : undefined,
      limit,
      engine,
    }),

  searchPath: (fromId: string, toId: string, maxHops = 3, engine: Engine = 'legacy') =>
    get<ApiPathResult>('/search/path', { from: fromId, to: toId, max_hops: maxHops, engine }),

  searchGraph: (q: string, includeNeighbors = false, engine: Engine = 'legacy') =>
    get<ApiGraphSearchResult>('/search/graph', { q, include_neighbors: includeNeighbors, engine }),

  // G: System
  getHealth: () => get<ApiHealthData>('/health'),

  getSystemStats: () => get<ApiStats>('/system/stats'),

  getDemoData: () =>
    get<{ nodes: ApiKGNode[]; edges: ApiKGEdge[]; stats: Record<string, unknown> }>('/system/demo'),
};
