/**
 * Login page for web UI
 */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuthStore } from '../stores/auth-store';
import { getAuthToken } from '../lib/auth';

export function LoginPage() {
  // Pre-fill with existing token from localStorage (if any)
  const [token, setToken] = useState(() => getAuthToken() || '');
  const { login, isLoading, error } = useAuthStore();
  const navigate = useNavigate();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const success = await login(token);
    if (success) {
      navigate('/');
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-background">
      <div className="w-full max-w-md p-8">
        <div className="text-center mb-8">
          <h1 className="text-3xl font-bold text-foreground">AIFactory</h1>
          <p className="text-muted-foreground mt-2">
            Enter your API token to continue
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-6">
          <div>
            <label
              htmlFor="token"
              className="block text-sm font-medium text-foreground mb-2"
            >
              API Token
            </label>
            <input
              id="token"
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="Enter your token"
              className="w-full px-4 py-3 rounded-lg border border-border bg-card text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary"
              required
            />
            <p className="mt-2 text-xs text-muted-foreground">
              Set your token in the server's .env file (APP_API_TOKEN)
            </p>
          </div>

          {error && (
            <div className="p-3 rounded-lg bg-destructive/10 border border-destructive/50 text-destructive text-sm">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={isLoading || !token}
            className="w-full py-3 px-4 bg-primary text-primary-foreground rounded-lg font-medium hover:bg-primary/90 focus:outline-none focus:ring-2 focus:ring-primary focus:ring-offset-2 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {isLoading ? 'Authenticating...' : 'Login'}
          </button>
        </form>

        {/*
          Epic #26 P3.7 — OIDC SSO button. The backend's
          /api/auth/oidc/login endpoint either:
          - issues a 302 to the IdP (when APP_OIDC_ENABLED=true and a
            valid issuer URL is configured), OR
          - returns 404 (when OIDC isn't configured on this install).
          We show the button unconditionally and let the browser
          follow the redirect chain — operators who haven't wired
          OIDC see a friendly 404 message instead of a non-functional
          UI element.
        */}
        <div className="mt-6 pt-6 border-t border-border">
          <button
            type="button"
            onClick={() => {
              window.location.href = '/api/auth/oidc/login';
            }}
            className="w-full py-3 px-4 bg-card text-foreground border border-border rounded-lg font-medium hover:bg-card/80 focus:outline-none focus:ring-2 focus:ring-primary focus:ring-offset-2 transition-colors flex items-center justify-center gap-2"
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
              <path d="M7 11V7a5 5 0 0 1 10 0v4" />
            </svg>
            Sign in with SSO
          </button>
          <p className="mt-2 text-xs text-muted-foreground text-center">
            Single sign-on via your organization's identity provider
          </p>
        </div>
      </div>
    </div>
  );
}
