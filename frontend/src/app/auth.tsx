import { ClerkProvider, useAuth } from '@clerk/react';
import React, { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { setAuthTokenProvider } from './api';

interface AuthRuntimeState {
  enabled: boolean;
  loaded: boolean;
  signedIn: boolean;
  apiReady: boolean;
}

const anonymousState: AuthRuntimeState = {
  enabled: false,
  loaded: true,
  signedIn: false,
  apiReady: true,
};

const AuthRuntimeContext = createContext<AuthRuntimeState>(anonymousState);

function runtimePublishableKey(): string {
  const meta = document.querySelector<HTMLMetaElement>('meta[name="clerk-publishable-key"]');
  return (import.meta.env.VITE_CLERK_PUBLISHABLE_KEY || meta?.content || '').trim();
}

function ClerkRuntimeBridge({ children }: { children: ReactNode }) {
  const { getToken, isLoaded, isSignedIn } = useAuth();
  const [apiReady, setApiReady] = useState(false);

  useEffect(() => {
    if (!isLoaded) return;
    setAuthTokenProvider(isSignedIn ? () => getToken() : null);
    setApiReady(true);
    return () => {
      setAuthTokenProvider(null);
      setApiReady(false);
    };
  }, [getToken, isLoaded, isSignedIn]);

  const value = useMemo<AuthRuntimeState>(() => ({
    enabled: true,
    loaded: isLoaded,
    signedIn: Boolean(isSignedIn),
    apiReady,
  }), [apiReady, isLoaded, isSignedIn]);

  return <AuthRuntimeContext.Provider value={value}>{children}</AuthRuntimeContext.Provider>;
}

export function AppAuthProvider({ children }: { children: ReactNode }) {
  const publishableKey = runtimePublishableKey();
  if (!publishableKey || publishableKey.startsWith('%VITE_')) {
    return <AuthRuntimeContext.Provider value={anonymousState}>{children}</AuthRuntimeContext.Provider>;
  }

  return (
    <ClerkProvider publishableKey={publishableKey}>
      <ClerkRuntimeBridge>{children}</ClerkRuntimeBridge>
    </ClerkProvider>
  );
}

export function useAuthRuntime() {
  return useContext(AuthRuntimeContext);
}
