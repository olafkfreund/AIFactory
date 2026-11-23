import type { ReactNode } from 'react';
import { useParams, Link } from 'react-router';
import { ArrowLeft, LayoutGrid } from 'lucide-react';
import { AgentConsole } from '../components/task-detail/AgentConsole';
import { useProjectStore } from '../stores/project-store';
import { useEnsureProjects } from '../hooks/useEnsureProjects';
import { findProjectByRef, projectSlug } from '../lib/project-route';
import { useTranslation } from 'react-i18next';

/**
 * Standalone deep-link page that drops you straight into the rmux Live
 * Agent Console for a given task, no portal chrome / kanban detour.
 *
 * URL pattern: ``/p/:slug/console/:specId`` (legacy ``/console/:projectId/:specId`` redirects here)
 */
export function ConsolePage(): ReactNode {
  const { slug, specId } = useParams<{ slug: string; specId: string }>();
  const { ready, error, retry } = useEnsureProjects();
  const { t } = useTranslation('welcome');
  const projects = useProjectStore((s) => s.projects);
  const project = ready && !error && slug ? findProjectByRef(projects, slug) : undefined;
  const projectId = project?.id;
  const slugOrRef = project ? projectSlug(projects, project) : slug;

  if (!slug || !specId) {
    return (
      <div className="flex h-screen items-center justify-center text-muted-foreground">
        <p>
          Invalid console URL. Expected{' '}
          <code className="text-xs bg-muted px-1 rounded">
            /p/&lt;project&gt;/console/&lt;specId&gt;
          </code>
          .
        </p>
      </div>
    );
  }

  if (!ready) return null;
  if (error) return <div role="alert">{error} <button onClick={retry}>{t('projectsPage.retry')}</button></div>;
  if (!projectId) return <div>{t('projectsPage.notFound', { ref: slug })}</div>;

  const taskId = `${projectId}:${specId}`;

  return (
    <div className="flex h-screen flex-col bg-background">
      <header className="flex items-center gap-3 border-b border-border px-4 py-2 text-sm">
        <Link
          to={project ? `/p/${encodeURIComponent(slugOrRef ?? '')}` : '/'}
          className="flex items-center gap-1 text-muted-foreground hover:text-foreground"
          title="Back to portal"
        >
          <ArrowLeft className="h-4 w-4" />
          <span>Back to portal</span>
        </Link>
        <span className="text-border">·</span>
        <span className="text-muted-foreground">
          {project?.name ?? projectId.slice(0, 8)}
        </span>
        <span className="text-border">·</span>
        <code className="text-xs bg-muted px-1.5 py-0.5 rounded font-mono">
          {specId}
        </code>
        <Link
          to={`/p/${encodeURIComponent(slugOrRef ?? '')}/console`}
          className="ml-auto flex items-center gap-1 text-muted-foreground hover:text-foreground"
          title="View all active consoles for this project"
        >
          <LayoutGrid className="h-4 w-4" />
          <span>All consoles</span>
        </Link>
      </header>
      <main className="flex-1 overflow-hidden">
        <AgentConsole taskId={taskId} />
      </main>
    </div>
  );
}
