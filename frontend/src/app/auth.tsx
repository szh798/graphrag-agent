import { ClerkProvider, useAuth } from '@clerk/react';
import React, { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { api, setAuthTokenProvider } from './api';

interface AuthRuntimeState {
  enabled: boolean;
  loaded: boolean;
  signedIn: boolean;
  apiReady: boolean;
  identityKey: string;
  environment: 'disabled' | 'development' | 'production';
}

const anonymousState: AuthRuntimeState = {
  enabled: false,
  loaded: true,
  signedIn: false,
  apiReady: true,
  identityKey: 'anonymous',
  environment: 'disabled',
};

const AuthRuntimeContext = createContext<AuthRuntimeState>(anonymousState);

function runtimePublishableKey(): string {
  const meta = document.querySelector<HTMLMetaElement>('meta[name="clerk-publishable-key"]');
  return (import.meta.env.VITE_CLERK_PUBLISHABLE_KEY || meta?.content || '').trim();
}

function ClerkRuntimeBridge({ children, environment }: { children: ReactNode; environment: 'development' | 'production' }) {
  const { getToken, isLoaded, isSignedIn, orgId, userId } = useAuth();
  const identityKey = !isLoaded
    ? 'loading'
    : isSignedIn
      ? `account:${userId}:${orgId ?? 'personal'}`
      : 'anonymous';
  const [preparedIdentityKey, setPreparedIdentityKey] = useState('');

  useEffect(() => {
    if (!isLoaded) return;
    let cancelled = false;
    setAuthTokenProvider(isSignedIn ? () => getToken() : null);

    const prepareIdentity = async () => {
      if (isSignedIn) {
        try {
          await api.claimVisitorData();
        } catch (error) {
          console.warn('Visitor data claim failed:', error);
        }
      }
      if (!cancelled) setPreparedIdentityKey(identityKey);
    };
    void prepareIdentity();

    return () => {
      cancelled = true;
      setAuthTokenProvider(null);
    };
  }, [getToken, identityKey, isLoaded, isSignedIn]);

  const value = useMemo<AuthRuntimeState>(() => ({
    enabled: true,
    loaded: isLoaded,
    signedIn: Boolean(isSignedIn),
    apiReady: isLoaded && preparedIdentityKey === identityKey,
    identityKey,
    environment,
  }), [environment, identityKey, isLoaded, isSignedIn, preparedIdentityKey]);

  return <AuthRuntimeContext.Provider value={value}>{children}</AuthRuntimeContext.Provider>;
}

export function AppAuthProvider({ children }: { children: ReactNode }) {
  const publishableKey = runtimePublishableKey();
  if (
    !publishableKey ||
    publishableKey === 'undefined' ||
    publishableKey === 'null' ||
    publishableKey.startsWith('%VITE_') ||
    publishableKey.startsWith('__CLERK_')
  ) {
    return <AuthRuntimeContext.Provider value={anonymousState}>{children}</AuthRuntimeContext.Provider>;
  }

  return (
    <ClerkProvider publishableKey={publishableKey}>
      <ClerkRuntimeBridge environment={publishableKey.startsWith('pk_live_') ? 'production' : 'development'}>{children}</ClerkRuntimeBridge>
    </ClerkProvider>
  );
}

export function useAuthRuntime() {
  return useContext(AuthRuntimeContext);
}
