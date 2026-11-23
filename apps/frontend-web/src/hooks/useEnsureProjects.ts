import { useEffect, useState } from 'react';
import { loadProjects, useProjectStore } from '../stores/project-store';

export function useEnsureProjects(): { ready: boolean; error: string | null; retry: () => void } {
  const error = useProjectStore((state) => state.error);
  const [ready, setReady] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    void loadProjects().finally(() => { if (!cancelled) setReady(true); });
    return () => { cancelled = true; };
  }, [attempt]);

  return { ready, error, retry: () => { setReady(false); setAttempt((n) => n + 1); } };
}
