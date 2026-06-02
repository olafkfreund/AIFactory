/**
 * ObservabilityTab — Hosts the task-scoped observability panels:
 *   1. Token-usage breakdown  (GET /api/tasks/{id}/token-usage)
 *   2. Agent inbox / message-send box (GET+POST /api/tasks/{id}/inbox)
 *   3. Resource usage (CPU/RAM) panel (GET /api/tasks/{id}/resource-usage, ~3 s poll)
 *
 * Added for issue #277 — task-detail observability panels.
 */

import { useTranslation } from 'react-i18next';
import { ScrollArea } from '../ui/scroll-area';
import { Separator } from '../ui/separator';
import { TokenUsagePanel } from './TokenUsagePanel';
import { AgentInboxPanel } from './AgentInboxPanel';
import { ResourceUsagePanel } from './ResourceUsagePanel';

interface ObservabilityTabProps {
  /** Composite task ID (projectId:specId) as the backend expects. */
  taskId: string;
}

export function ObservabilityTab({ taskId }: ObservabilityTabProps) {
  const { t } = useTranslation(['tasks']);

  return (
    <ScrollArea className="h-full">
      <div className="p-5 space-y-6">

        {/* ── 1. Token usage ── */}
        <section aria-labelledby="obs-token-heading">
          <h3
            id="obs-token-heading"
            className="text-xs font-medium uppercase tracking-wide text-muted-foreground mb-3"
          >
            {t('tasks:tokenUsage.title')}
          </h3>
          <TokenUsagePanel taskId={taskId} />
        </section>

        <Separator />

        {/* ── 2. Resource usage ── */}
        <section aria-labelledby="obs-resource-heading">
          <h3
            id="obs-resource-heading"
            className="text-xs font-medium uppercase tracking-wide text-muted-foreground mb-3"
          >
            {t('tasks:resourceUsage.title')}
          </h3>
          <ResourceUsagePanel taskId={taskId} />
        </section>

        <Separator />

        {/* ── 3. Agent inbox ── */}
        <section aria-labelledby="obs-inbox-heading">
          <h3
            id="obs-inbox-heading"
            className="text-xs font-medium uppercase tracking-wide text-muted-foreground mb-3"
          >
            {t('tasks:agentInbox.title')}
          </h3>
          <AgentInboxPanel taskId={taskId} />
        </section>

      </div>
    </ScrollArea>
  );
}
