import { createBrowserRouter, Navigate } from 'react-router';
import { AppLayout } from './components/layout/AppLayout';

export const router = createBrowserRouter([
  {
    path: '/',
    Component: AppLayout,
    children: [
      { index: true, element: <Navigate to="/dashboard" replace /> },
      { path: 'dashboard', lazy: async () => ({ Component: (await import('./components/pages/Dashboard')).Dashboard }) },
      { path: 'documents', lazy: async () => ({ Component: (await import('./components/pages/Documents')).Documents }) },
      { path: 'graph', lazy: async () => ({ Component: (await import('./components/pages/KGExplorer')).KGExplorer }) },
      { path: 'chat', lazy: async () => ({ Component: (await import('./components/pages/QAChat')).QAChat }) },
      { path: 'search', lazy: async () => ({ Component: (await import('./components/pages/SearchPage')).SearchPage }) },
      { path: 'settings', lazy: async () => ({ Component: (await import('./components/pages/SettingsPage')).SettingsPage }) },
      { path: 'account', lazy: async () => ({ Component: (await import('./components/pages/AccountPage')).AccountPage }) },
    ],
  },
]);
