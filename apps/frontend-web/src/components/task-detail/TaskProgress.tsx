import { useEffect, useState } from 'react';
import { Zap, Loader2, Clock } from 'lucide-react';
import { Progress } from '../ui/progress';
import { cn, calculateProgress } from '../../lib/utils';
import { EXECUTION_PHASE_BADGE_COLORS, EXECUTION_PHASE_LABELS } from '../../shared/constants';
import type { Task, ExecutionPhase } from '../../shared/types';

const PROGRESS_STATUS_LABELS: Record<string, string> = {
  spec_creation: 'planning',
  planning: 'planning',
  plan_review: 'planning completed',
  coding: 'coding',
  qa_review: 'reviewing',
  qa_fixing: 'fixing issues',
  complete: 'complete',
  failed: 'failed',
};

// Phase segments shown under the progress bar. Widths map to the overall
// progress ranges the backend reports, so the bar reads as a real timeline.
// Literal class strings per state (Tailwind can't scan runtime-concatenated names).
const PHASE_SEGMENTS: Array<{
  label: string;
  width: number;
  phases: ExecutionPhase[];
  active: string;
  complete: string;
  pending: string;
}> = [
  { label: 'Plan', width: 15, phases: ['spec_creation', 'planning'], active: 'bg-amber-500', complete: 'bg-amber-500/70', pending: 'bg-amber-500/20' },
  { label: 'Review', width: 5, phases: ['plan_review'], active: 'bg-orange-500', complete: 'bg-orange-500/70', pending: 'bg-orange-500/20' },
  { label: 'Code', width: 60, phases: ['coding'], active: 'bg-info', complete: 'bg-info/70', pending: 'bg-info/20' },
  { label: 'QA', width: 15, phases: ['qa_review', 'qa_fixing'], active: 'bg-purple-500', complete: 'bg-purple-500/70', pending: 'bg-purple-500/20' },
  { label: 'Done', width: 5, phases: ['complete'], active: 'bg-success', complete: 'bg-success/70', pending: 'bg-success/20' },
];

/** Format milliseconds as `m:ss` or `h:mm:ss`. */
function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  return `${m}:${String(s).padStart(2, '0')}`;
}

/** Live-ticking elapsed time since `startedAt`, paused when not running. */
function useElapsed(startedAt?: Date, running?: boolean): string | null {
  const [, tick] = useState(0);
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => { tick((n) => n + 1); }, 1000);
    return () => { clearInterval(id); };
  }, [running]);
  if (!startedAt) return null;
  const start = startedAt instanceof Date ? startedAt.getTime() : new Date(startedAt).getTime();
  if (Number.isNaN(start)) return null;
  return formatElapsed(Date.now() - start);
}

interface TaskProgressProps {
  task: Task;
  isRunning: boolean;
  hasActiveExecution: boolean;
  executionPhase?: ExecutionPhase;
  isStuck: boolean;
}

export function TaskProgress({ task, isRunning, hasActiveExecution, executionPhase, isStuck }: TaskProgressProps) {
  const progress = calculateProgress(task.subtasks);
  const elapsed = useElapsed(task.executionProgress?.startedAt, isRunning);

  return (
    <div>
      {/* Execution Phase Indicator */}
      {hasActiveExecution && executionPhase && !isStuck && (
        <div className={cn(
          'rounded-xl border p-3 flex items-center gap-3 mb-5',
          EXECUTION_PHASE_BADGE_COLORS[executionPhase]
        )}>
          <Loader2 className="h-5 w-5 animate-spin shrink-0" />
          <div className="flex-1 min-w-0">
            <div className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-2 text-sm font-medium min-w-0">
                <span className="truncate">{EXECUTION_PHASE_LABELS[executionPhase]}</span>
                <span className="live-dot inline-flex h-1.5 w-1.5 shrink-0 rounded-full bg-current" />
                <span className="text-[10px] font-bold uppercase tracking-wider opacity-70">Live</span>
              </span>
              <span className="flex items-center gap-2.5 shrink-0">
                {elapsed && (
                  <span className="flex items-center gap-1 text-xs tabular-nums opacity-80">
                    <Clock className="h-3 w-3" />
                    {elapsed}
                  </span>
                )}
                <span className="text-sm tabular-nums font-semibold">
                  {task.executionProgress?.overallProgress || 0}%
                </span>
              </span>
            </div>
            {task.executionProgress?.message && (
              <p className="text-xs mt-0.5 opacity-80 truncate">
                {task.executionProgress.message}
              </p>
            )}
            {task.executionProgress?.currentSubtask && (
              <p className="text-xs mt-0.5 opacity-70">
                Subtask: {task.executionProgress.currentSubtask}
              </p>
            )}
          </div>
        </div>
      )}

      {/* Progress Bar */}
      <div className="section-divider mb-3">
        <Zap className="h-3 w-3" />
        {hasActiveExecution && executionPhase && PROGRESS_STATUS_LABELS[executionPhase]
          ? `Progress (${PROGRESS_STATUS_LABELS[executionPhase]})`
          : 'Progress'}
      </div>
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs text-muted-foreground">
          {hasActiveExecution && task.executionProgress?.message
            ? task.executionProgress.message
            : task.subtasks.length > 0
              ? `${task.subtasks.filter(c => c.status === 'completed').length}/${task.subtasks.length} subtasks completed`
              : 'No subtasks yet'}
        </span>
        <span className={cn(
          'text-sm font-semibold tabular-nums',
          task.status === 'done' ? 'text-success' : 'text-foreground'
        )}>
          {hasActiveExecution
            ? `${task.executionProgress?.overallProgress || 0}%`
            : `${progress}%`}
        </span>
      </div>
      <Progress
        value={hasActiveExecution ? (task.executionProgress?.overallProgress || 0) : progress}
        className={cn(
          'h-2.5',
          task.status === 'done' && '[&>div]:bg-success'
        )}
        animated={isRunning || task.status === 'ai_review'}
      />
      {/* Phase Progress Bar Segments — labeled timeline */}
      {hasActiveExecution && (
        <div className="mt-2.5">
          <div className="flex gap-0.5 h-1.5 rounded-full overflow-hidden bg-muted/30">
            {PHASE_SEGMENTS.map((seg) => {
              const isActive = !!executionPhase && seg.phases.includes(executionPhase);
              const isComplete =
                (task.executionProgress?.overallProgress || 0) >=
                PHASE_SEGMENTS.slice(0, PHASE_SEGMENTS.indexOf(seg) + 1).reduce((a, s) => a + s.width, 0);
              return (
                <div
                  key={seg.label}
                  className={cn(
                    'transition-all duration-300',
                    isActive ? seg.active : isComplete ? seg.complete : seg.pending
                  )}
                  style={{ width: `${seg.width}%` }}
                  title={`${seg.label} — ${seg.width}% of run`}
                />
              );
            })}
          </div>
          <div className="mt-1 flex text-[10px] font-medium uppercase tracking-wide text-muted-foreground/70">
            {PHASE_SEGMENTS.map((seg) => {
              const isActive = !!executionPhase && seg.phases.includes(executionPhase);
              return (
                <span
                  key={seg.label}
                  className={cn('truncate', isActive && 'text-foreground font-semibold')}
                  style={{ width: `${seg.width}%` }}
                >
                  {seg.label}
                </span>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
