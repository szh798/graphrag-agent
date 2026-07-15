import React, { useState, useRef, useEffect } from 'react';
import { useNavigate } from 'react-router';
import { Menu, Search, X } from 'lucide-react';
import { OrganizationSwitcher, SignInButton, UserButton, useAuth } from '@clerk/react';
import { type KGNode } from '../../store';
import { api } from '../../api';
import { useAuthRuntime } from '../../auth';
import { TYPE_COLORS } from '../../mock-data';

function AccountControls() {
  const { isLoaded, isSignedIn } = useAuth();
  const organizationsEnabled = import.meta.env.VITE_CLERK_ORGANIZATIONS_ENABLED === 'true';
  if (!isLoaded) return null;
  return (
    <div className="flex items-center gap-2" style={{ marginLeft: 'auto' }}>
      {!isSignedIn && (
        <SignInButton mode="modal">
          <button
            type="button"
            className="px-3 py-1.5 rounded-md cursor-pointer"
            style={{ color: 'var(--on-blue)', background: 'var(--blue)', border: 0, fontSize: 12, fontWeight: 600 }}
          >
            登录
          </button>
        </SignInButton>
      )}
      {isSignedIn && (
        <>
          {organizationsEnabled && <OrganizationSwitcher />}
          <UserButton />
        </>
      )}
    </div>
  );
}

export function Header({ onMenuClick }: { onMenuClick: () => void }) {
  const [query, setQuery] = useState('');
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [suggestions, setSuggestions] = useState<KGNode[]>([]);
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const auth = useAuthRuntime();

  useEffect(() => {
    if (query.length >= 2) {
      clearTimeout(timerRef.current);
      timerRef.current = setTimeout(async () => {
        try {
          const res = await api.searchEntities(query, undefined, 5);
          setSuggestions(res.items.map(n => ({
            id: n.id, name: n.name, type: n.type as KGNode['type'],
            page: n.page, confidence: n.confidence as KGNode['confidence'],
            degree: n.degree, centrality: 0, doc_id: n.source_doc,
          })));
          setShowSuggestions(true);
        } catch {
          setSuggestions([]);
        }
      }, 300);
    } else {
      setSuggestions([]);
      setShowSuggestions(false);
    }
    return () => clearTimeout(timerRef.current);
  }, [query]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (query.trim()) {
      setShowSuggestions(false);
      navigate(`/search?q=${encodeURIComponent(query)}`);
    }
  };

  return (
    <header
      className="app-header flex items-center px-4 gap-4"
      style={{
        gridArea: 'header',
        height: 56,
        background: 'var(--bg-s1)',
        borderBottom: '1px solid var(--border-main)',
        position: 'sticky',
        top: 0,
        zIndex: 100,
      }}
    >
      {/* Left */}
      <button
        onClick={onMenuClick}
        className="p-1.5 rounded-md hover:opacity-80 cursor-pointer"
        style={{ background: 'var(--bg-s2)', color: 'var(--text-3)' }}
        aria-label="打开或收起导航"
      >
        <Menu size={18} />
      </button>
      <span className="app-brand" style={{ color: 'var(--blue)', fontSize: 16, fontWeight: 600, whiteSpace: 'nowrap' }}>
        GraphRAG Studio
      </span>
      <span
        className="app-demo-badge px-2 py-0.5 rounded-full"
        style={{
          color: 'var(--green)',
          background: 'rgba(63,185,80,0.1)',
          border: '1px solid rgba(63,185,80,0.25)',
          fontSize: 11,
          fontWeight: 600,
          whiteSpace: 'nowrap',
        }}
      >
        公开演示 · 可上传并索引
      </span>

      {/* Center - Search */}
      <form onSubmit={handleSubmit} className="app-global-search flex-1 flex justify-center relative" style={{ maxWidth: 400, margin: '0 auto' }}>
        <div className="relative w-full">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: 'var(--text-4)' }} />
          <input
            ref={inputRef}
            value={query}
            onChange={e => setQuery(e.target.value)}
            onFocus={() => query.length >= 3 && setShowSuggestions(true)}
            onBlur={() => setTimeout(() => setShowSuggestions(false), 200)}
            placeholder="搜索实体..."
            className="w-full pl-9 pr-8 py-1.5 rounded-md outline-none"
            style={{
              background: 'var(--bg-s2)',
              border: '1px solid var(--border-main)',
              color: 'var(--text-1)',
              fontSize: 13,
            }}
          />
          {query && (
            <button type="button" aria-label="清空搜索" onClick={() => { setQuery(''); setShowSuggestions(false); }} className="absolute right-2 top-1/2 -translate-y-1/2 cursor-pointer" style={{ color: 'var(--text-4)' }}>
              <X size={14} />
            </button>
          )}
        </div>
        {showSuggestions && suggestions.length > 0 && (
          <div
            className="absolute top-full mt-1 w-full rounded-md overflow-hidden"
            style={{ background: 'var(--bg-s3)', border: '1px solid var(--border-main)', boxShadow: 'var(--shadow-md)', zIndex: 200 }}
          >
            {suggestions.map(s => (
              <button
                key={s.id}
                type="button"
                className="w-full flex items-center gap-2 px-3 py-2 hover:opacity-80 cursor-pointer text-left"
                style={{ background: 'transparent', borderBottom: '1px solid var(--border-muted)' }}
                onMouseDown={() => {
                  setShowSuggestions(false);
                  setQuery('');
                  navigate(`/graph?node=${s.id}`);
                }}
              >
                <span style={{ color: 'var(--text-1)', fontSize: 13 }}>{s.name}</span>
                <span
                  className="px-1.5 py-0.5 rounded"
                  style={{
                    fontSize: 10, fontWeight: 600,
                    background: `${TYPE_COLORS[s.type]}20`,
                    color: TYPE_COLORS[s.type],
                  }}
                >
                  {s.type}
                </span>
              </button>
            ))}
          </div>
        )}
      </form>
      {auth.enabled && <AccountControls />}
    </header>
  );
}
