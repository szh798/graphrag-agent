import React, { createContext, useContext, useState, useCallback, useEffect, type ReactNode } from 'react';
import {
  api,
  type ApiDoc,
  type ApiKGNode,
  type ApiKGEdge,
  type ApiChatSessionSummary,
  type ApiIndexResult,
  type ApiQueryReference,
  type Engine,
  type LightRAGMode,
  ApiError,
} from './api';
import { useAuthRuntime } from './auth';
import { normalizeDocumentStatus, type DocumentStatus } from './document-status';
import { indexProgressPercent } from './index-progress';
import { hasActiveDocumentIndex } from './document-index-state';

// ─── Domain Types ─────────────────────────────────────────────────────────────

export interface KGNode {
  id: string;
  name: string;
  type: 'TECHNOLOGY' | 'CONCEPT' | 'PERSON' | 'ORGANIZATION' | 'LOCATION';
  page: number;
  confidence: 'match_exact' | 'match_greater' | 'match_lesser' | 'match_fuzzy';
  degree: number;
  centrality: number;
  doc_id: string;
  description?: string;
  pages?: number[];
  engine?: Engine;
}

export interface KGEdge {
  id: string;
  source: string;
  target: string;
  relation: string;
  weight: number;
  description?: string;
  pages?: number[];
  engine?: Engine;
}

export interface EngineIndexState {
  status: DocumentStatus;
  raw_status: string;
  job_id?: string;
  stage?: string;
  progress?: number;
  error?: string;
  nodes?: number;
  edges?: number;
  pages?: number;
}

export interface Document {
  id: string;
  filename: string;
  format: string;
  pages: number;
  status: DocumentStatus;
  upload_date: string;
  job_id?: string;
  index_stage?: string;
  progress?: number;
  error?: string;
  indexes?: Partial<Record<Engine, EngineIndexState>>;
  available_engines?: Engine[];
  result?: {
    nodes: number;
    edges: number;
    pages: number;
    extractions?: number;
    duration?: number;
    recovered?: boolean;
  };
}

export interface ChatMessage {
  id: string;
  role: 'human' | 'ai';
  content: string;
  timestamp: string;
  toolCalls?: ToolCall[];
  citedNodes?: { id?: string; name: string; type: string }[];
  duration?: number;
  engine?: Engine;
  retrievalMode?: LightRAGMode | null;
  references?: ApiQueryReference[];
}

export interface ToolCall {
  step: number;
  tool: string;
  input: string;
  output: string;
}

export interface HistoryItem {
  id: string;
  question: string;
  answer: string;
  timestamp: string;
  group: '今天' | '昨天' | '更早';
  toolCalls?: ToolCall[];
  citedNodeIds?: string[];
  duration?: number;
}

export interface ChatSession {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  last_question: string;
  last_answer: string;
  group: '今天' | '昨天' | '更早';
  engine: Engine;
  retrievalMode: LightRAGMode | null;
}

export interface HealthStatus {
  mineru: 'ok' | 'error';
  langextract: 'ok' | 'error';
  deepseek: 'ok' | 'error';
  storage: 'ok' | 'error';
}

export interface StatsData {
  kg_nodes: number;
  kg_edges: number;
  documents: number;
  queries: number;
}

// ─── Mappers ──────────────────────────────────────────────────────────────────

function mapApiDoc(d: ApiDoc): Document {
  const uploadedAt = d.upload_date ?? d.uploaded_at ?? new Date().toISOString();
  const indexes = Object.fromEntries(
    Object.entries(d.indexes ?? {}).map(([engine, state]) => [
      engine,
      {
        status: state.status === 'pending' || state.status === 'disabled'
          ? 'uploaded'
          : normalizeDocumentStatus(state.status),
        raw_status: state.status,
        job_id: state.job_id ?? undefined,
        stage: state.stage ?? undefined,
        progress: state.progress == null
          ? undefined
          : indexProgressPercent(state.progress),
        error: state.error ?? state.error_msg ?? undefined,
        nodes: state.nodes ?? state.stats?.nodes,
        edges: state.edges ?? state.stats?.edges,
        pages: state.pages ?? state.stats?.pages,
      } satisfies EngineIndexState,
    ]),
  ) as Partial<Record<Engine, EngineIndexState>>;

  if (!indexes.legacy) {
    indexes.legacy = {
      status: normalizeDocumentStatus(d.status),
      raw_status: d.status,
      job_id: d.job_id ?? undefined,
      error: d.error_msg ?? undefined,
    };
  }

  const availableEngines = d.available_engines?.filter(
    (engine): engine is Engine => engine === 'legacy' || engine === 'lightrag',
  ) ?? (Object.entries(indexes)
    .filter(([, state]) => state?.status === 'indexed')
    .map(([engine]) => engine as Engine));

  return {
    id: d.doc_id,
    filename: d.filename,
    format: d.format,
    pages: d.pages ?? 0,
    status: normalizeDocumentStatus(d.status),
    upload_date: uploadedAt,
    job_id: d.job_id ?? undefined,
    index_stage: d.index_stage ?? undefined,
    progress: d.progress == null ? undefined : indexProgressPercent(d.progress),
    error: d.error_msg ?? undefined,
    indexes,
    available_engines: availableEngines,
  };
}

function mapApiIndexResult(result: ApiIndexResult): NonNullable<Document['result']> {
  return {
    nodes: result.summary?.nodes ?? result.nodes_added ?? result.stats?.nodes ?? 0,
    edges: result.summary?.edges ?? result.edges_added ?? result.stats?.edges ?? 0,
    pages: result.summary?.pages ?? result.pages_processed ?? result.stats?.pages ?? 0,
    extractions: result.summary?.extractions ?? result.extractions_count ?? result.stats?.raw_extractions ?? 0,
    duration: result.summary?.duration_seconds ?? result.duration_seconds ?? result.stats?.elapsed_seconds ?? result.elapsed_seconds ?? 0,
  };
}

export function mapApiNode(n: ApiKGNode): KGNode {
  const pages = n.pages?.length ? n.pages : [n.page ?? 0];
  return {
    id: n.id,
    name: n.name,
    type: n.type as KGNode['type'],
    page: n.page ?? pages[0] ?? 0,
    confidence: (n.confidence || 'match_exact') as KGNode['confidence'],
    degree: n.degree,
    centrality: n.degree_centrality ?? 0,
    doc_id: n.source_doc,
    description: n.description ?? undefined,
    pages,
    engine: n.engine ?? 'legacy',
  };
}

export function mapApiEdge(e: ApiKGEdge): KGEdge {
  return {
    id: e.id,
    source: e.source,
    target: e.target,
    relation: e.relation,
    weight: e.weight ?? 1,
    description: e.description ?? undefined,
    pages: e.pages?.length ? e.pages : [e.page ?? 0],
    engine: e.engine ?? 'legacy',
  };
}

function getHistoryGroup(ts: string): HistoryItem['group'] {
  const diffDays = (Date.now() - new Date(ts).getTime()) / 86_400_000;
  if (diffDays < 1) return '今天';
  if (diffDays < 2) return '昨天';
  return '更早';
}

function mapApiSession(s: ApiChatSessionSummary): ChatSession {
  return {
    id: s.id,
    title: s.title || s.last_question || '新对话',
    created_at: s.created_at,
    updated_at: s.updated_at,
    message_count: s.message_count,
    last_question: s.last_question,
    last_answer: s.last_answer,
    group: getHistoryGroup(s.updated_at),
    engine: s.engine ?? 'legacy',
    retrievalMode: s.engine === 'lightrag' ? (s.retrieval_mode ?? 'mix') : null,
  };
}

// ─── Context ──────────────────────────────────────────────────────────────────

interface AppState {
  sidebarCollapsed: boolean;
  setSidebarCollapsed: (v: boolean) => void;

  nodes: KGNode[];
  edges: KGEdge[];
  kgLoading: boolean;
  graphEngine: Engine;
  setGraphEngine: (engine: Engine) => void;
  refreshKG: (engine?: Engine) => void;

  documents: Document[];
  docsLoading: boolean;
  refreshDocuments: () => Promise<void>;
  setDocuments: React.Dispatch<React.SetStateAction<Document[]>>;

  messages: ChatMessage[];
  setMessages: React.Dispatch<React.SetStateAction<ChatMessage[]>>;

  chatHistory: HistoryItem[];
  refreshHistory: () => Promise<void>;

  chatSessions: ChatSession[];
  refreshSessions: () => Promise<void>;

  health: HealthStatus;
  stats: StatsData;

  suggestedPrompts: string[];

  selectedNode: KGNode | null;
  setSelectedNode: (n: KGNode | null) => void;
  getNeighbors: (nodeId: string) => { nodes: KGNode[]; edges: KGEdge[] };
}

const AppContext = createContext<AppState | null>(null);

const DEFAULT_HEALTH: HealthStatus = { mineru: 'error', langextract: 'error', deepseek: 'error', storage: 'error' };
const DEFAULT_STATS: StatsData = { kg_nodes: 0, kg_edges: 0, documents: 0, queries: 0 };

const SUGGESTED_PROMPTS = [
  '给我一个知识图谱的概览',
  '列出所有 TECHNOLOGY 实体',
  'GraphRAG 与知识图谱有什么关系？',
  '什么是检索增强生成？',
];

const KG_NODES_PAGE_SIZE = 200;
const KG_EDGES_PAGE_SIZE = 5000;

// ─── Provider ─────────────────────────────────────────────────────────────────

export function AppProvider({ children }: { children: ReactNode }) {
  const auth = useAuthRuntime();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [nodes, setNodes] = useState<KGNode[]>([]);
  const [edges, setEdges] = useState<KGEdge[]>([]);
  const [kgLoading, setKgLoading] = useState(true);
  const [graphEngine, setGraphEngineState] = useState<Engine>('legacy');
  const graphEngineRef = React.useRef<Engine>('legacy');
  const kgRequestIdRef = React.useRef(0);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [docsLoading, setDocsLoading] = useState(true);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [chatHistory, setChatHistory] = useState<HistoryItem[]>([]);
  const [chatSessions, setChatSessions] = useState<ChatSession[]>([]);
  const [health, setHealth] = useState<HealthStatus>(DEFAULT_HEALTH);
  const [stats, setStats] = useState<StatsData>(DEFAULT_STATS);
  const [selectedNode, setSelectedNode] = useState<KGNode | null>(null);

  // ── KG data ──────────────────────────────────────────────────────────────

  const setGraphEngine = useCallback((engine: Engine) => {
    if (graphEngineRef.current === engine) return;
    graphEngineRef.current = engine;
    setGraphEngineState(engine);
    setNodes([]);
    setEdges([]);
    setKgLoading(true);
    setSelectedNode(null);
  }, []);

  const refreshKG = useCallback(async (engine = graphEngineRef.current) => {
    const requestId = ++kgRequestIdRef.current;
    try {
      setKgLoading(true);
      const [firstNodes, firstEdges] = await Promise.all([
        api.getNodes({ page: 1, pageSize: KG_NODES_PAGE_SIZE, layout: true, engine }),
        api.getEdges({ page: 1, pageSize: KG_EDGES_PAGE_SIZE, layout: true, engine }),
      ]);

      const nodePages = Math.ceil(firstNodes.total / Math.max(1, firstNodes.page_size));
      const edgePages = Math.ceil(firstEdges.total / Math.max(1, firstEdges.page_size));

      const [restNodePages, restEdgePages] = await Promise.all([
        nodePages > 1
          ? Promise.all(Array.from({ length: nodePages - 1 }, (_, index) =>
              api.getNodes({ page: index + 2, pageSize: KG_NODES_PAGE_SIZE, layout: true, engine })
            ))
          : Promise.resolve([]),
        edgePages > 1
          ? Promise.all(Array.from({ length: edgePages - 1 }, (_, index) =>
              api.getEdges({ page: index + 2, pageSize: KG_EDGES_PAGE_SIZE, layout: true, engine })
            ))
          : Promise.resolve([]),
      ]);

      const allNodeItems = [firstNodes, ...restNodePages].flatMap(page => page.items);
      const allEdgeItems = [firstEdges, ...restEdgePages].flatMap(page => page.items);
      if (requestId !== kgRequestIdRef.current || engine !== graphEngineRef.current) return;
      setNodes(allNodeItems.map(mapApiNode));
      setEdges(allEdgeItems.map(mapApiEdge));
    } catch (err) {
      if (requestId !== kgRequestIdRef.current || engine !== graphEngineRef.current) return;
      // code=3002 means empty KG — that's fine
      if (!(err instanceof ApiError && err.code === 3002)) {
        console.error('KG load error:', err);
      }
      setNodes([]);
      setEdges([]);
    } finally {
      if (requestId === kgRequestIdRef.current && engine === graphEngineRef.current) {
        setKgLoading(false);
      }
    }
  }, []);

  // ── Documents ─────────────────────────────────────────────────────────────

  const refreshDocuments = useCallback(async () => {
    try {
      const res = await api.listDocuments(1, 100);
      setDocuments(res.items.map(mapApiDoc));
    } catch (err) {
      console.error('Docs load error:', err);
    } finally {
      setDocsLoading(false);
    }
  }, []);

  // ── Query history ─────────────────────────────────────────────────────────

  const refreshHistory = useCallback(async () => {
    try {
      const res = await api.getQueryHistory(1, 50);
      setChatHistory(
        res.items.map(item => ({
          id: item.id,
          question: item.question,
          answer: item.answer,
          timestamp: item.timestamp,
          group: getHistoryGroup(item.timestamp),
          toolCalls: item.tool_calls.map((tc, i) => ({
            step: tc.step ?? i + 1,
            tool: tc.tool_name,
            input: tc.tool_input,
            output: tc.tool_output,
          })),
          citedNodeIds: item.cited_nodes,
          duration: item.duration_seconds,
        }))
      );
    } catch (err) {
      console.error('History load error:', err);
      setChatHistory([]);
    }
  }, []);

  const refreshSessions = useCallback(async () => {
    try {
      const res = await api.getQuerySessions(1, 50);
      setChatSessions(res.items.map(mapApiSession));
    } catch (err) {
      console.error('Session load error:', err);
      setChatSessions([]);
    }
  }, []);

  // ── Health & Stats ────────────────────────────────────────────────────────

  const refreshHealthStats = useCallback(async () => {
    try {
      const [h, s] = await Promise.all([api.getHealth(), api.getSystemStats()]);
      setHealth({
        mineru: h.components.mineru_venv.status === 'ok' ? 'ok' : 'error',
        langextract: h.components.langextract_venv.status === 'ok' ? 'ok' : 'error',
        deepseek: (h.components.llm_api ?? h.components.deepseek_api)?.status === 'ok' ? 'ok' : 'error',
        storage: (h.components.blob_storage ?? h.components.storage)?.status === 'ok' ? 'ok' : 'error',
      });
      setStats({
        kg_nodes: s.total_nodes,
        kg_edges: s.total_edges,
        documents: s.total_documents,
        queries: s.total_queries,
      });
    } catch (err) {
      console.error('Health/stats error:', err);
    }
  }, []);

  // ── Initial load ──────────────────────────────────────────────────────────

  useEffect(() => {
    setKgLoading(true);
    setDocsLoading(true);
    setNodes([]);
    setEdges([]);
    setDocuments([]);
    setMessages([]);
    setChatHistory([]);
    setChatSessions([]);
    setSelectedNode(null);

    if (!auth.apiReady) return;

    void Promise.all([
      refreshKG(),
      refreshDocuments(),
      refreshHistory(),
      refreshSessions(),
      refreshHealthStats(),
    ]);
  }, [
    auth.apiReady,
    auth.identityKey,
    refreshDocuments,
    refreshHealthStats,
    refreshHistory,
    refreshKG,
    refreshSessions,
  ]);

  // ── Revalidate health/stats without keeping background tabs hot ───────────

  useEffect(() => {
    const refreshWhenVisible = () => {
      if (document.visibilityState === 'visible') refreshHealthStats();
    };
    const id = window.setInterval(refreshWhenVisible, 60_000);
    document.addEventListener('visibilitychange', refreshWhenVisible);
    window.addEventListener('focus', refreshWhenVisible);
    return () => {
      window.clearInterval(id);
      document.removeEventListener('visibilitychange', refreshWhenVisible);
      window.removeEventListener('focus', refreshWhenVisible);
    };
  }, [refreshHealthStats]);

  // ── Poll active indexing jobs every 3 s ───────────────────────────────────

  useEffect(() => {
    const indexingDocs = documents.filter(d => d.job_id && hasActiveDocumentIndex(d));
    if (indexingDocs.length === 0) return;

    const id = setInterval(async () => {
      const updates: (Partial<Document> & { id: string })[] = [];

      for (const doc of indexingDocs) {
        if (!doc.job_id) continue;
        try {
          const status = await api.getJobStatus(doc.job_id);

          if (status.status === 'done' || status.status === 'partial') {
            try {
              const result = await api.getJobResult(doc.job_id);
              const legacyFailed = status.engines?.legacy?.status === 'failed';
              updates.push({
                id: doc.id,
                status: legacyFailed ? 'failed' : 'indexed',
                progress: 100,
                error: status.status === 'partial' ? (status.error ?? '其中一个引擎索引失败') : undefined,
                result: {
                  ...mapApiIndexResult(result),
                },
              });
            } catch {
              updates.push({
                id: doc.id,
                status: status.engines?.legacy?.status === 'failed' ? 'failed' : 'indexed',
                progress: 100,
                error: status.status === 'partial' ? (status.error ?? '其中一个引擎索引失败') : undefined,
              });
            }
          } else if (status.status === 'failed') {
            updates.push({ id: doc.id, status: 'failed', error: status.error_msg ?? 'Indexing failed' });
          } else if (status.status === 'cancelled') {
            updates.push({ id: doc.id, status: 'uploaded', progress: undefined });
          } else {
            // still running
            updates.push({
              id: doc.id,
              status: 'indexing',
              index_stage: status.stage,
              progress: indexProgressPercent(status.progress),
            });
          }
        } catch {
          // job not found – doc may have been cleaned up
        }
      }

      if (updates.length === 0) return;

      setDocuments(prev =>
        prev.map(d => {
          const u = updates.find(u => u.id === d.id);
          return u ? { ...d, ...u } : d;
        })
      );

      if (updates.some(u => u.status === 'indexed' || u.status === 'failed')) {
        refreshDocuments();
        refreshKG();
        refreshHealthStats();
        refreshHistory();
        refreshSessions();
      }
    }, 3000);

    return () => clearInterval(id);
  }, [documents, refreshDocuments, refreshKG, refreshHealthStats, refreshHistory, refreshSessions]);

  // LightRAG can continue after the compatibility `status` has reached the
  // classic engine's terminal state. Refresh the document summaries until all
  // advertised child indexes have settled.
  useEffect(() => {
    const hasBackgroundEngine = documents.some(doc =>
      Object.values(doc.indexes ?? {}).some(index => index?.status === 'indexing')
      && doc.status !== 'indexing'
    );
    if (!hasBackgroundEngine) return;

    const id = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refreshDocuments();
    }, 3_000);
    return () => window.clearInterval(id);
  }, [documents, refreshDocuments]);

  // ── Neighbor helper ───────────────────────────────────────────────────────

  const getNeighbors = useCallback(
    (nodeId: string) => {
      const connectedEdges = edges.filter(e => e.source === nodeId || e.target === nodeId);
      const neighborIds = new Set<string>();
      connectedEdges.forEach(e => {
        if (e.source !== nodeId) neighborIds.add(e.source);
        if (e.target !== nodeId) neighborIds.add(e.target);
      });
      return {
        nodes: nodes.filter(n => neighborIds.has(n.id)),
        edges: connectedEdges,
      };
    },
    [nodes, edges]
  );

  return (
    <AppContext.Provider
      value={{
        sidebarCollapsed, setSidebarCollapsed,
        nodes, edges, kgLoading, graphEngine, setGraphEngine, refreshKG,
        documents, docsLoading, refreshDocuments, setDocuments,
        messages, setMessages,
        chatHistory, refreshHistory,
        chatSessions, refreshSessions,
        health, stats,
        suggestedPrompts: SUGGESTED_PROMPTS,
        selectedNode, setSelectedNode,
        getNeighbors,
      }}
    >
      {children}
    </AppContext.Provider>
  );
}

export function useAppState() {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error('useAppState must be used within AppProvider');
  return ctx;
}
