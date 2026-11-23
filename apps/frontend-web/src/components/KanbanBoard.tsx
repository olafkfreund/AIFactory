import { useState, useMemo, memo } from 'react';
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
  type DragOverEvent,
} from '@dnd-kit/core';
import {
  SortableContext,
  sortableKeyboardCoordinates,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';
import { Plus, Archive, RefreshCw } from 'lucide-react';
import { toast } from '../hooks/use-toast';
import { Button } from './ui/button';
import { Tooltip, TooltipContent, TooltipTrigger } from './ui/tooltip';
import { TaskCard } from './TaskCard';
import { SortableTaskCard } from './SortableTaskCard';
import { WorkColumn } from './factory-ui/WorkColumn';
import type { StageTone } from './factory-ui/PipelineStrip';
import { KanbanColumnSkeleton } from './ui/skeleton';
import { TASK_STATUS_COLUMNS, TASK_STATUS_LABELS } from '../shared/constants';
import { cn } from '../lib/utils';
import { persistTaskStatus, archiveTasks } from '../stores/task-store';
import type { Task, TaskStatus } from '../shared/types';

interface KanbanBoardProps {
  tasks: Task[];
  projectId?: string;
  onTaskClick: (task: Task) => void;
  onNewTaskClick?: () => void;
  onRefresh?: () => void;
  isRefreshing?: boolean;
  isInitialized?: boolean;
}

interface DroppableColumnProps {
  status: TaskStatus;
  projectId?: string;
  tasks: Task[];
  onTaskClick: (task: Task) => void;
  isOver: boolean;
  onAddClick?: () => void;
  onArchiveAll?: () => void;
  archivedCount?: number;
  showArchived?: boolean;
  onToggleArchived?: () => void;
}

function tasksAreEquivalent(prevTasks: Task[], nextTasks: Task[]): boolean {
  if (prevTasks.length !== nextTasks.length) return false;
  if (prevTasks === nextTasks) return true;

  for (let i = 0; i < prevTasks.length; i++) {
    const prev = prevTasks[i];
    const next = nextTasks[i];
    if (
      prev.id !== next.id ||
      prev.status !== next.status ||
      prev.executionProgress?.phase !== next.executionProgress?.phase ||
      prev.updatedAt !== next.updatedAt
    ) {
      return false;
    }
  }
  return true;
}

function droppableColumnPropsAreEqual(
  prevProps: DroppableColumnProps,
  nextProps: DroppableColumnProps
): boolean {
  if (prevProps.status !== nextProps.status) return false;
  if (prevProps.projectId !== nextProps.projectId) return false;
  if (prevProps.isOver !== nextProps.isOver) return false;
  if (prevProps.onTaskClick !== nextProps.onTaskClick) return false;
  if (prevProps.onAddClick !== nextProps.onAddClick) return false;
  if (prevProps.onArchiveAll !== nextProps.onArchiveAll) return false;
  if (prevProps.archivedCount !== nextProps.archivedCount) return false;
  if (prevProps.showArchived !== nextProps.showArchived) return false;
  if (prevProps.onToggleArchived !== nextProps.onToggleArchived) return false;

  return tasksAreEquivalent(prevProps.tasks, nextProps.tasks);
}

function columnTone(status: TaskStatus): StageTone {
  switch (status) {
    case 'backlog':
      return 'plan';
    case 'in_progress':
      return 'code';
    case 'ai_review':
      return 'test';
    case 'human_review':
      return 'warning';
    case 'done':
      return 'success';
    default:
      return 'neutral';
  }
}

function emptyLabelForStatus(status: TaskStatus, t: (key: string) => string): string {
  switch (status) {
    case 'backlog':
      return t('kanban.emptyBacklog');
    case 'in_progress':
      return t('kanban.emptyInProgress');
    case 'ai_review':
      return t('kanban.emptyAiReview');
    case 'human_review':
      return t('kanban.emptyHumanReview');
    case 'done':
      return t('kanban.emptyDone');
    default:
      return t('kanban.emptyDefault');
  }
}

const DroppableColumn = memo(function DroppableColumn({
  status,
  projectId,
  tasks,
  onTaskClick,
  isOver,
  onAddClick,
  onArchiveAll,
  archivedCount,
  showArchived,
  onToggleArchived,
}: DroppableColumnProps) {
  const { t } = useTranslation(['tasks', 'common']);
  const { setNodeRef } = useDroppable({ id: status });

  const taskIds = useMemo(() => tasks.map((task) => task.id), [tasks]);

  const onClickHandlers = useMemo(() => {
    const handlers = new Map<string, () => void>();
    tasks.forEach((task) => {
      handlers.set(task.id, () => {
        onTaskClick(task);
      });
    });
    return handlers;
  }, [tasks, onTaskClick]);

  return (
    <div
      ref={setNodeRef}
      className={cn('min-w-56 flex-1 transition-all duration-200', isOver && 'drop-zone-highlight')}
    >
      <div className="mb-1 flex items-center justify-end gap-1">
        {status === 'backlog' && onAddClick && (
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 hover:bg-primary/10 hover:text-primary transition-colors"
            onClick={onAddClick}
          >
            <Plus className="h-4 w-4" />
          </Button>
        )}
        {status === 'done' && onArchiveAll && tasks.length > 0 && !showArchived && (
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 hover:bg-muted-foreground/10 hover:text-muted-foreground transition-colors"
            onClick={onArchiveAll}
            title={t('tooltips.archiveAllDone')}
          >
            <Archive className="h-4 w-4" />
          </Button>
        )}
        {status === 'done' && archivedCount !== undefined && archivedCount > 0 && onToggleArchived && (
          <Tooltip delayDuration={200}>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className={cn(
                  'h-7 w-7 transition-colors relative',
                  showArchived
                    ? 'text-primary bg-primary/10 hover:bg-primary/20'
                    : 'hover:bg-muted-foreground/10 hover:text-muted-foreground'
                )}
                onClick={onToggleArchived}
                aria-pressed={showArchived}
              >
                <Archive className="h-4 w-4" />
                <span className="absolute -top-1 -right-1 text-xs font-medium bg-muted rounded-full min-w-[14px] h-[14px] flex items-center justify-center">
                  {archivedCount}
                </span>
              </Button>
            </TooltipTrigger>
            <TooltipContent>
              {showArchived ? t('common:projectTab.hideArchived') : t('common:projectTab.showArchived')}
            </TooltipContent>
          </Tooltip>
        )}
      </div>

      <SortableContext items={taskIds} strategy={verticalListSortingStrategy}>
        <WorkColumn
          title={TASK_STATUS_LABELS[status]}
          tone={columnTone(status)}
          projectId={projectId}
          emptyLabel={emptyLabelForStatus(status, t)}
          showMoreLabel={(count) => t('tasks:pipeline.showMore', { count, defaultValue: `Show ${count} more` })}
          items={tasks.map((task) => {
            const done = task.status === 'done' || Boolean(task.metadata?.archivedAt);
            return {
              id: task.id,
              projectId: task.projectId,
              collapsedByDefault: done,
              render: ({ expanded, onToggle }) => (
                <SortableTaskCard
                  key={task.id}
                  task={task}
                  onClick={onClickHandlers.get(task.id)!}
                  expanded={expanded}
                  onToggle={onToggle}
                  collapsedByDefault={done}
                />
              ),
            };
          })}
        />
      </SortableContext>
    </div>
  );
}, droppableColumnPropsAreEqual);

export function KanbanBoard({
  tasks,
  projectId,
  onTaskClick,
  onNewTaskClick,
  onRefresh,
  isRefreshing,
  isInitialized,
}: KanbanBoardProps) {
  const { t } = useTranslation('tasks');
  const [activeTask, setActiveTask] = useState<Task | null>(null);
  const [overColumnId, setOverColumnId] = useState<string | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  const toggleShowArchived = () => setShowArchived((prev) => !prev);

  const archivedCount = useMemo(() => tasks.filter((task) => task.metadata?.archivedAt).length, [tasks]);

  const filteredTasks = useMemo(() => {
    if (showArchived) return tasks;
    return tasks.filter((task) => !task.metadata?.archivedAt);
  }, [tasks, showArchived]);

  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: { distance: 8 },
    }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    })
  );

  type KanbanColumn = (typeof TASK_STATUS_COLUMNS)[number];

  const tasksByStatus = useMemo(() => {
    const grouped: Record<KanbanColumn, Task[]> = {
      backlog: [],
      in_progress: [],
      ai_review: [],
      human_review: [],
      done: [],
    };

    filteredTasks.forEach((task) => {
      const column: KanbanColumn =
        task.status === 'copilot_running' || task.status === 'copilot_pr_opened'
          ? 'in_progress'
          : task.status;
      if (grouped[column] !== undefined) {
        grouped[column].push(task);
      }
    });

    Object.keys(grouped).forEach((status) => {
      grouped[status as KanbanColumn].sort((a, b) => {
        const dateA = new Date(a.createdAt).getTime();
        const dateB = new Date(b.createdAt).getTime();
        return dateB - dateA;
      });
    });

    return grouped;
  }, [filteredTasks]);

  const handleArchiveAll = async () => {
    const targetProjectId = projectId ?? tasks[0]?.projectId;
    if (!targetProjectId) {
      toast({ variant: 'destructive', title: t('kanban.archiveFailed'), description: 'No project found' });
      return;
    }

    const doneTaskIds = tasksByStatus.done.map((task) => task.id);
    if (doneTaskIds.length === 0) return;

    const result = await archiveTasks(targetProjectId, doneTaskIds);
    if (!result.success) {
      toast({ variant: 'destructive', title: t('kanban.archiveFailed'), description: result.error });
    } else {
      toast({ title: t('kanban.archiveSuccess'), description: `${doneTaskIds.length} task(s) archived` });
    }
  };

  const handleDragStart = (event: DragStartEvent) => {
    const task = tasks.find((tItem) => tItem.id === event.active.id);
    if (task) setActiveTask(task);
  };

  const handleDragOver = (event: DragOverEvent) => {
    const { over } = event;
    if (!over) {
      setOverColumnId(null);
      return;
    }

    const overId = over.id as string;
    if (TASK_STATUS_COLUMNS.includes(overId as KanbanColumn)) {
      setOverColumnId(overId);
      return;
    }

    const overTask = tasks.find((tItem) => tItem.id === overId);
    if (overTask) setOverColumnId(overTask.status);
  };

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    setActiveTask(null);
    setOverColumnId(null);

    if (!over) return;

    const activeTaskId = active.id as string;
    const overId = over.id as string;

    if (TASK_STATUS_COLUMNS.includes(overId as KanbanColumn)) {
      const newStatus = overId as KanbanColumn;
      const task = tasks.find((tItem) => tItem.id === activeTaskId);
      if (task && task.status !== newStatus) {
        void persistTaskStatus(activeTaskId, newStatus);
      }
      return;
    }

    const overTask = tasks.find((tItem) => tItem.id === overId);
    if (overTask) {
      const task = tasks.find((tItem) => tItem.id === activeTaskId);
      if (task && task.status !== overTask.status) {
        void persistTaskStatus(activeTaskId, overTask.status);
      }
    }
  };

  return (
    <div className="flex h-full flex-col">
      {onRefresh && (
        <div className="flex items-center justify-end px-6 pt-4 pb-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={onRefresh}
            disabled={isRefreshing}
            className="gap-2 text-muted-foreground hover:text-foreground"
          >
            <RefreshCw className={cn('h-4 w-4', isRefreshing && 'animate-spin')} />
            {isRefreshing ? 'Refreshing...' : 'Refresh Tasks'}
          </Button>
        </div>
      )}
      {isInitialized === false ? (
        <div className="flex flex-1 gap-3 overflow-x-auto p-4">
          {TASK_STATUS_COLUMNS.map((status, i) => (
            <div
              key={status}
              className="flex min-w-52 max-w-[22rem] flex-1 flex-col rounded-xl border border-border bg-card p-4"
            >
              <KanbanColumnSkeleton cards={i === 0 ? 3 : i < 3 ? 2 : 1} />
            </div>
          ))}
        </div>
      ) : (
        <DndContext
          sensors={sensors}
          collisionDetection={closestCorners}
          onDragStart={handleDragStart}
          onDragOver={handleDragOver}
          onDragEnd={handleDragEnd}
        >
          <div className="flex flex-1 gap-3 overflow-x-auto p-4">
            {TASK_STATUS_COLUMNS.map((status) => (
              <DroppableColumn
                key={status}
                status={status}
                projectId={projectId}
                tasks={tasksByStatus[status]}
                onTaskClick={onTaskClick}
                isOver={overColumnId === status}
                onAddClick={status === 'backlog' && isInitialized ? onNewTaskClick : undefined}
                onArchiveAll={status === 'done' ? () => { void handleArchiveAll(); } : undefined}
                archivedCount={status === 'done' ? archivedCount : undefined}
                showArchived={status === 'done' ? showArchived : undefined}
                onToggleArchived={status === 'done' ? toggleShowArchived : undefined}
              />
            ))}
          </div>

          <DragOverlay>
            {activeTask ? (
              <div className="drag-overlay-card">
                <TaskCard task={activeTask} onClick={() => {}} />
              </div>
            ) : null}
          </DragOverlay>
        </DndContext>
      )}
    </div>
  );
}
