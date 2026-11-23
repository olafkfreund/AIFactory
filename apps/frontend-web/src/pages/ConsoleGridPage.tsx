import { useEffect, useMemo, type ReactNode } from 'react';
import { useParams, Link } from 'react-router';
import { ArrowLeft, Maximize2, MonitorPlay } from 'lucide-react';
import { AgentConsole } from '../components/task-detail/AgentConsole';
import { useProjectStore } from '../stores/project-store';
import { useTaskStore, loadTasks } from '../stores/task-store';
import { useEnsureProjects } from '../hooks/useEnsureProjects';
import { findProjectByRef, projectSlug } from '../lib/project-route';
import { useTranslation } from 'react-i18next';

/**
 * Standalone deep-link page showing EVERY active agent's Live Console for a
 * project at once, in a responsive grid.
 *
 * URL pattern: ``/p/:slug/console`` (legacy ``/console/:projectId`` redirects here)
 */
export function ConsoleGridPage(): ReactNode {
  const { slug } = useParams<{ slug: string }>();
  const { ready, error, retry } = useEnsureProjects();
  const { t } = useTranslation('welcome');
  const projects = useProjectStore((s) => s.projects);
  const project = ready && !error && slug ? findProjectByRef(projects, slug) : undefined;
  const projectId = project?.id;
  const tasks = useTaskStore((s) => s.tasks);

  useEffect(() => {
    if (projectId) void loadTasks(projectId);
  }, [projectId]);

  // "Active" = currently doing work: running, or in AI review (QA agents).
  const active = useMemo(
    () =>
      tasks.filter(
        (tItem) =>
          tItem.projectId === projectId &&
          (tItem.status === 'in_progress' || tItem.status === 'ai_review')
      ),
    [tasks, projectId]
  );

  if (!slug) {
    return (
      <div className="flex h-screen items-center justify-center text-muted-foreground">
        <p>
          Invalid console URL. Expected{' '}
          <code className="text-xs bg-muted px-1 rounded">/p/&lt;project&gt;/console</code>.
        </p>
      </div>
    );
  }

  if (!ready) return null;
  if (error) return <div role="alert">{error} <button onClick={retry}>{t('projectsPage.retry')}</button></div>;
  if (!projectId) return <div>{t('projectsPage.notFound', { ref: slug })}</div>;

  return (
    <div className="flex h-screen flex-col bg-background">
      <header className="flex items-center gap-3 border-b border-border px-4 py-2 text-sm">
        <Link
          to={project ? `/p/${encodeURIComponent(projectSlug(projects, project))}` : '/'}
          className="flex items-center gap-1 text-muted-foreground hover:text-foreground"
          title="Back to portal"
        >
          <ArrowLeft className="h-4 w-4" />
          <span>Back to portal</span>
        </Link>
        <span className="text-border">·</span>
        <MonitorPlay className="h-4 w-4 text-primary" />
        <span className="font-medium text-foreground">Live Consoles</span>
        <span className="text-border">·</span>
        <span className="text-muted-foreground">{project?.name ?? projectId.slice(0, 8)}</span>
        <span className="ml-auto flex items-center gap-1.5 text-xs text-muted-foreground">
          <span
            className={active.length ? 'live-dot inline-flex h-1.5 w-1.5 rounded-full bg-success' : 'inline-flex h-1.5 w-1.5 rounded-full bg-muted-foreground/40'}
          />
          {active.length} active
        </span>
      </header>

      {active.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 text-center">
          <MonitorPlay className="h-10 w-10 text-muted-foreground/40" />
          <p className="text-sm text-muted-foreground">No agents are running right now</p>
          <p className="max-w-sm text-xs text-muted-foreground/60">
            Start a task and its live console will appear here. This page auto-updates as
            agents start and finish.
          </p>
        </div>
      ) : (
        <main className="flex-1 overflow-auto p-3">
          <div className="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(420px,1fr))]">
            {active.map((task) => (
              <div
                key={task.id}
                className="flex h-[360px] flex-col overflow-hidden rounded-xl border border-border bg-card"
              >
                <div className="flex items-center gap-2 border-b border-border px-3 py-1.5">
                  <span className="live-dot inline-flex h-1.5 w-1.5 shrink-0 rounded-full bg-success" />
                  <span className="truncate text-xs font-medium text-foreground" title={task.title}>
                    {task.title}
                  </span>
                  <code className="ml-1 shrink-0 rounded bg-muted px-1 py-0.5 font-mono text-[10px] text-muted-foreground">
                    {task.specId}
                  </code>
                  <Link
                    to={`/p/${encodeURIComponent(project ? projectSlug(projects, project) : task.projectId)}/console/${encodeURIComponent(task.specId)}`}
                    className="ml-auto shrink-0 text-muted-foreground hover:text-foreground"
                    title="Open this console fullscreen"
                  >
                    <Maximize2 className="h-3.5 w-3.5" />
                  </Link>
                </div>
                <div className="min-h-0 flex-1 overflow-hidden">
                  <AgentConsole taskId={task.id} />
                </div>
              </div>
            ))}
          </div>
        </main>
      )}
    </div>
  );
}
