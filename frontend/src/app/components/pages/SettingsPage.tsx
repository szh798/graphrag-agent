import React, { useEffect, useState } from 'react';
import { AlertTriangle, CheckCircle2, Cpu, Database, FileText, RefreshCw, XCircle } from 'lucide-react';
import { toast } from 'sonner';
import { api, ApiError, type ApiComponentHealth, type ApiHealthData } from '../../api';

function StatusBadge({ status }: { status?: string }) {
  const ok = status === 'ok';
  return (
    <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full" style={{
      background: ok ? 'rgba(63,185,80,0.12)' : 'rgba(248,81,73,0.12)',
      color: ok ? 'var(--green)' : 'var(--red)',
      fontSize: 11,
      fontWeight: 600,
    }}>
      {ok ? <CheckCircle2 size={11} /> : <XCircle size={11} />}
      {status ?? 'unknown'}
    </span>
  );
}

function Field({ label, value }: { label: string; value?: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 py-2" style={{ borderBottom: '1px solid var(--border-muted)' }}>
      <span style={{ color: 'var(--text-3)', fontSize: 12 }}>{label}</span>
      <span className="text-right" style={{ color: 'var(--text-1)', fontSize: 12, wordBreak: 'break-all' }}>
        {value ?? '未配置'}
      </span>
    </div>
  );
}

function SettingsCard({
  icon,
  title,
  component,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  component?: ApiComponentHealth;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg p-4" style={{ background: 'var(--bg-s1)', border: '1px solid var(--border-main)' }}>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span style={{ color: 'var(--blue)' }}>{icon}</span>
          <h2 style={{ color: 'var(--text-1)', fontSize: 15, fontWeight: 600 }}>{title}</h2>
        </div>
        <StatusBadge status={component?.status} />
      </div>
      {children}
    </section>
  );
}

export function SettingsPage() {
  const [health, setHealth] = useState<ApiHealthData | null>(null);
  const [loading, setLoading] = useState(false);

  const loadHealth = async () => {
    try {
      setLoading(true);
      setHealth(await api.getHealth());
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : '配置状态加载失败';
      toast.error(msg);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadHealth();
  }, []);

  const parser = health?.components.document_parser;
  const mineru = health?.components.mineru_api ?? health?.components.mineru_venv;
  const llm = health?.components.llm_api ?? health?.components.deepseek_api;
  const indexLlm = health?.components.llm_index_api;
  const storage = health?.components.storage;
  const graphDb = health?.components.graph_database;
  const appDb = health?.components.app_database;
  const blobStorage = health?.components.blob_storage;
  const taskQueue = health?.components.task_queue;
  const isEphemeral = storage?.persistent === false || storage?.persistence === 'ephemeral';

  return (
    <div className="p-6" style={{ maxWidth: 1180, margin: '0 auto' }}>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 style={{ color: 'var(--text-1)', fontSize: 20, fontWeight: 600 }}>系统设置</h1>
          <div style={{ color: 'var(--text-4)', fontSize: 12, marginTop: 4 }}>
            后端版本 {health?.version ?? '—'} · 运行 {health ? `${health.uptime_seconds}s` : '—'}
          </div>
        </div>
        <button
          onClick={loadHealth}
          disabled={loading}
          className="flex items-center gap-2 px-3 py-2 rounded-md cursor-pointer"
          style={{
            background: 'var(--bg-s2)',
            border: '1px solid var(--border-main)',
            color: 'var(--text-2)',
            fontSize: 13,
            opacity: loading ? 0.6 : 1,
          }}
        >
          <RefreshCw size={14} /> {loading ? '刷新中...' : '刷新状态'}
        </button>
      </div>

      {isEphemeral && (
        <div className="flex items-start gap-3 rounded-lg p-4 mb-4" style={{ background: 'rgba(210,153,34,0.1)', border: '1px solid rgba(210,153,34,0.35)' }}>
          <AlertTriangle size={18} style={{ color: 'var(--yellow)', flexShrink: 0, marginTop: 1 }} />
          <div>
            <div style={{ color: 'var(--yellow)', fontSize: 13, fontWeight: 600 }}>当前线上存储为临时文件系统</div>
            <div style={{ color: 'var(--text-3)', fontSize: 12, marginTop: 4, lineHeight: 1.6 }}>
              {storage?.warning ?? '生产环境请接入数据库、对象存储和后台队列。'}
            </div>
          </div>
        </div>
      )}

      <div className="grid grid-cols-2 gap-4">
        <SettingsCard icon={<FileText size={17} />} title="文档解析" component={parser}>
          <Field label="解析模式" value={parser?.mode} />
          <Field label="当前解析器" value={parser?.active_parser} />
          <Field label="MinerU 已配置" value={parser?.mineru_configured ? '是' : '否'} />
          <Field label="本地格式" value={parser?.local_supported_formats?.join(', ')} />
        </SettingsCard>

        <SettingsCard icon={<Cpu size={17} />} title="智谱模型" component={llm}>
          <Field label="Provider" value={llm?.provider} />
          <Field label="问答模型" value={llm?.model} />
          <Field label="索引轻量模型" value={indexLlm?.model ?? llm?.index_model} />
          <Field label="API Key" value={llm?.key_configured ? '已配置' : '未配置'} />
          <Field label="Base URL" value={llm?.base_url} />
        </SettingsCard>

        <SettingsCard icon={<FileText size={17} />} title="MinerU 云端" component={mineru}>
          <Field label="Base URL" value={mineru?.base_url} />
          <Field label="模型版本" value={mineru?.model} />
          <Field label="Token" value={mineru?.key_configured ? '已配置' : '未配置'} />
        </SettingsCard>

        <SettingsCard icon={<Database size={17} />} title="数据存储" component={storage}>
          <Field label="模式" value={storage?.mode} />
          <Field label="持久化" value={storage?.persistent ? '持久' : '临时'} />
          <Field label="数据目录" value={storage?.data_dir} />
          <Field label="KG 节点文件" value={storage?.kg_nodes_exists ? '存在' : '缺失'} />
          <Field label="KG 边文件" value={storage?.kg_edges_exists ? '存在' : '缺失'} />
          <Field label="上传目录" value={storage?.uploads_dir_exists ? '存在' : '缺失'} />
        </SettingsCard>

        <SettingsCard icon={<Database size={17} />} title="Neo4j 图谱库" component={graphDb}>
          <Field label="后端" value={graphDb?.backend} />
          <Field label="数据库" value={graphDb?.database} />
          <Field label="URI" value={graphDb?.uri_configured ? '已配置' : '未配置'} />
          <Field label="向量维度" value={graphDb?.vector_dimensions} />
          <Field label="错误" value={graphDb?.error} />
        </SettingsCard>

        <SettingsCard icon={<Database size={17} />} title="Postgres 业务库" component={appDb}>
          <Field label="后端" value={appDb?.backend} />
          <Field label="连接串" value={appDb?.url_configured ? '已配置' : '未配置'} />
          <Field label="文档记录" value={appDb?.documents} />
          <Field label="索引任务" value={appDb?.jobs} />
          <Field label="会话" value={appDb?.chat_sessions} />
          <Field label="批量任务" value={appDb?.batches} />
          <Field label="错误" value={appDb?.error} />
        </SettingsCard>

        <SettingsCard icon={<FileText size={17} />} title="Blob 对象存储" component={blobStorage}>
          <Field label="后端" value={blobStorage?.backend} />
          <Field label="Token" value={blobStorage?.token_configured ? '已配置' : '未配置'} />
          <Field label="上传目录" value={blobStorage?.uploads_dir_exists ? '存在' : '缺失'} />
          <Field label="错误" value={blobStorage?.error} />
        </SettingsCard>

        <SettingsCard icon={<Database size={17} />} title="索引队列" component={taskQueue}>
          <Field label="后端" value={taskQueue?.backend} />
          <Field label="持久队列" value={taskQueue?.durable ? '是' : '否'} />
          <Field label="URL" value={taskQueue?.url_configured ? '已配置' : '未配置'} />
          <Field label="Token" value={taskQueue?.token_configured ? '已配置' : '未配置'} />
          <Field label="提示" value={taskQueue?.warning} />
          <Field label="错误" value={taskQueue?.error} />
        </SettingsCard>
      </div>
    </div>
  );
}
