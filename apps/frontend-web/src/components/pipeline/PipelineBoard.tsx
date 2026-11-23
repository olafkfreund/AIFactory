/**
 * PipelineBoard — AIFactory tasks board rendered with the hub-owned
 * shared factory-ui PipelineStrip, WorkColumn and WorkCard components (#3876).
 *
 * Stages: Plan → Code → QA → Review.
 * Done/archived tasks collapse to a single 36px row by default; WorkCard sits
 * inside SortableTaskCard keeping drag-and-drop, memo comparators, start/stop/
 * recover/archive with busy states, Copilot issue/PR links, and phase progress.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  DndContext,
  DragOverlay,
  closestCorners,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  useDroppable,
  type DragStartEvent,
  type DragEndEvent,
} from '@dnd-kit/core';
import {
  SortableContext,
  sortableKeyboardCoordinates,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';

import { usePipelineStripSlot } from '../../contexts/PipelineStripContext';
import {
  PipelineStrip,
  type PipelineStage,
  type PipelineStripLabels,
  type PipelineStripProps,
  type StageTone,
} from '../factory-ui/PipelineStrip';
import { WorkColumn } from '../factory-ui/WorkColumn';
import { SortableTaskCard } from '../SortableTaskCard';
import { TaskCard, isTaskDone, isTaskFailed, buildTaskPhases } from '../TaskCard';
import { Button } from '../ui/button';
import { persistTaskStatus } from '../../stores/task-store';
import type { Task, TaskStatus } from '../../shared/types';

export type Stage = 'plan' | 'code' | 'qa' | 'review';

const PLAN_PHASES = new Set(['spec_creation', 'planning', 'plan_review']);
const QA_PHASES = new Set(['qa_review', 'qa_fixing']);
const ORDER: Stage[] = ['plan', 'code', 'qa', 'review'];

const STAGES: { key: Stage; label: string; service: string; tone: StageTone; dropStatus: TaskStatus }[] = [
  { key: 'plan', label: 'Plan', service: 'Planner', tone: 'plan', dropStatus: 'backlog' },
  { key: 'code', label: 'Code', service: 'Coder', tone: 'code', dropStatus: 'in_progress' },
  { key: 'qa', label: 'QA', service: 'QA Reviewer · Fixer', tone: 'test', dropStatus: 'ai_review' },
  { key: 'review', label: 'Review', service: 'Human Review · Shipped', tone: 'success', dropStatus: 'human_review' },
];

export { isTaskDone as isDone, isTaskFailed as isFailed, buildTaskPhases as phaseStrip };

export function stageOf(task: Task): Stage {
  const phase = task.executionProgress?.phase;
  if (task.status === 'done') return 'review';
  if (task.status === 'human_review') {
    if (task.reviewReason === 'plan_review') return 'plan';
    if (task.reviewReason === 'qa_rejected') return 'qa';
    if (task.reviewReason === 'errors' || task.reviewReason === 'no_work') return 'code';
    return 'review';
  }
  if (phase === 'coding') return 'code';
  if (phase && QA_PHASES.has(phase)) return 'qa';
  if (phase && PLAN_PHASES.has(phase)) return 'plan';
  if (task.status === 'in_progress' || task.status === 'copilot_running' || task.status === 'copilot_pr_opened') {
    return 'code';
  }
  if (task.status === 'ai_review') return 'qa';
  if (task.status === 'backlog') return 'plan';
  return 'plan';
}

export function activeStage(task: Task): Stage | null {
  if (task.status === 'done') return null;
  const phase = task.executionProgress?.phase;
  if (phase && phase !== 'idle' && phase !== 'complete' && phase !== 'failed') {
    if (phase === 'coding') return 'code';
    if (QA_PHASES.has(phase)) return 'qa';
    if (PLAN_PHASES.has(phase)) return 'plan';
  }
  if (task.status === 'in_progress' || task.status === 'copilot_running') return 'code';
  if (task.status === 'ai_review') return 'qa';
  return null;
}

function DroppableStageShell({
  stageKey,
  children,
}: {
  stageKey: Stage;
  children: React.ReactNode;
}) {
  const { setNodeRef } = useDroppable({ id: stageKey });
  return (
    <div ref={setNodeRef} data-stage={stageKey}>
      {children}
    </div>
  );
}

interface Props {
  tasks: Task[];
  projectId?: string;
  onTaskClick: (task: Task) => void;
  onNewTaskClick?: () => void;
  isInitialized?: boolean;
}

export function PipelineBoard({ tasks, projectId, onTaskClick }: Props) {
  const { t } = useTranslation(['tasks', 'navigation']);
  const stripSlot = usePipelineStripSlot();
  const [selectedStage, setSelectedStage] = useState<Stage | null>(null);
  const [failureOnly, setFailureOnly] = useState(false);
  const [showFailed, setShowFailed] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [activeTask, setActiveTask] = useState<Task | null>(null);
  const [alarm, setAlarm] = useState<{ key: string; event: string } | undefined>(undefined);
  const prevFailedRef = useRef<Map<string, string> | null>(null);

  const visible = useMemo(
    () => (showArchived ? tasks : tasks.filter((task) => !task.metadata?.archivedAt)),
    [tasks, showArchived]
  );

  const failed = useMemo(() => visible.filter(isTaskFailed), [visible]);

  useEffect(() => {
    const nextFailed = new Map<string, string>();
    let latestAlarm: { key: string; event: string } | undefined;
    for (const task of failed) {
      const key = `${task.projectId}:${task.id}`;
      const stamp = String(task.updatedAt);
      nextFailed.set(key, stamp);
      if (prevFailedRef.current && prevFailedRef.current.get(key) !== stamp) {
        latestAlarm = { key: stageOf(task), event: `${key}:${stamp}` };
      }
    }
    prevFailedRef.current = nextFailed;
    if (latestAlarm) setAlarm(latestAlarm);
  }, [failed]);

  const byStage = useMemo(() => {
    const g: Record<Stage, Task[]> = { plan: [], code: [], qa: [], review: [] };
    for (const task of visible) {
      if (!isTaskFailed(task)) g[stageOf(task)].push(task);
    }
    for (const k of ORDER) {
      g[k].sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime());
    }
    return g;
  }, [visible]);

  const failedByStage = useMemo(() => {
    const g: Record<Stage, Task[]> = { plan: [], code: [], qa: [], review: [] };
    for (const task of failed) {
      g[stageOf(task)].push(task);
    }
    for (const k of ORDER) {
      g[k].sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime());
    }
    return g;
  }, [failed]);

  const stages: PipelineStage[] = useMemo(
    () =>
      STAGES.map((stage) => ({
        key: stage.key,
        label: stage.label,
        service: stage.service,
        count: byStage[stage.key].length,
        failed: failedByStage[stage.key].length,
        tone: stage.tone,
      })),
    [byStage, failedByStage]
  );

  const handleSelectStage = useCallback((key: string | null) => {
    setSelectedStage((key as Stage | null) ?? null);
    setFailureOnly(false);
    if (key === null) setShowFailed(false);
  }, []);

  const handleSelectFailed = useCallback((key: string) => {
    setSelectedStage(key as Stage);
    setFailureOnly(true);
    setShowFailed(true);
  }, []);

  const stripProps: PipelineStripProps = useMemo(
    () => ({
      stages,
      activeStage: selectedStage,
      onSelectStage: handleSelectStage,
      onSelectFailed: handleSelectFailed,
      alarm,
    }),
    [stages, selectedStage, handleSelectStage, handleSelectFailed, alarm]
  );

  useEffect(() => {
    if (!stripSlot) return;
    stripSlot.setStrip(stripProps);
    return () => {
      stripSlot.setStrip(undefined);
    };
  }, [stripSlot, stripProps]);

  const pipelineLabels: PipelineStripLabels = useMemo(
    () => ({
      pipeline: t('navigation:shell.pipeline', 'Pipeline stages'),
      clearFilter: t('navigation:shell.clearFilter', 'Clear filter'),
      failures: (count: number) => t('tasks:pipeline.failed', { count, defaultValue: '{{count}} failed' }),
      filterFailures: (stage) =>
        t('navigation:shell.filterFailures', {
          stage: stage.label,
          count: stage.failed,
          defaultValue: 'Filter {{stage}} failures ({{count}})',
        }),
    }),
    [t]
  );

  const visibleStages = useMemo(
    () => (selectedStage ? STAGES.filter((s) => s.key === selectedStage) : STAGES),
    [selectedStage]
  );

  const visibleFailed = useMemo(
    () => (selectedStage ? failedByStage[selectedStage] : failed),
    [selectedStage, failedByStage, failed]
  );

  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: { distance: 8 },
    }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    })
  );

  const handleDragStart = (event: DragStartEvent) => {
    const task = tasks.find((tItem) => tItem.id === event.active.id);
    if (task) setActiveTask(task);
  };

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    setActiveTask(null);
    if (!over) return;

    const activeTaskId = String(active.id);
    const overId = String(over.id);
    const draggedTask = tasks.find((tItem) => tItem.id === activeTaskId);
    if (!draggedTask) return;

    const stageMatch = STAGES.find((s) => s.key === overId);
    if (stageMatch) {
      if (draggedTask.status !== stageMatch.dropStatus) {
        void persistTaskStatus(activeTaskId, stageMatch.dropStatus);
      }
      return;
    }

    const overTask = tasks.find((tItem) => tItem.id === overId);
    if (overTask && draggedTask.status !== overTask.status) {
      void persistTaskStatus(activeTaskId, overTask.status);
    }
  };

  return (
    <div className="space-y-4 p-4">
      {!stripSlot && <PipelineStrip {...stripProps} labels={pipelineLabels} />}

      <DndContext
        sensors={sensors}
        collisionDetection={closestCorners}
        onDragStart={handleDragStart}
        onDragEnd={handleDragEnd}
      >
        <div className="f-board">
          {visibleStages.map((stage) => {
            const stageTasks = failureOnly ? [] : byStage[stage.key];
            return (
              <DroppableStageShell key={stage.key} stageKey={stage.key}>
                <SortableContext
                  items={stageTasks.map((tItem) => tItem.id)}
                  strategy={verticalListSortingStrategy}
                >
                  <WorkColumn
                    title={stage.label}
                    tone={stage.tone}
                    projectId={projectId}
                    emptyLabel={emptyText(stage.key)}
                    showMoreLabel={(count) =>
                      t('tasks:pipeline.showMore', { count, defaultValue: `Show ${count} more` })
                    }
                    items={stageTasks.map((task) => {
                      const done = task.status === 'done' || Boolean(task.metadata?.archivedAt);
                      return {
                        id: task.id,
                        projectId: task.projectId,
                        collapsedByDefault: done,
                        render: ({ expanded, onToggle }) => (
                          <div data-testid={`task-row-${task.id}`}>
                            <SortableTaskCard
                              task={task}
                              onClick={() => onTaskClick(task)}
                              expanded={expanded}
                              onToggle={onToggle}
                              collapsedByDefault={done}
                            />
                          </div>
                        ),
                      };
                    })}
                  />
                </SortableContext>
              </DroppableStageShell>
            );
          })}
        </div>

        <DragOverlay>
          {activeTask ? (
            <div className="drag-overlay-card">
              <TaskCard task={activeTask} onClick={() => {}} />
            </div>
          ) : null}
        </DragOverlay>
      </DndContext>

      <div className="flex flex-wrap items-center gap-4 text-sm">
        <Button
          type="button"
          size="sm"
          variant="outline"
          aria-expanded={showFailed}
          onClick={() => setShowFailed((value) => !value)}
        >
          {t('tasks:pipeline.failed', { count: visibleFailed.length })}
        </Button>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(event) => setShowArchived(event.target.checked)}
          />
          {t('tasks:pipeline.showArchived', 'Show archived')}
        </label>
      </div>

      {showFailed && visibleFailed.length > 0 && (
        <div data-testid="failed-runs" className="grid gap-2">
          {visibleFailed.map((task) => (
            <div key={task.id} data-testid={`task-row-${task.id}`}>
              <SortableTaskCard
                task={task}
                onClick={() => onTaskClick(task)}
                collapsedByDefault={Boolean(task.metadata?.archivedAt)}
              />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function emptyText(stage: Stage): string {
  switch (stage) {
    case 'plan':
      return 'Nothing planned';
    case 'code':
      return 'Nothing building';
    case 'qa':
      return 'Nothing in QA';
    case 'review':
      return 'Nothing in review';
    default:
      return '';
  }
}
