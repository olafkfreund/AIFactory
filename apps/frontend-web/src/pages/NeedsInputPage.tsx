import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { NeedsInputBanner } from '../components/task-detail/NeedsInputBanner';
import { projectPath, projectSlugs } from '../lib/project-route';
import { useProjectStore } from '../stores/project-store';
import { useTaskStore } from '../stores/task-store';

export function NeedsInputPage() {
  const { t } = useTranslation('tasks');
  const projects = useProjectStore((state) => state.projects);
  const tasks = useTaskStore((state) => state.tasks);
  const slugs = projectSlugs(projects);
  const pending = tasks.filter((task) => task.status === 'human_review' && task.reviewReason === 'needs_input' && projects.some((project) => project.id === task.projectId));

  return <main className="h-full overflow-auto p-6 space-y-5">
    <h1 className="text-xl font-semibold">{t('needsInput.pageTitle')}</h1>
    {pending.length === 0 && <p>{t('needsInput.empty')}</p>}
    {pending.map((task) => {
      const project = projects.find((item) => item.id === task.projectId)!;
      const slug = slugs.get(project.id) ?? project.id;
      return <article key={`${project.id}:${task.id}`} className="space-y-3">
        <p className="text-sm text-muted-foreground">{project.name}</p>
        <Link className="font-medium underline" to={`${projectPath(slug)}/tasks/${encodeURIComponent(task.id)}`}>{task.title}</Link>
        <NeedsInputBanner taskId={task.id} />
      </article>;
    })}
  </main>;
}
