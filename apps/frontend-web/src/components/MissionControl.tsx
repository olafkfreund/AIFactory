import { useState, useEffect, useMemo, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import {
  X,
  Play,
  Square,
  RotateCcw,
  Loader2,
  Pencil,
  Activity,
  TerminalSquare,
  ListTree,
  FileCode,
  ClipboardList,
  ClipboardCheck,
  MonitorPlay,
  Minimize2,
} from 'lucide-react';
import { toast } from '../hooks/use-toast';
import { Button } from './ui/button';
import { Badge } from './ui/badge';
import { Progress } from './ui/progress';
import { Tabs, TabsContent, TabsList, TabsTrigger } from './ui/tabs';
import { ScrollArea } from './ui/scroll-area';
import { ResizablePanels } from './ui/resizable-panels';
import { cn, calculateProgress } from '../lib/utils';
import { startTask, stopTask, recoverStuckTask, submitReview, persistTaskStatus } from '../stores/task-store';
import { useProjectStore } from '../stores/project-store';
import { TASK_STATUS_LABELS } from '../shared/constants';
import { useTaskDetail } from './task-detail/hooks/useTaskDetail';
import { TaskProgress } from './task-detail/TaskProgress';
import { TaskSubtasks } from './task-detail/TaskSubtasks';
import { TaskMetadata } from './task-detail/TaskMetadata';
import { TaskLogs } from './task-detail/TaskLogs';
import { TaskFiles } from './task-detail/TaskFiles';
import { TaskReview } from './task-detail/TaskReview';
import { PlanReviewSection } from './task-detail/PlanReviewSection';
import { AgentConsole } from './task-detail/AgentConsole';
import { CreatePRDialog } from './task-detail/task-review/CreatePRDialog';
import { LivePreviewPane } from './LivePreviewPane';
import type { Task } from '../shared/types';

interface MissionControlProps {
  task: Task | null;
  onClose: () => void;
  /** Switch back to the compact modal view for this task. */
  onCollapse?: (taskId: string) => void;
  onEdit?: (taskId: string) => void;
}

/**
 * MissionControl — full-page, 3-pane task workspace (prototype).
 *
 * Plan/Subtasks · Activity & live Console · Files/Diff — the one-screen
 * layout the big agentic-coding tools converged on. Reuses the existing
 * `useTaskDetail` orchestrator and task-detail child components so there's
 * no duplicated data plumbing; it's purely a new composition/layout.
 */
export function MissionControl({ task, onClose, onCollapse, onEdit }: MissionControlProps) {
  if (!task) return null;
  return (
    <MissionControlContent
      task={task}
      onClose={onClose}
      onCollapse={onCollapse}
      onEdit={onEdit}
    />
  );
}

function MissionControlContent({
  task,
  onClose,
  onCollapse,
  onEdit,
}: {
  task: Task;
  onClose: () => void;
  onCollapse?: (taskId: string) => void;
  onEdit?: (taskId: string) => void;
}) {
  const { t } = useTranslation(['tasks']);
  const state = useTaskDetail({ task });

  // Probe rmux capability (same contract as TaskDetailModal).
  const [rmuxEnabled, setRmuxEnabled] = useState(false);
  useEffect(() => {
    let cancelled = false;
    fetch('/api/capabilities', { credentials: 'same-origin' })
      .then((r) => (r.ok ? r.json() : { rmux: false }))
      .then((c) => !cancelled && setRmuxEnabled(Boolean(c?.rmux)))
      .catch(() => !cancelled && setRmuxEnabled(false));
    return () => {
      cancelled = true;
    };
  }, []);

  // Close on Escape.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const executionPhase = task.executionProgress?.phase;
  const hasActiveExecution = Boolean(
    executionPhase &&
      executionPhase !== 'idle' &&
      executionPhase !== 'complete' &&
      executionPhase !== 'failed'
  );

  const progressPercent = useMemo(() => {
    const backend = task.executionProgress?.overallProgress;
    return Math.round(backend !== undefined ? backend : calculateProgress(task.subtasks));
  }, [task.executionProgress?.overallProgress, task.subtasks]);

  const statusVariant =
    task.status === 'done'
      ? 'success'
      : task.status === 'human_review'
        ? 'purple'
        : task.status === 'in_progress'
          ? 'info'
          : 'secondary';

  const worktreeSpecsPath =
    state.worktreeStatus?.exists && state.worktreeStatus.worktreePath && task.specId
      ? `${state.worktreeStatus.worktreePath}/.aifactory/specs/${task.specId}`
      : undefined;

  // Actions — kept open after start so the operator watches the run live.
  const handleStartStop = () => {
    if (state.isRunning && !state.isStuck) stopTask(task.id);
    else startTask(task.id);
  };

  const handleRecover = async () => {
    state.setIsRecovering(true);
    const result = await recoverStuckTask(task.id, { autoRestart: true });
    if (result.success) {
      state.setIsStuck(false);
      state.setHasCheckedRunning(false);
      toast({ title: t('labels.recovered'), description: 'Task recovered' });
    } else {
      toast({
        variant: 'destructive',
        title: t('labels.recoveryFailed'),
        description: result.message || 'Failed to recover task',
      });
    }
    state.setIsRecovering(false);
  };

  // ---- Review / merge flow (mirrors TaskDetailModal) ---------------------
  const [isCreatingPR, setIsCreatingPR] = useState(false);
  const [showCreatePRDialog, setShowCreatePRDialog] = useState(false);
  const selectedProject = useProjectStore((s) => s.getSelectedProject());

  const handleReject = async () => {
    if (!state.feedback.trim()) return;
    state.setIsSubmitting(true);
    await submitReview(task.id, false, state.feedback);
    state.setIsSubmitting(false);
    state.setFeedback('');
  };

  const handleMerge = async () => {
    state.setIsMerging(true);
    state.setWorkspaceError(null);
    try {
      const result = await state.unifiedMerge(state.stageOnly);
      if (result.success && result.data) {
        if (result.stageOnly && result.data.staged) {
          state.setWorkspaceError(null);
          state.setStagedSuccess(result.data.message || 'Changes staged in main project');
          state.setStagedProjectPath(result.data.projectPath);
          state.setSuggestedCommitMessage(result.data.suggestedCommitMessage);
        } else {
          await persistTaskStatus(task.id, 'done', { force: true });
          onClose();
        }
      }
    } finally {
      state.setIsMerging(false);
    }
  };

  const handleDiscard = async () => {
    state.setIsDiscarding(true);
    state.setWorkspaceError(null);
    const result = await window.API.discardWorktree(task.id);
    if (result.success && result.data?.success) {
      state.setShowDiscardDialog(false);
      onClose();
    } else {
      state.setWorkspaceError(result.data?.message || result.error || 'Failed to discard changes');
    }
    state.setIsDiscarding(false);
  };

  const handleCreatePR = () => setShowCreatePRDialog(true);

  const needsReview = state.needsReview && task.reviewReason !== 'plan_review';

  const renderPrimaryAction = () => {
    if (state.isStuck) {
      return (
        <Button variant="warning" size="sm" onClick={handleRecover} disabled={state.isRecovering}>
          {state.isRecovering ? (
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          ) : (
            <RotateCcw className="mr-2 h-4 w-4" />
          )}
          {state.isRecovering ? 'Recovering…' : 'Recover'}
        </Button>
      );
    }
    if (state.isIncomplete) {
      return (
        <Button variant="default" size="sm" onClick={handleStartStop}>
          <Play className="mr-2 h-4 w-4" />
          Resume
        </Button>
      );
    }
    if (task.status === 'backlog' || task.status === 'in_progress') {
      return (
        <Button
          variant={state.isRunning ? 'destructive' : 'default'}
          size="sm"
          onClick={handleStartStop}
        >
          {state.isRunning ? <Square className="mr-2 h-4 w-4" /> : <Play className="mr-2 h-4 w-4" />}
          {state.isRunning ? 'Stop' : 'Start'}
        </Button>
      );
    }
    return null;
  };

  // ---- Panes ------------------------------------------------------------

  const showPlanReview = task.status === 'human_review' && task.reviewReason === 'plan_review';

  const planPane = (
    <PaneShell icon={<ListTree className="h-3.5 w-3.5" />} title="Plan & Subtasks">
      <ScrollArea className="h-full">
        <div className="space-y-5 p-4">
          <TaskProgress
            task={task}
            isRunning={state.isRunning}
            hasActiveExecution={hasActiveExecution}
            executionPhase={executionPhase}
            isStuck={state.isStuck}
          />
          {showPlanReview && <PlanReviewSection task={task} onResume={handleStartStop} />}
          <TaskSubtasks task={task} />
          <TaskMetadata task={task} />
        </div>
      </ScrollArea>
    </PaneShell>
  );

  const activityPane = (
    <PaneShell icon={<Activity className="h-3.5 w-3.5" />} title="Activity">
      <Tabs defaultValue="activity" className="flex h-full flex-col">
        <div className="px-3 pt-2">
          <TabsList>
            <TabsTrigger value="activity" className="gap-1.5">
              <Activity className="h-3.5 w-3.5" />
              Activity
            </TabsTrigger>
            {rmuxEnabled && (
              <TabsTrigger value="console" className="gap-1.5">
                <TerminalSquare className="h-3.5 w-3.5" />
                Live Console
              </TabsTrigger>
            )}
          </TabsList>
        </div>
        <TabsContent value="activity" className="flex-1 min-h-0 mt-0">
          <TaskLogs
            task={task}
            phaseLogs={state.phaseLogs}
            isLoadingLogs={state.isLoadingLogs}
            expandedPhases={state.expandedPhases}
            isStuck={state.isStuck}
            logsEndRef={state.logsEndRef}
            logsContainerRef={state.logsContainerRef}
            onLogsScroll={state.handleLogsScroll}
            onTogglePhase={state.togglePhase}
          />
        </TabsContent>
        {rmuxEnabled && (
          <TabsContent value="console" className="flex-1 min-h-0 overflow-hidden mt-0">
            <AgentConsole taskId={task.id} />
          </TabsContent>
        )}
      </Tabs>
    </PaneShell>
  );

  const rightPane = (
    <PaneShell icon={<MonitorPlay className="h-3.5 w-3.5" />} title="Output">
      <Tabs defaultValue={needsReview ? 'review' : 'preview'} className="flex h-full flex-col">
        <div className="px-3 pt-2">
          <TabsList>
            <TabsTrigger value="preview" className="gap-1.5">
              <MonitorPlay className="h-3.5 w-3.5" />
              Preview
            </TabsTrigger>
            <TabsTrigger value="files" className="gap-1.5">
              <FileCode className="h-3.5 w-3.5" />
              Files
            </TabsTrigger>
            <TabsTrigger value="review" className="gap-1.5">
              <ClipboardCheck className="h-3.5 w-3.5" />
              Review
              {needsReview && (
                <span className="ml-0.5 inline-flex h-1.5 w-1.5 rounded-full bg-fuchsia-500" />
              )}
            </TabsTrigger>
          </TabsList>
        </div>

        <TabsContent value="preview" className="flex-1 min-h-0 overflow-hidden mt-0">
          <LivePreviewPane />
        </TabsContent>

        <TabsContent value="files" className="flex-1 min-h-0 overflow-hidden mt-0">
          <TaskFiles task={task} worktreeSpecsPath={worktreeSpecsPath} />
        </TabsContent>

        <TabsContent value="review" className="flex-1 min-h-0 mt-0">
          {needsReview ? (
            <ScrollArea className="h-full">
              <div className="p-4">
                <TaskReview
                  task={task}
                  feedback={state.feedback}
                  isSubmitting={state.isSubmitting}
                  worktreeStatus={state.worktreeStatus}
                  worktreeDiff={state.worktreeDiff}
                  isLoadingWorktree={state.isLoadingWorktree}
                  isMerging={state.isMerging}
                  isDiscarding={state.isDiscarding}
                  showDiscardDialog={state.showDiscardDialog}
                  showDiffDialog={state.showDiffDialog}
                  workspaceError={state.workspaceError}
                  stageOnly={state.stageOnly}
                  stagedSuccess={state.stagedSuccess}
                  stagedProjectPath={state.stagedProjectPath}
                  suggestedCommitMessage={state.suggestedCommitMessage}
                  mergePreview={state.mergePreview}
                  isLoadingPreview={state.isLoadingPreview}
                  showConflictDialog={state.showConflictDialog}
                  isAbortingMerge={state.isAbortingMerge}
                  mergeStep={state.mergeStep}
                  phaseLogs={state.phaseLogs ?? undefined}
                  onFeedbackChange={state.setFeedback}
                  onReject={handleReject}
                  onMerge={handleMerge}
                  onCreatePR={handleCreatePR}
                  isCreatingPR={isCreatingPR}
                  onDiscard={handleDiscard}
                  onShowDiscardDialog={state.setShowDiscardDialog}
                  onShowDiffDialog={state.setShowDiffDialog}
                  onStageOnlyChange={state.setStageOnly}
                  onShowConflictDialog={state.setShowConflictDialog}
                  onLoadMergePreview={state.loadMergePreview}
                  onAbortMerge={state.abortMerge}
                  onClose={onClose}
                />
              </div>
            </ScrollArea>
          ) : (
            <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center">
              <ClipboardCheck className="h-8 w-8 text-muted-foreground/40" />
              <p className="text-sm text-muted-foreground">No review pending</p>
              <p className="max-w-xs text-xs text-muted-foreground/60">
                When the agent finishes and the task moves to human review, the merge / accept /
                reject controls appear here.
              </p>
            </div>
          )}
        </TabsContent>
      </Tabs>
    </PaneShell>
  );

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-background animate-in fade-in duration-150">
      {/* Header bar */}
      <header className="flex items-center gap-3 border-b border-border px-4 py-2.5">
        <ClipboardList className="h-4 w-4 shrink-0 text-primary" />
        <div className="flex min-w-0 items-center gap-2">
          <h1 className="truncate text-sm font-semibold text-foreground" title={task.title}>
            {task.title}
          </h1>
          {task.specId && (
            <span className="shrink-0 font-mono text-xs text-muted-foreground">{task.specId}</span>
          )}
        </div>

        <Badge variant={statusVariant} className="shrink-0">
          {TASK_STATUS_LABELS[task.status]}
        </Badge>
        {hasActiveExecution && (
          <span className="flex shrink-0 items-center gap-1.5 text-xs font-medium text-info">
            <span className="live-dot inline-flex h-1.5 w-1.5 rounded-full bg-current" />
            LIVE
          </span>
        )}

        {/* Compact progress in the header */}
        <div className="ml-2 hidden min-w-0 flex-1 items-center gap-2 sm:flex">
          <Progress
            value={progressPercent}
            className="h-1.5 max-w-xs"
            animated={state.isRunning || task.status === 'ai_review'}
          />
          <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
            {progressPercent}%
          </span>
        </div>

        <div className="ml-auto flex shrink-0 items-center gap-1.5">
          {renderPrimaryAction()}
          {onEdit && (
            <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => onEdit(task.id)}>
              <Pencil className="h-4 w-4" />
            </Button>
          )}
          {onCollapse && (
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8"
              title="Collapse to compact view"
              onClick={() => onCollapse(task.id)}
            >
              <Minimize2 className="h-4 w-4" />
            </Button>
          )}
          <Button variant="ghost" size="icon" className="h-8 w-8" onClick={onClose} title="Close (Esc)">
            <X className="h-4 w-4" />
          </Button>
        </div>
      </header>

      {/* 3-pane body: Plan | (Activity | Files) */}
      <ResizablePanels
        className="flex-1"
        storageKey="mission-control:outer"
        defaultLeftWidth={26}
        minLeftWidth={18}
        maxLeftWidth={42}
        leftPanel={planPane}
        rightPanel={
          <ResizablePanels
            storageKey="mission-control:inner"
            defaultLeftWidth={58}
            minLeftWidth={35}
            maxLeftWidth={75}
            leftPanel={activityPane}
            rightPanel={rightPane}
          />
        }
      />

      {/* Create PR dialog (used by the Review tab) */}
      {selectedProject && (
        <CreatePRDialog
          open={showCreatePRDialog}
          task={task}
          projectPath={selectedProject.path}
          onOpenChange={setShowCreatePRDialog}
          onSuccess={() => setIsCreatingPR(false)}
          onError={(error) => {
            state.setWorkspaceError(error);
            setIsCreatingPR(false);
          }}
        />
      )}
    </div>
  );
}

/** Pane wrapper with a small sticky label header. */
function PaneShell({
  icon,
  title,
  children,
}: {
  icon: ReactNode;
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="flex h-full min-h-0 flex-1 flex-col bg-card/30">
      <div
        className={cn(
          'flex items-center gap-1.5 border-b border-border px-3 py-1.5',
          'text-[11px] font-semibold uppercase tracking-wide text-muted-foreground'
        )}
      >
        {icon}
        {title}
      </div>
      <div className="flex min-h-0 flex-1 flex-col">{children}</div>
    </div>
  );
}
