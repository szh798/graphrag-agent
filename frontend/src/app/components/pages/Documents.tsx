import React, { useState, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router';
import { toast } from 'sonner';
import { FileText, UploadCloud, X, ChevronDown, ChevronRight, Eye, Play, Square } from 'lucide-react';
import { useAppState, type Document } from '../../store';
import { api, ApiError, type ApiDocumentExtractions, type ApiIndexResult } from '../../api';
import { useAuthRuntime } from '../../auth';
import { uploadDocumentDirect } from '../../direct-upload';
import { documentStatusLabel, documentStatusStyles } from '../../document-status';

export function Documents() {
  const { documents, setDocuments, refreshDocuments } = useAppState();
  const auth = useAuthRuntime();
  const identityPending = auth.enabled && !auth.apiReady;
  const navigate = useNavigate();
  const [formatFilter, setFormatFilter] = useState('All');
  const [statusFilter, setStatusFilter] = useState('All');
  const [searchTerm, setSearchTerm] = useState('');
  const [expandedDoc, setExpandedDoc] = useState<string | null>(null);
  const [loadingResultDocId, setLoadingResultDocId] = useState<string | null>(null);
  const [extractionsDoc, setExtractionsDoc] = useState<Document | null>(null);
  const [extractions, setExtractions] = useState<ApiDocumentExtractions | null>(null);
  const [extractionsLoading, setExtractionsLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const filteredDocs = documents.filter(d => {
    if (formatFilter !== 'All' && d.format !== formatFilter) return false;
    if (statusFilter !== 'All' && d.status !== statusFilter) return false;
    if (searchTerm && !d.filename.toLowerCase().includes(searchTerm.toLowerCase())) return false;
    return true;
  });

  const mapIndexResult = (result: ApiIndexResult): Document['result'] => ({
    nodes: result.summary?.nodes ?? result.nodes_added ?? result.stats?.nodes ?? 0,
    edges: result.summary?.edges ?? result.edges_added ?? result.stats?.edges ?? 0,
    pages: result.summary?.pages ?? result.pages_processed ?? result.stats?.pages ?? 0,
    extractions: result.recovered ? undefined : (result.summary?.extractions ?? result.extractions_count ?? result.stats?.raw_extractions ?? 0),
    duration: result.recovered ? undefined : (result.summary?.duration_seconds ?? result.duration_seconds ?? result.stats?.elapsed_seconds ?? result.elapsed_seconds ?? 0),
    recovered: result.recovered,
  });

  const handleToggleExpanded = useCallback(async (doc: Document) => {
    if (doc.status !== 'indexed') return;
    if (expandedDoc === doc.id) {
      setExpandedDoc(null);
      return;
    }

    setExpandedDoc(doc.id);
    if (doc.result) return;

    try {
      setLoadingResultDocId(doc.id);
      const result = await api.getDocumentIndexResult(doc.id);
      setDocuments(prev => prev.map(item => (
        item.id === doc.id ? { ...item, result: mapIndexResult(result) } : item
      )));
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : '索引结果加载失败';
      toast.error(msg);
    } finally {
      setLoadingResultDocId(null);
    }
  }, [expandedDoc, setDocuments]);

  const handleViewExtractions = useCallback(async (doc: Document) => {
    try {
      setExtractionsDoc(doc);
      setExtractions(null);
      setExtractionsLoading(true);
      setExtractions(await api.getDocumentExtractions(doc.id, 1, 200));
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : '提取结果加载失败';
      toast.error(msg);
    } finally {
      setExtractionsLoading(false);
    }
  }, []);

  const handleUpload = useCallback(async (file: File) => {
    if (identityPending) {
      toast.error('登录状态正在同步，请稍后重试');
      return;
    }
    setUploading(true);
    setUploadProgress(0);
    try {
      await uploadDocumentDirect(file, {
        onProgress: event => setUploadProgress(Math.round(event.percentage)),
      });
      toast.success('上传完成，正在登记文档');
      for (const delay of [300, 700, 1200, 2000]) {
        await new Promise(resolve => window.setTimeout(resolve, delay));
        await refreshDocuments();
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '上传失败');
    } finally {
      setUploading(false);
      setUploadProgress(0);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  }, [identityPending, refreshDocuments]);

  const handleStartIndex = useCallback(async (doc: Document) => {
    try {
      const job = await api.startIndexing(doc.id);
      setDocuments(items => items.map(item => item.id === doc.id
        ? { ...item, status: 'indexing', job_id: job.job_id, progress: 0, error: undefined }
        : item));
      toast.success('索引任务已启动');
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : '索引启动失败');
    }
  }, [setDocuments]);

  const handleCancelIndex = useCallback(async (doc: Document) => {
    if (!doc.job_id) return;
    try {
      await api.cancelJob(doc.job_id);
      setDocuments(items => items.map(item => item.id === doc.id
        ? { ...item, status: 'uploaded', job_id: undefined, progress: undefined }
        : item));
      toast.success('索引任务已取消');
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : '取消索引失败');
    }
  }, [setDocuments]);

  return (
    <div className="page-shell documents-page p-6" style={{ maxWidth: 1200, margin: '0 auto' }}>
      <h1 className="mb-4" style={{ color: 'var(--text-1)', fontSize: 20, fontWeight: 600 }}>文档浏览</h1>

      <div
        className="flex items-start gap-3 rounded-lg px-4 py-3 mb-6"
        style={{ background: 'rgba(88,166,255,0.07)', border: '1px solid rgba(88,166,255,0.22)' }}
      >
        <UploadCloud size={17} style={{ color: 'var(--blue)', flexShrink: 0, marginTop: 1 }} />
        <div>
          <div style={{ color: 'var(--text-1)', fontSize: 13, fontWeight: 600 }}>上传与索引已开放</div>
          <div style={{ color: 'var(--text-3)', fontSize: 12, marginTop: 2 }}>
            支持 PDF、Office、图片、HTML、TXT 和 Markdown，单文件上限 200MB。你的文档按当前访客或登录空间隔离。
          </div>
        </div>
      </div>

      {/* Toolbar */}
      <div className="documents-toolbar flex items-center gap-3 mb-4">
        <input
          ref={fileInputRef}
          type="file"
          disabled={uploading || identityPending}
          className="hidden"
          accept=".pdf,.doc,.docx,.ppt,.pptx,.png,.jpg,.jpeg,.html,.txt,.md,.markdown"
          onChange={event => {
            const file = event.target.files?.[0];
            if (file) void handleUpload(file);
          }}
        />
        <button
          type="button"
          disabled={uploading || identityPending}
          onClick={() => fileInputRef.current?.click()}
          className="flex items-center gap-2 px-3 py-1.5 rounded-md cursor-pointer"
          style={{ background: 'var(--blue)', border: 0, color: 'var(--on-blue)', fontSize: 13, fontWeight: 600, opacity: uploading || identityPending ? 0.7 : 1 }}
        >
          <UploadCloud size={14} /> {identityPending ? '正在同步登录' : uploading ? `上传中 ${uploadProgress}%` : '上传文档'}
        </button>
        <select
          aria-label="按文档格式筛选"
          value={formatFilter}
          onChange={e => setFormatFilter(e.target.value)}
          className="px-3 py-1.5 rounded-md cursor-pointer"
          style={{ background: 'var(--bg-s2)', border: '1px solid var(--border-main)', color: 'var(--text-2)', fontSize: 13 }}
        >
          <option value="All">全部格式</option>
          <option>PDF</option>
          <option>DOCX</option>
          <option>PPTX</option>
          <option>PNG</option>
          <option>JPG</option>
          <option>HTML</option>
        </select>
        <select
          aria-label="按索引状态筛选"
          value={statusFilter}
          onChange={e => setStatusFilter(e.target.value)}
          className="px-3 py-1.5 rounded-md cursor-pointer"
          style={{ background: 'var(--bg-s2)', border: '1px solid var(--border-main)', color: 'var(--text-2)', fontSize: 13 }}
        >
          <option value="All">全部状态</option>
          <option value="indexed">已索引</option>
          <option value="indexing">索引中</option>
          <option value="uploaded">已上传</option>
          <option value="failed">失败</option>
        </select>
        <input
          aria-label="搜索文档"
          value={searchTerm}
          onChange={e => setSearchTerm(e.target.value)}
          placeholder="搜索文档..."
          className="px-3 py-1.5 rounded-md flex-1"
          style={{ background: 'var(--bg-s2)', border: '1px solid var(--border-main)', color: 'var(--text-1)', fontSize: 13, outline: 'none' }}
        />
      </div>

      {/* Document Table */}
      <div className="documents-table rounded-lg overflow-auto" style={{ background: 'var(--bg-s1)', border: '1px solid var(--border-main)' }}>
        {/* Header */}
        <div
          className="grid gap-4 px-4 py-2.5"
          style={{
            gridTemplateColumns: '24px 1fr 70px 50px 100px 140px 160px',
            background: 'var(--bg-s2)', fontSize: 11, fontWeight: 600,
            color: 'var(--text-3)', textTransform: 'uppercase', letterSpacing: '0.5px',
          }}
        >
          <span />
          <span>文件名</span>
          <span>格式</span>
          <span>页数</span>
          <span>状态</span>
          <span>上传日期</span>
          <span>操作</span>
        </div>

        {/* Rows */}
        {filteredDocs.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 gap-3">
            <FileText size={40} style={{ color: 'var(--text-4)' }} />
            <span style={{ color: 'var(--text-3)', fontSize: 14 }}>
              {documents.length === 0 ? '当前空间暂无文档' : '未找到匹配文档'}
            </span>
          </div>
        ) : (
          filteredDocs.map(doc => {
            const st = documentStatusStyles[doc.status] ?? documentStatusStyles.unknown;
            const isExpanded = expandedDoc === doc.id;
            return (
              <React.Fragment key={doc.id}>
                <div
                  className="grid gap-4 px-4 py-3 items-center"
                  style={{
                    gridTemplateColumns: '24px 1fr 70px 50px 100px 140px 160px',
                    borderBottom: '1px solid var(--border-muted)',
                    fontSize: 13,
                  }}
                >
	                  <button
	                    type="button"
	                    aria-label={doc.status === 'indexed' ? `${isExpanded ? '收起' : '展开'} ${doc.filename} 的索引结果` : undefined}
	                    disabled={doc.status !== 'indexed'}
	                    onClick={() => handleToggleExpanded(doc)}
	                    className="cursor-pointer"
	                    style={{ background: 'none', border: 'none', color: 'var(--text-4)', padding: 0 }}
	                  >
                    {doc.status === 'indexed'
                      ? (isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />)
                      : <span style={{ width: 14, display: 'inline-block' }} />}
                  </button>
                  <span className="flex items-center gap-2 truncate" style={{ color: 'var(--text-1)' }}>
                    <FileText size={14} style={{ color: 'var(--text-3)', flexShrink: 0 }} />
                    <span className="truncate">{doc.filename}</span>
                  </span>
                  <span style={{ color: 'var(--text-3)' }}>{doc.format}</span>
                  <span style={{ color: 'var(--text-3)' }}>{doc.pages || '—'}</span>
                  <span>
                    <span className="px-2 py-0.5 rounded-full inline-flex items-center gap-1" style={{ fontSize: 11, fontWeight: 600, background: st.bg, color: st.color }}>
                      {doc.status === 'indexing' && (
                        <span className="inline-block w-1.5 h-1.5 rounded-full animate-pulse" style={{ background: st.color }} />
                      )}
                      {documentStatusLabel[doc.status]}
                    </span>
                  </span>
                  <span style={{ color: 'var(--text-4)', fontSize: 12 }}>
                    {new Date(doc.upload_date).toLocaleDateString('zh-CN', { month: 'short', day: 'numeric', year: 'numeric' })}
                  </span>
                  <span className="flex items-center gap-2">
                    {doc.status === 'uploaded' && (
                      <button
                        onClick={() => void handleStartIndex(doc)}
                        className="flex items-center gap-1 px-2 py-1 rounded cursor-pointer"
                        style={{ fontSize: 11, background: 'rgba(63,185,80,0.12)', color: 'var(--green)', border: '1px solid rgba(63,185,80,0.25)' }}
                      >
                        <Play size={10} /> 开始索引
                      </button>
                    )}
                    {doc.status === 'indexing' && (
                      <>
                        <div className="flex items-center gap-1.5 flex-1">
                          <div style={{ flex: 1, height: 4, background: 'var(--bg-s2)', borderRadius: 2, overflow: 'hidden', minWidth: 40 }}>
                            <div style={{ width: `${doc.progress ?? 0}%`, height: '100%', background: 'var(--yellow)', borderRadius: 2, transition: 'width 300ms' }} />
                          </div>
                          <span style={{ fontSize: 10, color: 'var(--yellow)', whiteSpace: 'nowrap' }}>{doc.progress ?? 0}%</span>
                        </div>
                        <button
                          onClick={() => void handleCancelIndex(doc)}
                          disabled={!doc.job_id}
                          className="flex items-center gap-1 px-2 py-1 rounded cursor-pointer"
                          style={{ fontSize: 10, background: 'rgba(248,81,73,0.1)', color: 'var(--red)', border: '1px solid rgba(248,81,73,0.2)' }}
                        >
                          <Square size={9} /> 取消
                        </button>
                      </>
                    )}
                    {doc.status === 'indexed' && (
                      <button
                        onClick={() => navigate(`/graph?doc_id=${doc.id}`)}
                        className="flex items-center gap-1 px-2 py-1 rounded cursor-pointer"
                        style={{ fontSize: 11, background: 'rgba(88,166,255,0.1)', color: 'var(--blue)', border: 'none' }}
                      >
                        <Eye size={10} /> 查看图谱
                      </button>
                    )}
                    {doc.status === 'failed' && (
                      <button
                        onClick={() => void handleStartIndex(doc)}
                        className="flex items-center gap-1 px-2 py-1 rounded cursor-pointer"
                        style={{ fontSize: 11, background: 'rgba(63,185,80,0.12)', color: 'var(--green)', border: '1px solid rgba(63,185,80,0.25)' }}
                      >
                        <Play size={10} /> 重新索引
                      </button>
                    )}
                  </span>
                </div>

                {/* Expanded Result Row */}
	                {isExpanded && doc.status === 'indexed' && (
	                  <div className="px-12 py-3" style={{ background: 'var(--bg-s2)', borderBottom: '1px solid var(--border-muted)' }}>
	                    {doc.result ? (
	                      <div className="flex items-center gap-4 mb-2" style={{ fontSize: 13, color: 'var(--text-2)' }}>
	                        <span>{doc.result.nodes} 个节点</span>
	                        <span style={{ color: 'var(--text-4)' }}>&middot;</span>
	                        <span>{doc.result.edges} 条边</span>
	                        <span style={{ color: 'var(--text-4)' }}>&middot;</span>
	                        <span>{doc.result.pages} 页</span>
	                        {doc.result.recovered ? (
	                          <>
	                            <span style={{ color: 'var(--text-4)' }}>&middot;</span>
	                            <span style={{ color: 'var(--yellow)' }}>历史索引摘要（由现有图谱恢复）</span>
	                          </>
	                        ) : (
	                          <>
	                            <span style={{ color: 'var(--text-4)' }}>&middot;</span>
	                            <span>{doc.result.extractions ?? 0} 次提取</span>
	                            <span style={{ color: 'var(--text-4)' }}>&middot;</span>
	                            <span>{(doc.result.duration ?? 0).toFixed(1)}秒</span>
	                          </>
	                        )}
	                      </div>
	                    ) : (
	                      <div className="mb-2" style={{ fontSize: 13, color: 'var(--text-3)' }}>
	                        {loadingResultDocId === doc.id ? '正在加载索引结果...' : '暂无索引结果详情'}
	                      </div>
	                    )}
	                    <div className="flex items-center gap-2">
	                      <button
	                        onClick={() => navigate(`/graph?doc_id=${doc.id}`)}
                        className="flex items-center gap-1 px-2 py-1 rounded cursor-pointer"
                        style={{ fontSize: 11, background: 'rgba(88,166,255,0.1)', color: 'var(--blue)', border: 'none' }}
	                      >
	                        在图谱中查看
	                      </button>
	                      {!doc.result?.recovered && (
	                        <button
	                          onClick={() => handleViewExtractions(doc)}
	                          className="flex items-center gap-1 px-2 py-1 rounded cursor-pointer"
	                          style={{ fontSize: 11, background: 'var(--bg-s1)', color: 'var(--text-2)', border: '1px solid var(--border-muted)' }}
	                        >
	                          查看提取结果
	                        </button>
	                      )}
	                    </div>
	                  </div>
                )}

                {/* Error message */}
                {doc.status === 'failed' && doc.error && (
                  <div className="px-12 py-2" style={{ background: 'rgba(248,81,73,0.05)', borderBottom: '1px solid var(--border-muted)' }}>
                    <span style={{ fontSize: 12, color: 'var(--red)' }}>{doc.error}</span>
                  </div>
                )}
              </React.Fragment>
            );
          })
        )}
      </div>

	      {extractionsDoc && (
	        <div
	          className="fixed inset-0 flex items-center justify-center"
	          style={{ background: 'rgba(0,0,0,0.6)', zIndex: 1000 }}
	          onClick={() => setExtractionsDoc(null)}
	        >
	          <div
	            className="rounded-xl"
	            style={{ background: 'var(--bg-s1)', border: '1px solid var(--border-main)', width: 760, maxHeight: '82vh', boxShadow: 'var(--shadow-lg)', overflow: 'hidden' }}
	            onClick={e => e.stopPropagation()}
	          >
	            <div className="flex items-center justify-between px-5 py-4" style={{ borderBottom: '1px solid var(--border-main)' }}>
	              <div>
	                <h3 style={{ color: 'var(--text-1)', fontSize: 16, fontWeight: 600 }}>提取结果</h3>
	                <div style={{ color: 'var(--text-4)', fontSize: 12, marginTop: 2 }}>{extractionsDoc.filename}</div>
	              </div>
	              <button
	                onClick={() => setExtractionsDoc(null)}
	                className="cursor-pointer p-1 rounded"
	                style={{ background: 'transparent', border: 'none', color: 'var(--text-4)' }}
	              >
	                <X size={16} />
	              </button>
	            </div>

	            <div className="p-5 overflow-y-auto" style={{ maxHeight: 'calc(82vh - 74px)' }}>
	              {extractionsLoading ? (
	                <div style={{ color: 'var(--text-3)', fontSize: 13 }}>正在加载提取记录...</div>
	              ) : extractions ? (
	                <>
	                  <div className="grid grid-cols-4 gap-3 mb-4">
	                    {[
	                      { label: '节点', value: extractions.summary.nodes },
	                      { label: '边', value: extractions.summary.edges },
	                      { label: '页数', value: extractions.summary.pages },
	                      { label: '提取', value: extractions.total },
	                    ].map(item => (
	                      <div key={item.label} className="rounded-md p-3" style={{ background: 'var(--bg-s2)', border: '1px solid var(--border-muted)' }}>
	                        <div style={{ color: 'var(--text-4)', fontSize: 11 }}>{item.label}</div>
	                        <div style={{ color: 'var(--text-1)', fontSize: 20, fontWeight: 700 }}>{item.value}</div>
	                      </div>
	                    ))}
	                  </div>

	                  {extractions.items.length === 0 ? (
	                    <div style={{ color: 'var(--text-3)', fontSize: 13 }}>暂无提取记录</div>
	                  ) : (
	                    <div className="rounded-lg overflow-hidden" style={{ border: '1px solid var(--border-muted)' }}>
	                      <div className="grid gap-3 px-3 py-2" style={{ gridTemplateColumns: '1fr 130px 70px 120px', background: 'var(--bg-s2)', color: 'var(--text-4)', fontSize: 11, fontWeight: 600 }}>
	                        <span>文本</span>
	                        <span>类型</span>
	                        <span>页码</span>
	                        <span>匹配</span>
	                      </div>
	                      {extractions.items.map((item, index) => (
	                        <div key={`${item.text}-${index}`} className="grid gap-3 px-3 py-2" style={{ gridTemplateColumns: '1fr 130px 70px 120px', borderTop: '1px solid var(--border-muted)', fontSize: 12 }}>
	                          <span style={{ color: 'var(--text-1)', wordBreak: 'break-word' }}>{item.text}</span>
	                          <span style={{ color: 'var(--blue)' }}>{item.type}</span>
	                          <span style={{ color: 'var(--text-3)' }}>{item.page}</span>
	                          <span style={{ color: 'var(--text-3)' }}>{item.alignment ?? '—'}</span>
	                        </div>
	                      ))}
	                    </div>
	                  )}
	                </>
	              ) : (
	                <div style={{ color: 'var(--text-3)', fontSize: 13 }}>未加载到提取结果</div>
	              )}
	            </div>
	          </div>
	        </div>
	      )}
	    </div>
	  );
	}
