import React, { useState, useRef, useEffect } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { Send, Plus, ChevronRight, Clock, ExternalLink, Info, X, Download, RefreshCw, Ban } from 'lucide-react';
import { toast } from 'sonner';
import { useAppState, type ChatMessage, type ChatSession, type ToolCall } from '../../store';
import { api, ApiError, type ApiBatchResult, type ApiBatchSummary, type ApiToolCall } from '../../api';
import { TYPE_COLORS } from '../../mock-data';

export function QAChat() {
  const { messages, setMessages, chatSessions, suggestedPrompts, nodes, refreshHistory, refreshSessions } = useAppState();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [input, setInput] = useState('');
  const [isThinking, setIsThinking] = useState(false);
  const [streamStatus, setStreamStatus] = useState<string | null>(null);
  const [streamingMessageId, setStreamingMessageId] = useState<string | null>(null);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [showBatchPanel, setShowBatchPanel] = useState(false);
  const [batchInput, setBatchInput] = useState('');
  const [batchRunning, setBatchRunning] = useState(false);
  const [batchResult, setBatchResult] = useState<ApiBatchResult | null>(null);
  const [batchHistory, setBatchHistory] = useState<ApiBatchSummary[]>([]);
  const [batchHistoryLoading, setBatchHistoryLoading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const batchPollRef = useRef<number | null>(null);

  useEffect(() => {
    const q = searchParams.get('q');
    if (q) {
      setInput(q);
      inputRef.current?.focus();
    }
  }, [searchParams]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isThinking, streamStatus]);

  useEffect(() => () => {
    if (batchPollRef.current) window.clearTimeout(batchPollRef.current);
  }, []);

  useEffect(() => {
    if (showBatchPanel) loadBatchHistory();
  }, [showBatchPanel]);

  // Build cited node objects from node IDs using local KG
  function resolveCitedNodes(ids: string[]) {
    return ids
      .map(id => {
        const n = nodes.find(n => n.id === id);
        return n ? { id: n.id, name: n.name, type: n.type } : null;
      })
      .filter(Boolean) as { id: string; name: string; type: string }[];
  }

  function mapSessionMessage(msg: {
    id: string;
    role: 'human' | 'ai';
    content: string;
    timestamp: string;
    tool_calls?: Array<{ step?: number; tool_name: string; tool_input: string; tool_output: string }>;
    cited_nodes?: string[];
    duration_seconds?: number;
  }): ChatMessage {
    return {
      id: msg.id,
      role: msg.role,
      content: msg.content,
      timestamp: msg.timestamp,
      toolCalls: (msg.tool_calls ?? []).map(mapApiToolCall),
      citedNodes: resolveCitedNodes(msg.cited_nodes ?? []),
      duration: msg.duration_seconds,
    };
  }

  function mapApiToolCall(tc: ApiToolCall | { step?: number; tool_name: string; tool_input: string; tool_output: string }, i: number): ToolCall {
    return {
      step: tc.step ?? i + 1,
      tool: tc.tool_name,
      input: tc.tool_input,
      output: tc.tool_output,
    };
  }

  const handleSend = async () => {
    if (!input.trim() || isThinking) return;
    const question = input.trim();
    setInput('');
    setIsThinking(true);
    setStreamStatus('正在分析问题...');

    const userMsg: ChatMessage = {
      id: `m${Date.now()}`,
      role: 'human',
      content: question,
      timestamp: new Date().toISOString(),
    };

    const aiMessageId = `stream${Date.now() + 1}`;
    const pendingAiMsg: ChatMessage = {
      id: aiMessageId,
      role: 'ai',
      content: '',
      timestamp: new Date().toISOString(),
      toolCalls: [],
    };
    setStreamingMessageId(aiMessageId);
    setMessages(prev => [...prev, userMsg, pendingAiMsg]);

    try {
      await api.streamQuery(question, [], activeSessionId, evt => {
        if (evt.event === 'status') {
          setStreamStatus(evt.data.message);
          return;
        }

        if (evt.event === 'tool_call') {
          setMessages(prev => prev.map(msg => {
            if (msg.id !== aiMessageId) return msg;
            const toolCalls = msg.toolCalls ?? [];
            return { ...msg, toolCalls: [...toolCalls, mapApiToolCall(evt.data, toolCalls.length)] };
          }));
          return;
        }

        if (evt.event === 'answer_delta') {
          setMessages(prev => prev.map(msg => (
            msg.id === aiMessageId ? { ...msg, content: msg.content + evt.data.text } : msg
          )));
          return;
        }

        if (evt.event === 'done') {
          const result = evt.data;
          setMessages(prev => prev.map(msg => (
            msg.id === aiMessageId
              ? {
                  ...msg,
                  id: result.id ?? msg.id,
                  content: result.answer || msg.content,
                  timestamp: result.timestamp ?? msg.timestamp,
                  toolCalls: result.tool_calls.map(mapApiToolCall),
                  citedNodes: resolveCitedNodes(result.cited_nodes ?? []),
                  duration: result.duration_seconds,
                }
              : msg
          )));
          if (result.session_id) setActiveSessionId(result.session_id);
          setStreamStatus(null);
          refreshHistory();
          refreshSessions();
        }
      });
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : '问答服务异常';
      toast.error(msg);
      setMessages(prev => prev.map(item => item.id === aiMessageId ? {
        ...item,
        content: `⚠️ 请求失败：${msg}\n\n请确认：\n1. 在线问答服务可用\n2. 公开演示已有可查询的知识图谱\n3. LLM 服务已配置`,
        timestamp: new Date().toISOString(),
        toolCalls: undefined,
      } : item));
    } finally {
      setIsThinking(false);
      setStreamStatus(null);
      setStreamingMessageId(null);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleNewChat = () => {
    setMessages([]);
    setInput('');
    setActiveSessionId(null);
    setStreamStatus(null);
    setStreamingMessageId(null);
  };

  const clearBatchPoll = () => {
    if (batchPollRef.current) {
      window.clearTimeout(batchPollRef.current);
      batchPollRef.current = null;
    }
  };

  const loadBatchHistory = async () => {
    try {
      setBatchHistoryLoading(true);
      const result = await api.listBatches(1, 20);
      setBatchHistory(result.items);
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : '批量任务列表加载失败';
      toast.error(msg);
    } finally {
      setBatchHistoryLoading(false);
    }
  };

  const pollBatch = async (batchId: string): Promise<{
    detail: ApiBatchResult | null;
    terminal: boolean;
    retryAfterMs?: number;
  }> => {
    try {
      const detail = await api.getBatchResult(batchId);
      setBatchResult(detail);
      if (detail.status === 'done' || detail.status === 'cancelled') {
        clearBatchPoll();
        setBatchRunning(false);
        loadBatchHistory();
        return { detail, terminal: true };
      }
      return { detail, terminal: false };
    } catch (err) {
      if (err instanceof ApiError && (err.code === 409 || err.code === 429)) {
        const retryAfterMs = Math.max(2000, (err.retryAfterSeconds ?? 2) * 1000);
        if (err.code === 429) {
          toast.warning(err.message, { id: 'batch-poll-rate-limit' });
        }
        return { detail: null, terminal: false, retryAfterMs };
      }
      clearBatchPoll();
      setBatchRunning(false);
      const msg = err instanceof ApiError ? err.message : '批量任务查询失败';
      toast.error(msg);
      return { detail: null, terminal: true };
    }
  };

  const scheduleBatchPoll = (batchId: string, delayMs = 2000) => {
    clearBatchPoll();
    batchPollRef.current = window.setTimeout(async () => {
      batchPollRef.current = null;
      const outcome = await pollBatch(batchId);
      if (!outcome.terminal) {
        scheduleBatchPoll(batchId, outcome.retryAfterMs ?? 2000);
      }
    }, delayMs);
  };

  const handleStartBatch = async () => {
    const questions = batchInput
      .split('\n')
      .map(q => q.trim())
      .filter(Boolean);

    if (questions.length === 0) {
      toast.warning('请先输入至少一个问题');
      return;
    }
    if (questions.length > 20) {
      toast.warning('一次最多提交 20 个问题');
      return;
    }

    try {
      clearBatchPoll();
      setBatchRunning(true);
      const started = await api.startBatch(questions);
      setBatchResult({
        batch_id: started.batch_id,
        total: started.total,
        completed: 0,
        failed: 0,
        status: started.status as ApiBatchResult['status'],
        created_at: started.created_at,
        results: [],
      });
      const firstPoll = await pollBatch(started.batch_id);
      loadBatchHistory();
      if (!firstPoll.terminal) {
        scheduleBatchPoll(started.batch_id, firstPoll.retryAfterMs ?? 2000);
      }
    } catch (err) {
      setBatchRunning(false);
      const msg = err instanceof ApiError ? err.message : '批量任务提交失败';
      toast.error(msg);
    }
  };

  const handleLoadBatch = async (batchId: string) => {
    try {
      clearBatchPoll();
      setBatchRunning(true);
      const outcome = await pollBatch(batchId);
      const running = !outcome.terminal && (
        !outcome.detail ||
        outcome.detail.status === 'submitted' ||
        outcome.detail.status === 'running'
      );
      setBatchRunning(running);
      if (running) {
        scheduleBatchPoll(batchId, outcome.retryAfterMs ?? 2000);
      }
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : '批量任务加载失败';
      toast.error(msg);
    }
  };

  const handleCancelBatch = async () => {
    if (!batchResult) return;
    try {
      await api.cancelBatch(batchResult.batch_id);
      clearBatchPoll();
      setBatchRunning(false);
      const outcome = await pollBatch(batchResult.batch_id);
      if (!outcome.terminal) {
        scheduleBatchPoll(batchResult.batch_id, outcome.retryAfterMs ?? 2000);
      }
      await loadBatchHistory();
      toast.success('批量任务已取消');
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : '批量任务取消失败';
      toast.error(msg);
    }
  };

  const downloadText = (text: string, filename: string, type: string) => {
    const url = URL.createObjectURL(new Blob([text], { type }));
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  };

  const escapeCsvCell = (value: unknown) => {
    const text = String(value ?? '');
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };

  const handleExportBatch = (format: 'json' | 'csv') => {
    if (!batchResult) return;
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    if (format === 'json') {
      downloadText(
        JSON.stringify(batchResult, null, 2),
        `graphrag-batch-${batchResult.batch_id}-${stamp}.json`,
        'application/json;charset=utf-8'
      );
      return;
    }

    const rows = [
      ['index', 'question', 'answer', 'error', 'duration_seconds'],
      ...batchResult.results.map((item, index) => [
        index + 1,
        item.question,
        item.answer ?? '',
        item.error ?? '',
        item.duration_seconds ?? '',
      ]),
    ];
    downloadText(
      rows.map(row => row.map(escapeCsvCell).join(',')).join('\n'),
      `graphrag-batch-${batchResult.batch_id}-${stamp}.csv`,
      'text/csv;charset=utf-8'
    );
  };

  const handleLoadSession = async (session: ChatSession) => {
    try {
      setActiveSessionId(session.id);
      setStreamStatus(null);
      setStreamingMessageId(null);
      const detail = await api.getQuerySession(session.id);
      setMessages(detail.messages.map(mapSessionMessage));
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : '会话加载失败';
      toast.error(msg);
    }
  };

  const groupedSessions = {
    '今天': chatSessions.filter(s => s.group === '今天'),
    '昨天': chatSessions.filter(s => s.group === '昨天'),
    '更早': chatSessions.filter(s => s.group === '更早'),
  };
  const isBatchActive = batchResult?.status === 'submitted' || batchResult?.status === 'running';
  const batchStatus = batchResult ? getBatchStatusMeta(batchResult.status) : null;

  return (
    <div className="flex h-full" style={{ background: 'var(--bg-base)' }}>
      {/* History Sidebar */}
      <div
        className="flex flex-col"
        style={{ width: 240, background: 'var(--bg-s1)', borderRight: '1px solid var(--border-main)', flexShrink: 0 }}
      >
        <div className="p-3">
          <button
            onClick={handleNewChat}
            className="flex items-center gap-2 w-full px-3 py-2 rounded-md cursor-pointer"
            style={{ background: 'var(--bg-s2)', border: '1px solid var(--border-main)', color: 'var(--text-2)', fontSize: 13 }}
          >
            <Plus size={14} /> 新对话
          </button>
        </div>

        {/* 历史会话管理说明 */}
        <div className="mx-3 mb-2 px-2 py-1.5 rounded-md flex items-start gap-1.5" style={{ background: 'rgba(88,166,255,0.08)', border: '1px solid rgba(88,166,255,0.2)' }}>
          <Info size={11} style={{ color: 'var(--blue)', flexShrink: 0, marginTop: 1 }} />
          <span style={{ fontSize: 10, color: 'var(--text-4)', lineHeight: 1.4 }}>
            点击会话继续多轮对话；发送后自动保存上下文
          </span>
        </div>

        <div className="flex-1 overflow-y-auto px-2">
          {Object.entries(groupedSessions).map(([group, items]) => items.length > 0 && (
            <div key={group} className="mb-3">
              <div className="px-2 py-1" style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-4)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
                {group}
              </div>
              {items.map(session => (
                <button
                  key={session.id}
                  onClick={() => handleLoadSession(session)}
                  className="w-full text-left px-2 py-2 rounded cursor-pointer block"
                  style={{
                    background: activeSessionId === session.id ? 'var(--bg-s2)' : 'transparent',
                    color: activeSessionId === session.id ? 'var(--text-1)' : 'var(--text-3)',
                    fontSize: 12, border: 'none',
                  }}
                >
                  <span className="truncate block">
                    {session.title.length > 28 ? session.title.slice(0, 28) + '...' : session.title}
                  </span>
                  <span className="block mt-0.5" style={{ color: 'var(--text-4)', fontSize: 10 }}>
                    {Math.max(0, Math.floor(session.message_count / 2))} 轮
                  </span>
                </button>
              ))}
            </div>
          ))}
          {chatSessions.length === 0 && (
            <div className="px-2 py-4 text-center" style={{ color: 'var(--text-4)', fontSize: 12 }}>暂无会话</div>
          )}
        </div>
      </div>

      {/* Chat Area */}
      <div className="flex-1 flex flex-col">
        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-6">
          {messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full gap-4">
              <div style={{ fontSize: 32 }}>
                <span style={{ color: 'var(--blue)' }}>GraphRAG</span>{' '}
                <span style={{ color: 'var(--text-3)' }}>Studio</span>
              </div>
              <p style={{ color: 'var(--text-3)', fontSize: 14, textAlign: 'center', maxWidth: 500 }}>
                向知识图谱提问。我将使用多步推理从已索引的文档中为您找到准确答案。
              </p>
              <div className="grid grid-cols-2 gap-3 mt-4" style={{ maxWidth: 600, width: '100%' }}>
                {suggestedPrompts.map((p, i) => (
                  <button
                    key={i}
                    onClick={() => setInput(p)}
                    className="text-left p-3 rounded-lg cursor-pointer"
                    style={{ background: 'var(--bg-s1)', border: '1px solid var(--border-main)', color: 'var(--text-2)', fontSize: 13 }}
                  >
                    {p}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="flex flex-col gap-4 max-w-3xl mx-auto">
              {messages.map(msg => (
                <div key={msg.id}>
                  {msg.role === 'human' ? (
                    <div className="flex justify-end">
                      <div
                        className="rounded-lg px-4 py-3"
                        style={{ background: 'rgba(88,166,255,0.15)', color: 'var(--text-1)', fontSize: 14, maxWidth: '80%', lineHeight: 1.6 }}
                      >
                        {msg.content}
                      </div>
                    </div>
                  ) : (
                    <div className="flex justify-start">
                      <div
                        className="rounded-lg px-4 py-3"
                        style={{ background: 'var(--bg-s1)', border: '1px solid var(--border-main)', color: 'var(--text-2)', fontSize: 14, maxWidth: '90%', lineHeight: 1.6 }}
                      >
                        {msg.id === streamingMessageId && (
                          <StreamTrace status={streamStatus ?? '正在准备回答...'} />
                        )}

                        {msg.content ? (
                          <SafeMarkdown text={msg.content} />
                        ) : msg.id === streamingMessageId ? (
                          <InlineThinkingDots />
                        ) : null}

                        {msg.toolCalls && msg.toolCalls.length > 0 && (
                          <ToolCallPanel toolCalls={msg.toolCalls} />
                        )}

                        {msg.citedNodes && msg.citedNodes.length > 0 && (
                          <div className="flex flex-wrap gap-2 mt-3 pt-3" style={{ borderTop: '1px solid var(--border-muted)' }}>
                            {msg.citedNodes.map(cn => (
                              <button
                                key={cn.id}
                                onClick={() => navigate(`/graph?node=${cn.id}`)}
                                className="flex items-center gap-1.5 px-2 py-1 rounded-full cursor-pointer"
                                style={{
                                  background: `${TYPE_COLORS[cn.type] ?? '#8b949e'}15`,
                                  border: `1px solid ${TYPE_COLORS[cn.type] ?? '#8b949e'}40`,
                                  color: TYPE_COLORS[cn.type] ?? '#8b949e',
                                  fontSize: 11, fontWeight: 500,
                                }}
                              >
                                <span className="inline-block w-1.5 h-1.5 rounded-full" style={{ background: TYPE_COLORS[cn.type] ?? '#8b949e' }} />
                                {cn.name}
                                <ExternalLink size={9} />
                              </button>
                            ))}
                          </div>
                        )}

                        {msg.duration !== undefined && (
                          <div className="flex items-center gap-1 mt-2" style={{ color: 'var(--text-4)', fontSize: 11 }}>
                            <Clock size={10} /> {msg.duration.toFixed(1)}s
                          </div>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              ))}

              <div ref={messagesEndRef} />
            </div>
          )}
        </div>

        {/* Input Area */}
        <div className="p-4" style={{ borderTop: '1px solid var(--border-main)', background: 'var(--bg-s1)' }}>
          {showBatchPanel && (
            <div
              className="max-w-3xl mx-auto mb-3 rounded-lg overflow-hidden"
              style={{ background: 'var(--bg-s2)', border: '1px solid var(--border-main)' }}
            >
              <div className="flex items-center justify-between px-3 py-2" style={{ borderBottom: '1px solid var(--border-muted)' }}>
                <div>
                  <div style={{ color: 'var(--text-1)', fontSize: 13, fontWeight: 600 }}>批量问答管理</div>
                  <div style={{ color: 'var(--text-4)', fontSize: 11 }}>每行一个问题，最多 20 条；提交后后台逐条回答</div>
                </div>
                <div className="flex items-center gap-1.5">
                  <button
                    onClick={loadBatchHistory}
                    disabled={batchHistoryLoading}
                    className="p-1 rounded cursor-pointer"
                    title="刷新批次历史"
                    style={{ background: 'transparent', border: 'none', color: 'var(--text-4)', opacity: batchHistoryLoading ? 0.5 : 1 }}
                  >
                    <RefreshCw size={14} />
                  </button>
                  <button
                    onClick={() => setShowBatchPanel(false)}
                    className="p-1 rounded cursor-pointer"
                    title="关闭"
                    style={{ background: 'transparent', border: 'none', color: 'var(--text-4)' }}
                  >
                    <X size={14} />
                  </button>
                </div>
              </div>

              <div className="p-3">
                <textarea
                  value={batchInput}
                  onChange={e => setBatchInput(e.target.value)}
                  disabled={batchRunning}
                  rows={4}
                  className="w-full resize-none rounded-md px-3 py-2 outline-none"
                  placeholder={'给我一个知识图谱的概览\\n列出所有 TECHNOLOGY 实体\\nPython 和 C++ 有什么关系？'}
                  style={{
                    background: 'var(--bg-base)',
                    border: '1px solid var(--border-main)',
                    color: 'var(--text-1)',
                    fontSize: 13,
                    lineHeight: 1.5,
                    opacity: batchRunning ? 0.6 : 1,
                  }}
                />

                <div className="flex items-center justify-between mt-2 gap-3">
                  <span style={{ color: 'var(--text-4)', fontSize: 11 }}>
                    {batchInput.split('\n').map(q => q.trim()).filter(Boolean).length}/20 个问题
                  </span>
                  <button
                    onClick={handleStartBatch}
                    disabled={batchRunning}
                    className="px-3 py-1.5 rounded-md cursor-pointer"
                    style={{
                      background: batchRunning ? 'var(--bg-s3)' : 'var(--green-btn)',
                      border: 'none',
                      color: batchRunning ? 'var(--text-4)' : '#fff',
                      fontSize: 12,
                      fontWeight: 600,
                    }}
                  >
                    {batchRunning ? '处理中...' : '开始批量问答'}
                  </button>
                </div>

                {batchResult && (
                  <div className="mt-3 rounded-md" style={{ border: '1px solid var(--border-muted)', background: 'var(--bg-s1)' }}>
                    <div className="px-3 py-2" style={{ borderBottom: '1px solid var(--border-muted)' }}>
                      <div className="flex items-center justify-between mb-1">
                        <span style={{ color: 'var(--text-2)', fontSize: 12 }}>
                          任务 {batchResult.batch_id}
                        </span>
                        <span style={{ color: batchStatus?.color ?? 'var(--text-3)', fontSize: 12 }}>
                          {batchStatus?.label ?? batchResult.status}
                        </span>
                      </div>
                      <div style={{ height: 4, background: 'var(--bg-base)', borderRadius: 2, overflow: 'hidden' }}>
                        <div
                          style={{
                            width: `${Math.round(((batchResult.completed + batchResult.failed) / Math.max(1, batchResult.total)) * 100)}%`,
                            height: '100%',
                            background: batchStatus?.color ?? 'var(--yellow)',
                            transition: 'width 240ms',
                          }}
                        />
                      </div>
                      <div className="mt-1 flex items-center justify-between gap-2" style={{ color: 'var(--text-4)', fontSize: 11 }}>
                        <span>完成 {batchResult.completed} / 失败 {batchResult.failed} / 总计 {batchResult.total}</span>
                        <span>{formatDateTime(batchResult.updated_at ?? batchResult.created_at)}</span>
                      </div>
                      <div className="mt-2 flex flex-wrap gap-2">
                        <button
                          onClick={handleCancelBatch}
                          disabled={!isBatchActive}
                          className="flex items-center gap-1 px-2 py-1 rounded cursor-pointer"
                          style={{
                            background: isBatchActive ? 'rgba(248,81,73,0.12)' : 'var(--bg-s2)',
                            border: isBatchActive ? '1px solid rgba(248,81,73,0.35)' : '1px solid var(--border-muted)',
                            color: isBatchActive ? 'var(--red)' : 'var(--text-4)',
                            fontSize: 11,
                            opacity: isBatchActive ? 1 : 0.55,
                          }}
                        >
                          <Ban size={12} /> 取消任务
                        </button>
                        <button
                          onClick={() => handleExportBatch('json')}
                          className="flex items-center gap-1 px-2 py-1 rounded cursor-pointer"
                          style={{ background: 'var(--bg-s2)', border: '1px solid var(--border-muted)', color: 'var(--text-3)', fontSize: 11 }}
                        >
                          <Download size={12} /> JSON
                        </button>
                        <button
                          onClick={() => handleExportBatch('csv')}
                          className="flex items-center gap-1 px-2 py-1 rounded cursor-pointer"
                          style={{ background: 'var(--bg-s2)', border: '1px solid var(--border-muted)', color: 'var(--text-3)', fontSize: 11 }}
                        >
                          <Download size={12} /> CSV
                        </button>
                      </div>
                    </div>

                    <div className="max-h-72 overflow-y-auto">
                      {batchResult.results.length === 0 ? (
                        <div className="px-3 py-4 text-center" style={{ color: 'var(--text-4)', fontSize: 12 }}>
                          等待第一条结果...
                        </div>
                      ) : (
                        batchResult.results.map((item, index) => (
                          <details key={`${item.question}-${index}`} className="px-3 py-2" style={{ borderBottom: '1px solid var(--border-muted)' }}>
                            <summary className="cursor-pointer" style={{ color: item.error ? 'var(--red)' : 'var(--text-2)', fontSize: 12 }}>
                              {index + 1}. {item.question}
                            </summary>
                            <div className="mt-2 pl-4" style={{ color: item.error ? 'var(--red)' : 'var(--text-3)', fontSize: 12, lineHeight: 1.6 }}>
                              {item.error ? (
                                `失败：${item.error}`
                              ) : (
                                <SafeMarkdown text={item.answer ?? ''} />
                              )}
                            </div>
                          </details>
                        ))
                      )}
                    </div>
                  </div>
                )}

                <div className="mt-3 rounded-md" style={{ border: '1px solid var(--border-muted)', background: 'var(--bg-s1)' }}>
                  <div className="flex items-center justify-between px-3 py-2" style={{ borderBottom: '1px solid var(--border-muted)' }}>
                    <span style={{ color: 'var(--text-2)', fontSize: 12, fontWeight: 600 }}>历史批次</span>
                    <button
                      onClick={loadBatchHistory}
                      disabled={batchHistoryLoading}
                      className="flex items-center gap-1 cursor-pointer"
                      style={{ background: 'none', border: 'none', color: 'var(--text-4)', fontSize: 11, opacity: batchHistoryLoading ? 0.5 : 1 }}
                    >
                      <RefreshCw size={12} /> 刷新
                    </button>
                  </div>
                  <div className="max-h-36 overflow-y-auto">
                    {batchHistoryLoading ? (
                      <div className="px-3 py-3 text-center" style={{ color: 'var(--text-4)', fontSize: 12 }}>加载中...</div>
                    ) : batchHistory.length === 0 ? (
                      <div className="px-3 py-3 text-center" style={{ color: 'var(--text-4)', fontSize: 12 }}>暂无历史批次</div>
                    ) : (
                      batchHistory.map(item => {
                        const status = getBatchStatusMeta(item.status);
                        return (
                          <button
                            key={item.batch_id}
                            onClick={() => handleLoadBatch(item.batch_id)}
                            className="w-full flex items-center justify-between gap-3 px-3 py-2 cursor-pointer text-left"
                            style={{
                              background: batchResult?.batch_id === item.batch_id ? 'var(--bg-s2)' : 'transparent',
                              border: 'none',
                              borderBottom: '1px solid var(--border-muted)',
                            }}
                          >
                            <span className="flex-1 min-w-0">
                              <span className="block truncate" style={{ color: 'var(--text-2)', fontSize: 12 }}>{item.batch_id}</span>
                              <span className="block mt-0.5" style={{ color: 'var(--text-4)', fontSize: 10 }}>
                                {formatDateTime(item.updated_at ?? item.created_at)} · {item.completed + item.failed}/{item.total}
                              </span>
                            </span>
                            <span style={{ color: status.color, fontSize: 11, flexShrink: 0 }}>{status.label}</span>
                          </button>
                        );
                      })
                    )}
                  </div>
                </div>
              </div>
            </div>
          )}

          <div className="max-w-3xl mx-auto flex gap-2">
            <textarea
              ref={inputRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="向知识图谱提问..."
              disabled={isThinking}
              rows={1}
              className="flex-1 resize-none rounded-lg px-4 py-2.5 outline-none"
              style={{
                background: 'var(--bg-s2)', border: '1px solid var(--border-main)',
                color: 'var(--text-1)', fontSize: 14, minHeight: 42, maxHeight: 120,
                opacity: isThinking ? 0.5 : 1,
              }}
            />
            <button
              onClick={handleSend}
              disabled={isThinking || !input.trim()}
              className="px-4 py-2 rounded-lg cursor-pointer flex items-center gap-2"
              style={{
                background: input.trim() ? 'var(--green-btn)' : 'var(--bg-s2)',
                color: input.trim() ? '#fff' : 'var(--text-4)',
                border: 'none', fontSize: 13, fontWeight: 500,
                opacity: isThinking ? 0.5 : 1,
              }}
            >
              <Send size={14} /> 发送
            </button>
          </div>
          <div className="max-w-3xl mx-auto mt-1.5">
            <div className="flex items-center gap-2" style={{ color: 'var(--text-4)', fontSize: 11 }}>
              <span>Enter 发送，Shift+Enter 换行</span>
              <span>|</span>
              <button
                onClick={() => setShowBatchPanel(v => !v)}
                className="cursor-pointer"
                style={{ background: 'none', border: 'none', color: showBatchPanel ? 'var(--blue)' : 'var(--text-4)', fontSize: 11, padding: 0 }}
              >
                批量问答管理
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function StreamTrace({ status }: { status: string }) {
  return (
    <div
      className="mb-3 rounded-md px-3 py-2 flex items-center gap-2"
      style={{ background: 'rgba(88,166,255,0.08)', border: '1px solid rgba(88,166,255,0.18)', color: 'var(--text-3)', fontSize: 12 }}
    >
      <Info size={13} style={{ color: 'var(--blue)', flexShrink: 0 }} />
      <span style={{ color: 'var(--text-4)' }}>推理过程</span>
      <span style={{ color: 'var(--text-2)' }}>{status}</span>
    </div>
  );
}

function InlineThinkingDots() {
  return (
    <div className="flex items-center gap-1.5 py-1">
      <span className="thinking-dot" />
      <span className="thinking-dot" />
      <span className="thinking-dot" />
    </div>
  );
}

function ToolCallPanel({ toolCalls }: { toolCalls: ToolCall[] }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="mt-3">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 cursor-pointer"
        style={{ background: 'none', border: 'none', color: 'var(--text-3)', fontSize: 12 }}
      >
        <ChevronRight
          size={12}
          style={{ transform: expanded ? 'rotate(90deg)' : 'none', transition: 'transform 150ms' }}
        />
        工具调用 ({toolCalls.length} 步)
      </button>
      {expanded && (
        <div className="mt-2 rounded-md overflow-hidden" style={{ background: 'var(--bg-s3)', border: '1px solid var(--border-muted)' }}>
          {toolCalls.map(tc => (
            <div key={tc.step} className="p-3" style={{ borderBottom: '1px solid var(--border-muted)' }}>
              <div className="flex items-center gap-2 mb-2">
                <span style={{ color: 'var(--text-4)', fontSize: 11 }}>步骤 {tc.step}</span>
                <span style={{ color: 'var(--yellow)', fontSize: 12, fontFamily: 'monospace', fontWeight: 600 }}>{tc.tool}</span>
              </div>
              <div className="mb-1" style={{ fontSize: 11, color: 'var(--text-4)' }}>输入:</div>
              <pre className="mb-2 p-2 rounded overflow-x-auto" style={{ background: 'var(--bg-base)', fontSize: 11, color: 'var(--text-3)', fontFamily: 'monospace', lineHeight: 1.5 }}>
                {tc.input}
              </pre>
              <div className="mb-1" style={{ fontSize: 11, color: 'var(--text-4)' }}>输出:</div>
              <pre className="p-2 rounded overflow-x-auto" style={{ background: 'var(--bg-base)', fontSize: 11, color: 'var(--text-3)', fontFamily: 'monospace', lineHeight: 1.5 }}>
                {tc.output}
              </pre>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function SafeMarkdown({ text }: { text: string }) {
  return (
    <div>
      {text.split(/\r?\n/).map((line, index) => renderMarkdownLine(line, index))}
    </div>
  );
}

function renderMarkdownLine(line: string, index: number) {
  const key = `line-${index}`;
  if (!line.trim()) return <div key={key} style={{ height: 8 }} />;
  if (line.startsWith('### ')) {
    return <div key={key} style={{ fontSize: 14, fontWeight: 600, color: 'var(--text-1)', margin: '6px 0 4px' }}>{renderInlineMarkdown(line.slice(4))}</div>;
  }
  if (line.startsWith('## ')) {
    return <div key={key} style={{ fontSize: 16, fontWeight: 600, color: 'var(--text-1)', margin: '8px 0 4px' }}>{renderInlineMarkdown(line.slice(3))}</div>;
  }
  if (line.startsWith('> ')) {
    return <div key={key} style={{ borderLeft: '3px solid var(--blue)', paddingLeft: 12, color: 'var(--text-3)', margin: '8px 0' }}>{renderInlineMarkdown(line.slice(2))}</div>;
  }
  const ordered = line.match(/^(\d+)\.\s+(.*)$/);
  if (ordered) {
    return <div key={key} style={{ paddingLeft: 16, margin: '2px 0' }}>{ordered[1]}. {renderInlineMarkdown(ordered[2])}</div>;
  }
  if (line.startsWith('- ')) {
    return <div key={key} style={{ paddingLeft: 16, margin: '2px 0' }}>&bull; {renderInlineMarkdown(line.slice(2))}</div>;
  }
  return <div key={key}>{renderInlineMarkdown(line)}</div>;
}

function renderInlineMarkdown(text: string): React.ReactNode[] {
  const parts: React.ReactNode[] = [];
  const regex = /\*\*(.*?)\*\*/g;
  let lastIndex = 0;
  let partIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) parts.push(text.slice(lastIndex, match.index));
    parts.push(<strong key={`strong-${partIndex++}`} style={{ color: 'var(--text-1)' }}>{match[1]}</strong>);
    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) parts.push(text.slice(lastIndex));
  return parts;
}

function getBatchStatusMeta(status: ApiBatchResult['status']) {
  if (status === 'done') return { label: '已完成', color: 'var(--green)' };
  if (status === 'cancelled') return { label: '已取消', color: 'var(--red)' };
  if (status === 'running') return { label: '运行中', color: 'var(--yellow)' };
  return { label: '排队中', color: 'var(--blue)' };
}

function formatDateTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}
