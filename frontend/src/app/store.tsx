import React, { createContext, useContext, useState, useCallback, useEffect, type ReactNode } from 'react';
import { api, type ApiDoc, type ApiKGNode, type ApiKGEdge, type ApiChatSessionSummary, type ApiIndexResult, ApiError } from './api';

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
}

export interface KGEdge {
  id: string;
  source: string;
  target: string;
  relation: string;
  weight: number;
}

export interface Document {
  id: string;
  filename: string;
  format: string;
  pages: number;
  status: 'uploaded' | 'indexing' | 'indexed' | 'failed';
  upload_date: string;
  job_id?: string;
  index_stage?: string;
  progress?: number;
  error?: string;
  result?: {
    nodes: number;
    edges: number;
    pages: number;
    extractions: number;
    duration: number;
  };
}

export interface ChatMessage {
  id: string;
  role: 'human' | 'ai';
  content: string;
  timestamp: string;
  toolCalls?: ToolCall[];
  citedNodes?: { id: string; name: string; type: string }[];
  duration?: number;
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
  return {
    id: d.doc_id,
    filename: d.filename,
    format: d.format,
    pages: d.pages ?? 0,
    status: d.status,
    upload_date: uploadedAt,
    job_id: d.job_id ?? undefined,
    error: d.error_msg ?? undefined,
  };
}

function mapApiIndexResult(result: ApiIndexResult): Document['result'] {
  return {
    nodes: result.summary?.nodes ?? result.nodes_added ?? result.stats?.nodes ?? 0,
    edges: result.summary?.edges ?? result.edges_added ?? result.stats?.edges ?? 0,
    pages: result.summary?.pages ?? result.pages_processed ?? result.stats?.pages ?? 0,
    extractions: result.summary?.extractions ?? result.extractions_count ?? result.stats?.raw_extractions ?? 0,
    duration: result.summary?.duration_seconds ?? result.duration_seconds ?? result.stats?.elapsed_seconds ?? result.elapsed_seconds ?? 0,
  };
}

export function mapApiNode(n: ApiKGNode): KGNode {
  return {
    id: n.id,
    name: n.name,
    type: n.type as KGNode['type'],
    page: n.page,
    confidence: n.confidence as KGNode['confidence'],
    degree: n.degree,
    centrality: n.degree_centrality ?? 0,
    doc_id: n.source_doc,
  };
}

export function mapApiEdge(e: ApiKGEdge): KGEdge {
  return {
    id: e.id,
    source: e.source,
    target: e.target,
    relation: e.relation,
    weight: 1,
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
  };
}

// ─── Context ──────────────────────────────────────────────────────────────────

interface AppState {
  sidebarCollapsed: boolean;
  setSidebarCollapsed: (v: boolean) => void;

  nodes: KGNode[];
  edges: KGEdge[];
  kgLoading: boolean;
  refreshKG: () => void;

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
const KG_EDGES_PAGE_SIZE = 500;

// ─── Provider ─────────────────────────────────────────────────────────────────

export function AppProvider({ children }: { children: ReactNode }) {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [nodes, setNodes] = useState<KGNode[]>([]);
  const [edges, setEdges] = useState<KGEdge[]>([]);
  const [kgLoading, setKgLoading] = useState(true);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [docsLoading, setDocsLoading] = useState(true);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [chatHistory, setChatHistory] = useState<HistoryItem[]>([]);
  const [chatSessions, setChatSessions] = useState<ChatSession[]>([]);
  const [health, setHealth] = useState<HealthStatus>(DEFAULT_HEALTH);
  const [stats, setStats] = useState<StatsData>(DEFAULT_STATS);
  const [selectedNode, setSelectedNode] = useState<KGNode | null>(null);

  // ── KG data ──────────────────────────────────────────────────────────────

  const refreshKG = useCallback(async () => {
    try {
      setKgLoading(true);
      const [firstNodes, firstEdges] = await Promise.all([
        api.getNodes({ page: 1, pageSize: KG_NODES_PAGE_SIZE }),
        api.getEdges({ page: 1, pageSize: KG_EDGES_PAGE_SIZE }),
      ]);

      const nodePages = Math.ceil(firstNodes.total / Math.max(1, firstNodes.page_size));
      const edgePages = Math.ceil(firstEdges.total / Math.max(1, firstEdges.page_size));

      const [restNodePages, restEdgePages] = await Promise.all([
        nodePages > 1
          ? Promise.all(Array.from({ length: nodePages - 1 }, (_, index) =>
              api.getNodes({ page: index + 2, pageSize: KG_NODES_PAGE_SIZE })
            ))
          : Promise.resolve([]),
        edgePages > 1
          ? Promise.all(Array.from({ length: edgePages - 1 }, (_, index) =>
              api.getEdges({ page: index + 2, pageSize: KG_EDGES_PAGE_SIZE })
            ))
          : Promise.resolve([]),
      ]);

      const allNodeItems = [firstNodes, ...restNodePages].flatMap(page => page.items);
      const allEdgeItems = [firstEdges, ...restEdgePages].flatMap(page => page.items);
      setNodes(allNodeItems.map(mapApiNode));
      setEdges(allEdgeItems.map(mapApiEdge));
    } catch (err) {
      // code=3002 means empty KG — that's fine
      if (!(err instanceof ApiError && err.code === 3002)) {
        console.error('KG load error:', err);
      }
      setNodes([]);
      setEdges([]);
    } finally {
      setKgLoading(false);
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
        storage: [
          h.components.storage,
          h.components.graph_database,
          h.components.app_database,
          h.components.blob_storage,
          h.components.task_queue,
        ].every(component => !component || component.status === 'ok') ? 'ok' : 'error',
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
    refreshKG();
    refreshDocuments();
    refreshHistory();
    refreshSessions();
    refreshHealthStats();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Poll health/stats every 10 s ──────────────────────────────────────────

  useEffect(() => {
    const id = setInterval(refreshHealthStats, 10_000);
    return () => clearInterval(id);
  }, [refreshHealthStats]);

  // ── Poll active indexing jobs every 3 s ───────────────────────────────────

  useEffect(() => {
    const indexingDocs = documents.filter(d => d.status === 'indexing' && d.job_id);
    if (indexingDocs.length === 0) return;

    const id = setInterval(async () => {
      const updates: (Partial<Document> & { id: string })[] = [];

      for (const doc of indexingDocs) {
        if (!doc.job_id) continue;
        try {
          const status = await api.getJobStatus(doc.job_id);

          if (status.status === 'done') {
            try {
              const result = await api.getJobResult(doc.job_id);
              updates.push({
                id: doc.id,
                status: 'indexed',
                progress: 100,
                result: {
                  ...mapApiIndexResult(result),
                },
              });
            } catch {
              updates.push({ id: doc.id, status: 'indexed', progress: 100 });
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
              progress: Math.round(status.progress * 100),
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

      if (updates.some(u => u.status === 'indexed')) {
        refreshKG();
        refreshHealthStats();
        refreshHistory();
        refreshSessions();
      }
    }, 3000);

    return () => clearInterval(id);
  }, [documents, refreshKG, refreshHealthStats, refreshHistory, refreshSessions]);

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
        nodes, edges, kgLoading, refreshKG,
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
