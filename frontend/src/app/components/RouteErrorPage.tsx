import React from 'react';
import { AlertTriangle, Home, RefreshCw } from 'lucide-react';
import { isRouteErrorResponse, useRouteError } from 'react-router';

function errorMessage(error: unknown) {
  if (isRouteErrorResponse(error)) return error.statusText || `请求失败 (${error.status})`;
  if (error instanceof Error) return error.message;
  return '页面遇到了暂时无法处理的数据';
}

export function RouteErrorPage() {
  const error = useRouteError();

  return (
    <main className="min-h-screen flex items-center justify-center p-6" style={{ background: 'var(--bg-base)', color: 'var(--text-1)' }}>
      <section className="w-full max-w-lg rounded-xl p-6" style={{ background: 'var(--bg-s1)', border: '1px solid var(--border-main)' }}>
        <AlertTriangle size={28} style={{ color: 'var(--yellow)', marginBottom: 16 }} />
        <h1 style={{ fontSize: 20, fontWeight: 600, marginBottom: 8 }}>页面暂时无法显示</h1>
        <p style={{ color: 'var(--text-3)', fontSize: 13, lineHeight: 1.6, marginBottom: 20 }}>
          {errorMessage(error)}。你可以重新加载页面；如果问题持续存在，请返回仪表盘后重试。
        </p>
        <div className="flex gap-3">
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="flex items-center gap-2 rounded-md px-3 py-2 cursor-pointer"
            style={{ border: 0, background: 'var(--blue)', color: 'var(--on-blue)', fontSize: 13 }}
          >
            <RefreshCw size={14} /> 重新加载
          </button>
          <a
            href="/dashboard"
            className="flex items-center gap-2 rounded-md px-3 py-2"
            style={{ border: '1px solid var(--border-main)', color: 'var(--text-2)', fontSize: 13, textDecoration: 'none' }}
          >
            <Home size={14} /> 返回仪表盘
          </a>
        </div>
      </section>
    </main>
  );
}
