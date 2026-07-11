import { useEffect, useRef, useState } from 'react';
import { RotateCw, ExternalLink, Monitor, Globe } from 'lucide-react';
import { Button } from './ui/button';
import { cn } from '../lib/utils';

const STORAGE_KEY = 'mission-control:preview-url';
const DEFAULT_URL = 'http://localhost:3000';
const PORT_PRESETS = [3000, 5173, 8080, 4173, 8000];

/**
 * LivePreviewPane — renders a running app inside an iframe so the operator can
 * SEE the result of a build (the headline feature of Lovable/Bolt/Replit).
 *
 * Prototype scope: the URL is entered/picked manually (or defaults to a common
 * dev-server port). Auto-starting the worktree's dev server + auto-detecting its
 * port is a backend follow-up — this pane is the front-end surface for it.
 */
export function LivePreviewPane() {
  const [url, setUrl] = useState(() => {
    try {
      return localStorage.getItem(STORAGE_KEY) || DEFAULT_URL;
    } catch {
      return DEFAULT_URL;
    }
  });
  const [draft, setDraft] = useState(url);
  const [loaded, setLoaded] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, url);
    } catch {
      // localStorage may be unavailable
    }
  }, [url]);

  const navigate = (next: string) => {
    const trimmed = next.trim();
    if (!trimmed) return;
    const withScheme = /^https?:\/\//i.test(trimmed) ? trimmed : `http://${trimmed}`;
    setDraft(withScheme);
    setUrl(withScheme);
    setLoaded(false);
    setReloadKey((k) => k + 1);
  };

  const setPort = (port: number) => { navigate(`http://localhost:${port}`); };
  const reload = () => {
    setLoaded(false);
    setReloadKey((k) => k + 1);
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* URL / address bar */}
      <div className="flex items-center gap-1.5 border-b border-border px-2 py-1.5">
        <Button variant="ghost" size="icon" className="h-7 w-7 shrink-0" onClick={reload} title="Reload">
          <RotateCw className="h-3.5 w-3.5" />
        </Button>
        <div className="flex min-w-0 flex-1 items-center gap-1.5 rounded-md border border-border bg-background px-2">
          <Globe className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          <input
            ref={inputRef}
            value={draft}
            onChange={(e) => { setDraft(e.target.value); }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') navigate(draft);
            }}
            spellCheck={false}
            placeholder="http://localhost:3000"
            className="min-w-0 flex-1 bg-transparent py-1 text-xs text-foreground outline-none placeholder:text-muted-foreground/60"
          />
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 shrink-0"
          onClick={() => window.open(url, '_blank', 'noopener,noreferrer')}
          title="Open in new tab"
        >
          <ExternalLink className="h-3.5 w-3.5" />
        </Button>
      </div>

      {/* Port presets */}
      <div className="flex items-center gap-1 border-b border-border px-2 py-1">
        <span className="mr-1 text-[10px] uppercase tracking-wide text-muted-foreground/70">Ports</span>
        {PORT_PRESETS.map((port) => {
          const active = url === `http://localhost:${port}`;
          return (
            <button
              key={port}
              onClick={() => { setPort(port); }}
              className={cn(
                'rounded px-1.5 py-0.5 text-[10px] font-medium tabular-nums transition-colors',
                active
                  ? 'bg-primary/15 text-primary'
                  : 'text-muted-foreground hover:bg-muted hover:text-foreground'
              )}
            >
              {port}
            </button>
          );
        })}
      </div>

      {/* Preview surface */}
      <div className="relative flex-1 min-h-0 bg-background">
        {!loaded && (
          <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center gap-2 text-center">
            <Monitor className="h-8 w-8 text-muted-foreground/40" />
            <p className="text-sm text-muted-foreground">Loading {url}…</p>
            <p className="max-w-xs text-xs text-muted-foreground/60">
              Start your app's dev server, then pick its port above. Nothing showing means no server
              is listening there yet.
            </p>
          </div>
        )}
        <iframe
          key={reloadKey}
          src={url}
          title="Live app preview"
          className="h-full w-full border-0 bg-white"
          sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-modals"
          onLoad={() => { setLoaded(true); }}
        />
      </div>
    </div>
  );
}
