import { useState, useEffect, useRef, useCallback, memo, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from '../hooks/use-toast';
import {
  formatRelativeTime,
  sanitizeMarkdownForDisplay,
  extractTaskNumber,
  formatTaskTitleWithNumber,
} from '../lib/utils';
import { PhaseProgressIndicator } from './PhaseProgressIndicator';
import { CodingRobot } from './CodingRobot';
import {
  TASK_CATEGORY_LABELS,
  TASK_COMPLEXITY_LABELS,
  TASK_IMPACT_LABELS,
  TASK_PRIORITY_LABELS,
  EXECUTION_PHASE_LABELS,
  reviewReasonBadge,
} from '../shared/constants';
import {
  startTask,
  stopTask,
  checkTaskRunning,
  recoverStuckTask,
  isIncompleteHumanReview,
  archiveTasks,
} from '../stores/task-store';
import {
  WorkCard,
  type WorkAction,
  type WorkCardLabels,
  type WorkPhase,
  type WorkTone,
} from './factory-ui/WorkCard';
import type { Task, ReviewReason } from '../shared/types';

export interface TaskCardProps {
  task: Task;
  onClick: () => void;
  expanded?: boolean | undefined;
  onToggle?: (() => void) | undefined;
  collapsedByDefault?: boolean | undefined;
}

const PLAN_PHASES = new Set(['spec_creation', 'planning', 'plan_review']);
const QA_PHASES = new Set(['qa_review', 'qa_fixing']);

export function isTaskDone(task: Task): boolean {
  if (task.status === 'done') return true;
  if (task.reviewReason === 'completed') return true;
  return task.executionProgress?.phase === 'complete';
}

export function isTaskFailed(task: Task): boolean {
  if (task.status === 'human_review' && (task.reviewReason === 'needs_input' || task.reviewReason === 'approval_rejected')) return false;
  return (
    task.reviewReason === 'errors' ||
    task.reviewReason === 'qa_rejected' ||
    task.reviewReason === 'no_work' ||
    task.reviewReason === 'pr_closed' ||
    task.executionProgress?.phase === 'failed'
  );
}

export function isTaskEmptyOutcome(task: Task): boolean {
  return task.reviewReason === 'no_work';
}

export function buildTaskPhases(task: Task): WorkPhase[] {
  const phase = task.executionProgress?.phase;
  const done = isTaskDone(task);
  const failed = isTaskFailed(task);

  const planState: WorkPhase['state'] =
    done || phase === 'coding' || (phase && QA_PHASES.has(phase)) || task.status === 'ai_review' || task.status === 'human_review' || task.status === 'done'
      ? 'done'
      : phase && PLAN_PHASES.has(phase)
        ? 'active'
        : task.status === 'backlog' && !phase
          ? 'pending'
          : 'done';

  const codeState: WorkPhase['state'] =
    done || (phase && QA_PHASES.has(phase)) || task.status === 'ai_review' || task.status === 'human_review'
      ? 'done'
      : phase === 'coding' || task.status === 'in_progress' || task.status === 'copilot_running'
        ? 'active'
        : failed && (task.reviewReason === 'errors' || task.reviewReason === 'no_work')
          ? 'failed'
          : 'pending';

  const qaState: WorkPhase['state'] =
    done || task.status === 'human_review'
      ? (task.reviewReason === 'qa_rejected' ? 'failed' : 'done')
      : phase && QA_PHASES.has(phase) || task.status === 'ai_review'
        ? 'active'
        : failed
          ? 'failed'
          : 'pending';

  const reviewState: WorkPhase['state'] =
    task.status === 'done'
      ? 'done'
      : task.status === 'human_review'
        ? (failed ? 'failed' : 'active')
        : 'pending';

  return [
    { key: 'plan', label: 'Plan', state: planState },
    { key: 'code', label: 'Code', state: codeState },
    { key: 'qa', label: 'QA', state: qaState },
    { key: 'review', label: 'Review', state: reviewState },
  ];
}

// Custom comparator for React.memo - only re-render when relevant task data changes
function taskCardPropsAreEqual(prevProps: TaskCardProps, nextProps: TaskCardProps): boolean {
  const prevTask = prevProps.task;
  const nextTask = nextProps.task;

  if (
    prevProps.expanded !== nextProps.expanded ||
    prevProps.onToggle !== nextProps.onToggle ||
    prevProps.collapsedByDefault !== nextProps.collapsedByDefault ||
    prevProps.onClick !== nextProps.onClick
  ) {
    return false;
  }

  if (prevTask === nextTask) {
    return true;
  }

  return (
    prevTask.id === nextTask.id &&
    prevTask.status === nextTask.status &&
    prevTask.title === nextTask.title &&
    prevTask.specId === nextTask.specId &&
    prevTask.description === nextTask.description &&
    prevTask.updatedAt === nextTask.updatedAt &&
    prevTask.reviewReason === nextTask.reviewReason &&
    prevTask.executionProgress?.phase === nextTask.executionProgress?.phase &&
    prevTask.executionProgress?.phaseProgress === nextTask.executionProgress?.phaseProgress &&
    prevTask.executionProgress?.overallProgress === nextTask.executionProgress?.overallProgress &&
    prevTask.subtasks.length === nextTask.subtasks.length &&
    prevTask.metadata?.category === nextTask.metadata?.category &&
    prevTask.metadata?.complexity === nextTask.metadata?.complexity &&
    prevTask.metadata?.archivedAt === nextTask.metadata?.archivedAt &&
    prevTask.metadata?.mode === nextTask.metadata?.mode &&
    prevTask.metadata?.copilotDispatch?.issue_url === nextTask.metadata?.copilotDispatch?.issue_url &&
    prevTask.metadata?.copilotDispatch?.pr_url === nextTask.metadata?.copilotDispatch?.pr_url &&
    prevTask.subtasks.every((s, i) => s.status === nextTask.subtasks[i]?.status)
  );
}

export const TaskCard = memo(function TaskCard({
  task,
  onClick,
  expanded: controlledExpanded,
  onToggle: controlledOnToggle,
  collapsedByDefault: controlledCollapsedByDefault,
}: TaskCardProps) {
  const { t } = useTranslation('tasks');
  const [localExpanded, setLocalExpanded] = useState(false);
  const [isStuck, setIsStuck] = useState(false);
  const [isRecovering, setIsRecovering] = useState(false);
  const [isStartingStopping, setIsStartingStopping] = useState(false);
  const [isArchiving, setIsArchiving] = useState(false);
  const stuckCheckRef = useRef<{ timeout: NodeJS.Timeout | null; interval: NodeJS.Timeout | null }>({
    timeout: null,
    interval: null,
  });

  const expanded = controlledExpanded ?? localExpanded;
  const onToggle = useMemo(
    () => controlledOnToggle ?? (() => setLocalExpanded((v) => !v)),
    [controlledOnToggle]
  );

  const isRunning = task.status === 'in_progress';
  const executionPhase = task.executionProgress?.phase;
  const hasActiveExecution =
    executionPhase &&
    executionPhase !== 'idle' &&
    executionPhase !== 'complete' &&
    executionPhase !== 'failed';

  const isIncomplete = isIncompleteHumanReview(task);
  const isArchived = !!task.metadata?.archivedAt;
  const done = task.status === 'done' || isArchived;
  const failed = !done && (isTaskFailed(task) || isStuck || isIncomplete);

  const collapsedByDefault = controlledCollapsedByDefault ?? done;

  const sanitizedDescription = useMemo(
    () => (task.description ? sanitizeMarkdownForDisplay(task.description, 120) : null),
    [task.description]
  );

  const relativeTime = useMemo(() => formatRelativeTime(task.updatedAt), [task.updatedAt]);

  const taskNumber = useMemo(() => extractTaskNumber(task.specId), [task.specId]);

  const formattedTitle = useMemo(
    () => formatTaskTitleWithNumber(task.title, task.specId),
    [task.title, task.specId]
  );

  const performStuckCheck = useCallback(() => {
    const doCheck = () => {
      void checkTaskRunning(task.id).then((actuallyRunning) => {
        setIsStuck(!actuallyRunning);
      });
    };

    if ('requestIdleCallback' in window) {
      (window as Window & { requestIdleCallback: (cb: () => void) => void }).requestIdleCallback(doCheck);
    } else {
      doCheck();
    }
  }, [task.id]);

  useEffect(() => {
    if (!isRunning) {
      setIsStuck(false);
      if (stuckCheckRef.current.timeout) {
        clearTimeout(stuckCheckRef.current.timeout);
        stuckCheckRef.current.timeout = null;
      }
      if (stuckCheckRef.current.interval) {
        clearInterval(stuckCheckRef.current.interval);
        stuckCheckRef.current.interval = null;
      }
      return;
    }

    stuckCheckRef.current.timeout = setTimeout(performStuckCheck, 5000);
    stuckCheckRef.current.interval = setInterval(performStuckCheck, 30000);

    return () => {
      if (stuckCheckRef.current.timeout) clearTimeout(stuckCheckRef.current.timeout);
      if (stuckCheckRef.current.interval) clearInterval(stuckCheckRef.current.interval);
    };
  }, [task.id, isRunning, performStuckCheck]);

  useEffect(() => {
    let debounceTimeout: NodeJS.Timeout | null = null;
    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible' && isRunning) {
        if (debounceTimeout) clearTimeout(debounceTimeout);
        debounceTimeout = setTimeout(performStuckCheck, 500);
      }
    };
    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange);
      if (debounceTimeout) clearTimeout(debounceTimeout);
    };
  }, [isRunning, performStuckCheck]);

  const handleStartStop = useCallback(async () => {
    setIsStartingStopping(true);
    try {
      if (isRunning && !isStuck) {
        await stopTask(task.id);
      } else {
        await startTask(task.id);
      }
    } catch (error) {
      toast({ variant: 'destructive', title: t('labels.error', 'Task action failed'), description: error instanceof Error ? error.message : 'Task action failed' });
    } finally {
      setIsStartingStopping(false);
    }
  }, [isRunning, isStuck, task.id]);

  const handleRecover = useCallback(async () => {
    setIsRecovering(true);
    try {
      const result = await recoverStuckTask(task.id, { autoRestart: true });
      if (result.success) {
        setIsStuck(false);
        if (result.autoRestarted) {
          toast({ title: t('labels.recovered'), description: 'Task recovered and restarted successfully' });
        } else if (result.autoRestartError) {
          toast({
            variant: 'destructive',
            title: t('labels.recovered'),
            description: `Task recovered but restart failed: ${result.autoRestartError}`,
          });
        } else {
          toast({ title: t('labels.recovered'), description: 'Task recovered and reset to backlog' });
        }
      } else {
        toast({
          variant: 'destructive',
          title: 'Recovery Failed',
          description: result.message || 'Failed to recover task',
        });
      }
    } finally {
      setIsRecovering(false);
    }
  }, [task.id, t]);

  const handleArchive = useCallback(async () => {
    setIsArchiving(true);
    try {
      const result = await archiveTasks(task.projectId, [task.id]);
      if (!result.success) {
        toast({ variant: 'destructive', title: t('kanban.archiveFailed'), description: result.error });
      } else {
        toast({ title: t('status.archived'), description: task.title });
      }
    } finally {
      setIsArchiving(false);
    }
  }, [task.id, task.projectId, task.title, t]);

  const getStatusLabel = (status: string) => {
    switch (status) {
      case 'in_progress':
        return t('labels.running');
      case 'ai_review':
        return t('labels.aiReview');
      case 'human_review':
        return t(task.reviewReason === 'needs_input' ? 'labels.needsInput' : 'labels.needsReview');
      case 'done':
        return t('status.complete');
      case 'copilot_running':
        return 'Copilot Running';
      case 'copilot_pr_opened':
        return 'Copilot PR';
      default:
        return t('labels.pending');
    }
  };

  const getReviewReasonLabel = (
    reason?: ReviewReason
  ): { label: string; variant: 'success' | 'destructive' | 'warning' | 'default' } | null => {
    const badge = reviewReasonBadge(reason);
    return badge ? { label: t(badge.labelKey), variant: badge.variant } : null;
  };

  const reviewReasonInfo = task.status === 'human_review' ? getReviewReasonLabel(task.reviewReason) : null;

  const statusText = isStuck
    ? t('labels.needsRecovery')
    : isIncomplete
      ? t('labels.needsResume')
      : hasActiveExecution && executionPhase
        ? EXECUTION_PHASE_LABELS[executionPhase] ?? getStatusLabel(task.status)
        : reviewReasonInfo
          ? reviewReasonInfo.label
          : getStatusLabel(task.status);

  const tone: WorkTone = useMemo(() => {
    if (isTaskEmptyOutcome(task)) return 'warning';
    if (failed) return 'danger';
    if (done) return 'success';
    if (isRunning || hasActiveExecution || task.status === 'ai_review' || task.status === 'copilot_running') {
      return 'running';
    }
    if (task.status === 'human_review') return 'warning';
    return 'neutral';
  }, [task, failed, done, isRunning, hasActiveExecution]);

  const progress = useMemo(() => {
    if (done) return undefined;
    if (task.executionProgress?.overallProgress !== undefined && task.executionProgress.overallProgress > 0) {
      return task.executionProgress.overallProgress;
    }
    if (task.subtasks.length > 0) {
      const completed = task.subtasks.filter((s) => s.status === 'completed').length;
      return Math.round((completed / task.subtasks.length) * 100);
    }
    return undefined;
  }, [done, task.executionProgress?.overallProgress, task.subtasks]);

  const reason = useMemo(() => {
    if (isStuck) return t('labels.stuck');
    if (isIncomplete) return t('labels.incomplete');
    if (reviewReasonInfo) return reviewReasonInfo.label;
    if (executionPhase === 'failed') return EXECUTION_PHASE_LABELS.failed;
    return undefined;
  }, [isStuck, isIncomplete, reviewReasonInfo, executionPhase, t]);

  const badges = useMemo(() => {
    const list: string[] = [];
    if (task.specId) list.push(task.specId);
    if (task.metadata?.mode === 'quick') list.push('QUICK');
    if (isArchived) list.push(t('status.archived'));
    if (task.metadata?.category && TASK_CATEGORY_LABELS[task.metadata.category]) {
      list.push(TASK_CATEGORY_LABELS[task.metadata.category]);
    }
    if (
      task.metadata?.impact &&
      (task.metadata.impact === 'high' || task.metadata.impact === 'critical') &&
      TASK_IMPACT_LABELS[task.metadata.impact]
    ) {
      list.push(TASK_IMPACT_LABELS[task.metadata.impact]);
    }
    if (task.metadata?.complexity && TASK_COMPLEXITY_LABELS[task.metadata.complexity]) {
      list.push(TASK_COMPLEXITY_LABELS[task.metadata.complexity]);
    }
    if (
      task.metadata?.priority &&
      (task.metadata.priority === 'urgent' || task.metadata.priority === 'high') &&
      TASK_PRIORITY_LABELS[task.metadata.priority]
    ) {
      list.push(TASK_PRIORITY_LABELS[task.metadata.priority]);
    }
    if (task.metadata?.securitySeverity) {
      list.push(`${task.metadata.securitySeverity} ${t('metadata.severity')}`);
    }
    return list;
  }, [task.specId, task.metadata, isArchived, t]);

  const phases = useMemo(() => buildTaskPhases(task), [task]);

  const actions: WorkAction[] = useMemo(() => {
    const list: WorkAction[] = [];
    if (isStuck) {
      list.push({
        id: 'recover',
        label: isRecovering ? t('labels.recovering') : t('actions.recover'),
        variant: 'primary',
        disabled: isRecovering,
        busy: isRecovering,
        onClick: () => { void handleRecover(); },
      });
    } else if (isIncomplete) {
      list.push({
        id: 'resume',
        label: t('actions.resume'),
        variant: 'primary',
        disabled: isStartingStopping,
        busy: isStartingStopping,
        onClick: () => { void handleStartStop(); },
      });
    } else if (failed) {
      list.push({
        id: 'start',
        label: t('actions.start'),
        variant: 'outline',
        disabled: isStartingStopping,
        busy: isStartingStopping,
        onClick: () => { void handleStartStop(); },
      });
      if (!isArchived) {
        list.push({
          id: 'archive',
          label: t('actions.archive'),
          variant: 'ghost',
          disabled: isArchiving,
          busy: isArchiving,
          onClick: () => { void handleArchive(); },
        });
      }
    } else if (task.status === 'done' && !isArchived) {
      list.push({
        id: 'archive',
        label: t('actions.archive'),
        variant: 'ghost',
        disabled: isArchiving,
        busy: isArchiving,
        onClick: () => { void handleArchive(); },
      });
    } else if (task.status === 'backlog' || task.status === 'in_progress') {
      list.push({
        id: isRunning ? 'stop' : 'start',
        label: isRunning ? t('actions.stop') : t('actions.start'),
        variant: 'primary',
        disabled: isStartingStopping,
        busy: isStartingStopping,
        onClick: () => { void handleStartStop(); },
      });
    }
    return list;
  }, [
    isStuck,
    isRecovering,
    isIncomplete,
    failed,
    isArchived,
    task.status,
    isRunning,
    isStartingStopping,
    isArchiving,
    t,
    handleRecover,
    handleStartStop,
    handleArchive,
  ]);

  const workCardLabels: WorkCardLabels = useMemo(
    () => ({
      expand: (title: string) => `Expand ${title}`,
      collapse: (title: string) => `Collapse ${title}`,
      phaseStates: {
        done: 'done',
        active: 'active',
        failed: 'failed',
        pending: 'pending',
      },
    }),
    []
  );

  const copilotIssueUrl =
    task.status === 'copilot_running' ? task.metadata?.copilotDispatch?.issue_url : undefined;
  const copilotIssueNum = task.metadata?.copilotDispatch?.issue_number;
  const copilotPrUrl =
    task.status === 'copilot_pr_opened' ? task.metadata?.copilotDispatch?.pr_url : undefined;
  const copilotPrNum = task.metadata?.copilotDispatch?.pr_number;
  const agentCount = task.status !== 'done' && (executionPhase === 'coding' || (task.status === 'in_progress' && !executionPhase))
    ? Math.max(task.subtasks.filter((subtask) => subtask.status === 'in_progress').length, 1)
    : 0;

  return (
    <div data-testid={`task-card-${task.id}`}>
      <WorkCard
        labels={workCardLabels}
        title={formattedTitle}
        subtitle={sanitizedDescription ?? (taskNumber ? `#${taskNumber}` : task.specId)}
        status={agentCount ? `${statusText} · ${t('pipeline.activeAgents', { count: agentCount, defaultValue: '{{count}} active agents' })}` : statusText}
        tone={tone}
        age={relativeTime}
        reason={reason}
        progress={progress}
        phases={phases}
        badges={badges}
        collapsedByDefault={collapsedByDefault}
        expanded={expanded}
        onToggle={onToggle}
        onOpen={onClick}
        actions={actions}
      >
        <div className="space-y-2 text-xs">
          {sanitizedDescription && (
            <p className="text-muted-foreground">{sanitizedDescription}</p>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <CodingRobot active={executionPhase === 'coding' && !isStuck} />
            {copilotIssueUrl && (
              <a
                href={copilotIssueUrl}
                target="_blank"
                rel="noopener noreferrer"
                onClick={(e) => { e.stopPropagation(); }}
                className="px-1.5 py-0.5 rounded-sm bg-warning/10 text-warning hover:bg-warning/20 transition-colors"
              >
                #{copilotIssueNum}
              </a>
            )}
            {copilotPrUrl && (
              <a
                href={copilotPrUrl}
                target="_blank"
                rel="noopener noreferrer"
                onClick={(e) => { e.stopPropagation(); }}
                className="px-1.5 py-0.5 rounded-sm bg-info/10 text-info hover:bg-info/20 transition-colors"
              >
                PR #{copilotPrNum}
              </a>
            )}
          </div>
          {(task.subtasks.length > 0 || hasActiveExecution || isRunning || isStuck) && (
            <PhaseProgressIndicator
              phase={executionPhase}
              subtasks={task.subtasks}
              isStuck={isStuck}
              isRunning={isRunning}
              overallProgress={task.executionProgress?.overallProgress}
            />
          )}
        </div>
      </WorkCard>
    </div>
  );
}, taskCardPropsAreEqual);
