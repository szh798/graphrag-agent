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
        {value ?? '—'}
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
  const storage = health?.components.storage;
  const graphDb = health?.components.graph_database;
  const appDb = health?.components.app_database;
  const blobStorage = health?.components.blob_storage;
  const taskQueue = health?.components.task_queue;
  const productionReady = health?.production_ready === true;

  return (
    <div className="page-shell settings-page p-6" style={{ maxWidth: 1180, margin: '0 auto' }}>
      <div className="page-heading flex items-center justify-between mb-6">
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

      {health && !productionReady && (
        <div className="flex items-start gap-3 rounded-lg p-4 mb-4" style={{ background: 'rgba(210,153,34,0.1)', border: '1px solid rgba(210,153,34,0.35)' }}>
          <AlertTriangle size={18} style={{ color: 'var(--yellow)', flexShrink: 0, marginTop: 1 }} />
          <div>
            <div style={{ color: 'var(--yellow)', fontSize: 13, fontWeight: 600 }}>生产持久化依赖尚未全部就绪</div>
            <div style={{ color: 'var(--text-3)', fontSize: 12, marginTop: 4, lineHeight: 1.6 }}>
              请检查图谱数据库、业务数据库、对象存储和后台队列。
            </div>
          </div>
        </div>
      )}

      <div className="settings-grid grid grid-cols-2 gap-4">
        <SettingsCard icon={<FileText size={17} />} title="文档解析" component={parser}>
          <Field label="解析模式" value={parser?.mode} />
          <Field label="当前解析器" value={parser?.active_parser} />
          <Field label="配置详情" value="公开环境已隐藏" />
        </SettingsCard>

        <SettingsCard icon={<Cpu size={17} />} title="问答与索引模型" component={llm}>
          <Field label="服务状态" value={llm?.status === 'ok' ? '可用' : '不可用'} />
          <Field label="配置详情" value="公开环境已隐藏" />
        </SettingsCard>

        <SettingsCard icon={<FileText size={17} />} title="MinerU 云端" component={mineru}>
          <Field label="服务状态" value={mineru?.status === 'ok' ? '可用' : '未启用或不可用'} />
          <Field label="配置详情" value="公开环境已隐藏" />
        </SettingsCard>

        <SettingsCard icon={<Database size={17} />} title="临时处理目录" component={storage}>
          <Field label="用途" value="解析过程缓存" />
          <Field label="模式" value={storage?.persistence === 'ephemeral' ? '临时（正常）' : storage?.persistence} />
        </SettingsCard>

        <SettingsCard icon={<Database size={17} />} title="图谱数据库" component={graphDb}>
          <Field label="后端" value={graphDb?.backend} />
          <Field label="持久化状态" value={graphDb?.persistence ?? (graphDb?.backend === 'filesystem' ? '临时' : '持久')} />
        </SettingsCard>

        <SettingsCard icon={<Database size={17} />} title="业务数据库" component={appDb}>
          <Field label="后端" value={appDb?.backend} />
          <Field label="持久化状态" value={appDb?.persistence ?? (appDb?.backend === 'filesystem' ? '临时' : '持久')} />
        </SettingsCard>

        <SettingsCard icon={<FileText size={17} />} title="Blob 对象存储" component={blobStorage}>
          <Field label="后端" value={blobStorage?.backend} />
          <Field label="持久化状态" value={blobStorage?.persistence ?? (blobStorage?.backend === 'filesystem' ? '临时' : '持久')} />
        </SettingsCard>

        <SettingsCard icon={<Database size={17} />} title="索引队列" component={taskQueue}>
          <Field label="后端" value={taskQueue?.backend} />
          <Field label="持久队列" value={taskQueue?.durable ? '是' : '否'} />
        </SettingsCard>
      </div>
    </div>
  );
}
