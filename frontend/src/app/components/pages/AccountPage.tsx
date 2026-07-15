import React, { useCallback, useEffect, useState } from 'react';
import { SignInButton } from '@clerk/react';
import {
  Building2,
  Download,
  Gauge,
  RefreshCw,
  ShieldCheck,
  Trash2,
  UserRound,
} from 'lucide-react';
import { toast } from 'sonner';
import {
  api,
  ApiError,
  type ApiAccountIdentity,
  type ApiOpsSummary,
  type ApiUsageSummary,
} from '../../api';
import { useAuthRuntime } from '../../auth';

function Card({ title, icon, children }: { title: string; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="rounded-lg p-4" style={{ background: 'var(--bg-s1)', border: '1px solid var(--border-main)' }}>
      <div className="flex items-center gap-2 mb-4">
        <span style={{ color: 'var(--blue)' }}>{icon}</span>
        <h2 style={{ color: 'var(--text-1)', fontSize: 15, fontWeight: 600 }}>{title}</h2>
      </div>
      {children}
    </section>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 py-2" style={{ borderBottom: '1px solid var(--border-muted)' }}>
      <span style={{ color: 'var(--text-3)', fontSize: 12 }}>{label}</span>
      <span className="text-right" style={{ color: 'var(--text-1)', fontSize: 12, overflowWrap: 'anywhere' }}>{value}</span>
    </div>
  );
}

function formatNumber(value = 0) {
  return Number(value).toLocaleString('zh-CN');
}

function UsagePanel({ usage, title }: { usage: ApiUsageSummary | null; title: string }) {
  return (
    <Card title={title} icon={<Gauge size={17} />}>
      <div className="grid grid-cols-2 gap-3">
        {[
          ['调用次数', formatNumber(usage?.total.events)],
          ['输入 Token', formatNumber(usage?.total.input_tokens)],
          ['输出 Token', formatNumber(usage?.total.output_tokens)],
          ['估算成本', usage?.pricing_configured ? `¥${Number(usage.total.cost_cny).toFixed(6)}` : '价格未配置'],
        ].map(([label, value]) => (
          <div key={label} className="rounded-md p-3" style={{ background: 'var(--bg-s2)' }}>
            <div style={{ color: 'var(--text-4)', fontSize: 11 }}>{label}</div>
            <div style={{ color: 'var(--text-1)', fontSize: 18, fontWeight: 650, marginTop: 4 }}>{value}</div>
          </div>
        ))}
      </div>
      <div style={{ color: 'var(--text-4)', fontSize: 11, marginTop: 10 }}>
        最近 {usage?.days ?? 30} 天 · 按实际模型用量记录，缺少供应商计量时标记为估算。
      </div>
    </Card>
  );
}

function AccountDashboard({ apiReady }: { apiReady: boolean }) {
  const [identity, setIdentity] = useState<ApiAccountIdentity | null>(null);
  const [usage, setUsage] = useState<ApiUsageSummary | null>(null);
  const [tenantUsage, setTenantUsage] = useState<ApiUsageSummary | null>(null);
  const [ops, setOps] = useState<ApiOpsSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [confirmation, setConfirmation] = useState('');
  const [deleting, setDeleting] = useState(false);

  const load = useCallback(async () => {
    if (!apiReady) return;
    setLoading(true);
    try {
      const [me, ownUsage] = await Promise.all([api.getAccountMe(), api.getAccountUsage(30)]);
      setIdentity(me);
      setUsage(ownUsage);
      const admin = me.role === 'admin' || me.role === 'owner';
      if (admin) {
        const [allUsage, operations] = await Promise.all([
          api.getAccountUsage(30, true),
          api.getOpsSummary(24),
        ]);
        setTenantUsage(allUsage);
        setOps(operations);
      } else {
        setTenantUsage(null);
        setOps(null);
      }
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : '账户数据加载失败');
    } finally {
      setLoading(false);
    }
  }, [apiReady]);

  useEffect(() => {
    void load();
  }, [load]);

  const exportData = async () => {
    try {
      const data = await api.exportAccountData();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `graphrag-export-${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      URL.revokeObjectURL(url);
      toast.success('数据导出已生成');
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : '数据导出失败');
    }
  };

  const deletePersonal = async () => {
    if (!identity || confirmation !== identity.user_id) return;
    if (!window.confirm('确认删除当前用户在 GraphRAG Studio 中保存的数据？登录身份不会被删除。')) return;
    setDeleting(true);
    try {
      await api.deletePersonalData(identity.user_id);
      setConfirmation('');
      toast.success('当前用户的站内数据已删除');
      await load();
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : '删除失败');
    } finally {
      setDeleting(false);
    }
  };

  const deleteTenant = async () => {
    if (!identity || confirmation !== identity.tenant_id) return;
    if (!window.confirm('确认永久删除整个组织空间的数据？此操作不可恢复。')) return;
    setDeleting(true);
    try {
      await api.deleteTenantData(identity.tenant_id);
      setConfirmation('');
      toast.success('组织空间数据已删除');
      await load();
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : '删除失败');
    } finally {
      setDeleting(false);
    }
  };

  const isAdmin = identity?.role === 'admin' || identity?.role === 'owner';

  return (
    <>
      <div className="page-heading flex items-center justify-between mb-6">
        <div>
          <h1 style={{ color: 'var(--text-1)', fontSize: 20, fontWeight: 600 }}>账户与组织</h1>
          <div style={{ color: 'var(--text-4)', fontSize: 12, marginTop: 4 }}>
            跨设备身份、组织空间、角色权限、成本与运维追踪
          </div>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          className="flex items-center gap-2 px-3 py-2 rounded-md cursor-pointer"
          style={{ background: 'var(--bg-s2)', border: '1px solid var(--border-main)', color: 'var(--text-2)', fontSize: 13 }}
        >
          <RefreshCw size={14} /> {loading ? '刷新中…' : '刷新'}
        </button>
      </div>

      <div className="settings-grid grid grid-cols-2 gap-4 mb-4">
        <Card title="当前身份" icon={<UserRound size={17} />}>
          <Field label="用户 ID" value={identity?.user_id ?? '加载中…'} />
          <Field label="角色" value={identity?.role ?? '—'} />
          <Field label="权限" value={identity?.permissions?.length ? identity.permissions.join(', ') : '默认角色权限'} />
        </Card>
        <Card title="当前租户" icon={<Building2 size={17} />}>
          <Field label="租户 ID" value={identity?.tenant_id ?? '加载中…'} />
          <Field label="组织 ID" value={identity?.organization_id ?? '个人空间'} />
          <Field label="组织标识" value={identity?.organization_slug ?? '—'} />
        </Card>
        <UsagePanel usage={usage} title="我的用量" />
        {isAdmin && <UsagePanel usage={tenantUsage} title="组织总用量" />}
      </div>

      {isAdmin && (
        <Card title="运维概览（最近 24 小时）" icon={<ShieldCheck size={17} />}>
          <div className="grid grid-cols-4 gap-3 mb-4">
            {[
              ['事件', ops?.totals.total ?? 0],
              ['错误', ops?.totals.errors ?? 0],
              ['警告', ops?.totals.warnings ?? 0],
              ['独立问题', ops?.totals.unique_issues ?? 0],
            ].map(([label, value]) => (
              <div key={String(label)} className="rounded-md p-3" style={{ background: 'var(--bg-s2)' }}>
                <div style={{ color: 'var(--text-4)', fontSize: 11 }}>{label}</div>
                <div style={{ color: 'var(--text-1)', fontSize: 20, fontWeight: 650, marginTop: 4 }}>{String(value)}</div>
              </div>
            ))}
          </div>
          <div className="flex flex-col gap-2">
            {(ops?.issues ?? []).slice(0, 8).map(issue => (
              <div key={issue.fingerprint} className="rounded-md p-3" style={{ background: 'var(--bg-s2)', fontSize: 12 }}>
                <div className="flex items-center justify-between gap-3">
                  <span style={{ color: issue.severity === 'error' ? 'var(--red)' : 'var(--yellow)', fontWeight: 600 }}>
                    {issue.source} · {issue.event_type}
                  </span>
                  <span style={{ color: 'var(--text-4)' }}>{issue.occurrences} 次</span>
                </div>
                <div style={{ color: 'var(--text-2)', marginTop: 5 }}>{issue.message}</div>
                <div style={{ color: 'var(--text-4)', marginTop: 5 }}>最后出现：{new Date(issue.last_seen).toLocaleString('zh-CN')}</div>
              </div>
            ))}
            {!ops?.issues?.length && <div style={{ color: 'var(--text-4)', fontSize: 12 }}>当前时间窗内没有异常事件。</div>}
          </div>
        </Card>
      )}

      <div className="settings-grid grid grid-cols-2 gap-4 mt-4">
        <Card title="数据导出" icon={<Download size={17} />}>
          <p style={{ color: 'var(--text-3)', fontSize: 12, lineHeight: 1.7, marginBottom: 14 }}>
            导出账户、当前租户数据、用量和审计记录。组织空间仅管理员可导出。
          </p>
          <button type="button" onClick={() => void exportData()} className="px-3 py-2 rounded-md cursor-pointer" style={{ background: 'var(--blue)', color: '#fff', border: 0, fontSize: 12 }}>
            生成 JSON 导出
          </button>
        </Card>

        <Card title="数据删除" icon={<Trash2 size={17} />}>
          <p style={{ color: 'var(--text-3)', fontSize: 12, lineHeight: 1.7 }}>
            输入用户 ID 删除自己的站内数据；组织管理员也可以输入租户 ID 删除整个组织空间。登录身份本身不会被删除。
          </p>
          <input
            value={confirmation}
            onChange={event => setConfirmation(event.target.value)}
            placeholder="输入用户 ID 或租户 ID"
            className="w-full px-3 py-2 rounded-md outline-none mt-3"
            style={{ background: 'var(--bg-s2)', border: '1px solid var(--border-main)', color: 'var(--text-1)', fontSize: 12 }}
          />
          <div className="flex gap-2 mt-3">
            <button
              type="button"
              disabled={deleting || confirmation !== identity?.user_id}
              onClick={() => void deletePersonal()}
              className="px-3 py-2 rounded-md cursor-pointer"
              style={{ background: 'rgba(248,81,73,0.14)', color: 'var(--red)', border: '1px solid rgba(248,81,73,0.3)', fontSize: 12, opacity: confirmation === identity?.user_id ? 1 : 0.45 }}
            >
              删除我的数据
            </button>
            {isAdmin && identity?.organization_id && (
              <button
                type="button"
                disabled={deleting || confirmation !== identity.tenant_id}
                onClick={() => void deleteTenant()}
                className="px-3 py-2 rounded-md cursor-pointer"
                style={{ background: 'rgba(248,81,73,0.14)', color: 'var(--red)', border: '1px solid rgba(248,81,73,0.3)', fontSize: 12, opacity: confirmation === identity.tenant_id ? 1 : 0.45 }}
              >
                删除组织数据
              </button>
            )}
          </div>
        </Card>
      </div>
    </>
  );
}

export function AccountPage() {
  const auth = useAuthRuntime();

  if (!auth.enabled) {
    return (
      <div className="page-shell p-6" style={{ maxWidth: 760, margin: '0 auto' }}>
        <Card title="账户服务尚未启用" icon={<ShieldCheck size={17} />}>
          <p style={{ color: 'var(--text-3)', fontSize: 13, lineHeight: 1.7 }}>
            当前环境未配置正式身份服务，仍可继续使用匿名公开演示。
          </p>
        </Card>
      </div>
    );
  }

  return (
    <div className="page-shell p-6" style={{ maxWidth: 1180, margin: '0 auto' }}>
      {!auth.signedIn && (
        <Card title="登录后使用账户与组织功能" icon={<ShieldCheck size={17} />}>
          <p style={{ color: 'var(--text-3)', fontSize: 13, lineHeight: 1.7, marginBottom: 16 }}>
            登录后可跨设备使用个人或组织空间，并查看按账户统计的模型用量、导出和删除站内数据。
          </p>
          <SignInButton mode="modal">
            <button type="button" className="px-4 py-2 rounded-md cursor-pointer" style={{ background: 'var(--blue)', color: '#fff', border: 0, fontSize: 13, fontWeight: 600 }}>
              登录或注册
            </button>
          </SignInButton>
        </Card>
      )}
      {auth.signedIn && <AccountDashboard apiReady={auth.apiReady} />}
    </div>
  );
}
