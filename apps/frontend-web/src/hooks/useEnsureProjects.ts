import { useEffect, useState } from 'react';
import { useProjectStore, loadProjects } from '../stores/project-store';

/** For routes outside AuthenticatedApp (console pages, legacy redirects): load
 * the project list on a cold browser load. True once projects are available. */
export function useEnsureProjects(): boolean {
  const have = useProjectStore((s) => s.projects.length > 0);
  const [done, setDone] = useState(have);
  useEffect(() => {
    if (have) {
      setDone(true);
      return;
    }
    let cancelled = false;
    void loadProjects().finally(() => { if (!cancelled) setDone(true); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- load once on mount
  }, []);
  return done;
}
