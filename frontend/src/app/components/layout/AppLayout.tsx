import React, { useState } from 'react';
import { Outlet } from 'react-router';
import { Toaster } from 'sonner';
import { Header } from './Header';
import { Sidebar } from './Sidebar';
import { StatusBar } from './StatusBar';
import { useAppState, AppProvider } from '../../store';

function AppLayoutInner() {
  const { sidebarCollapsed, setSidebarCollapsed } = useAppState();
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);

  const handleMenuClick = () => {
    if (window.matchMedia('(max-width: 840px)').matches) {
      setMobileSidebarOpen(open => !open);
    } else {
      setSidebarCollapsed(!sidebarCollapsed);
    }
  };

  return (
    <div
      className="app-shell"
      style={{
        '--sidebar-width': `${sidebarCollapsed ? 72 : 220}px`,
      } as React.CSSProperties}
    >
      <Header onMenuClick={handleMenuClick} />
      <Sidebar
        mobileOpen={mobileSidebarOpen}
        onNavigate={() => setMobileSidebarOpen(false)}
      />
      {mobileSidebarOpen && (
        <button
          type="button"
          className="app-sidebar-backdrop"
          aria-label="关闭导航"
          onClick={() => setMobileSidebarOpen(false)}
        />
      )}
      <main
        className="app-main"
        style={{
          gridArea: 'main',
          overflowY: 'auto',
          background: 'var(--bg-base)',
        }}
      >
        <Outlet />
      </main>
      <StatusBar />
    </div>
  );
}

export function AppLayout() {
  return (
    <AppProvider>
      <AppLayoutInner />
      <Toaster position="top-right" theme="dark" richColors />
    </AppProvider>
  );
}
