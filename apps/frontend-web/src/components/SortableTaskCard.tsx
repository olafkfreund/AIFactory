import { memo, useCallback } from 'react';
import { useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { TaskCard } from './TaskCard';
import { cn } from '../lib/utils';
import type { Task } from '../shared/types';

interface SortableTaskCardProps {
  task: Task;
  onClick: () => void;
  expanded?: boolean | undefined;
  onToggle?: (() => void) | undefined;
  collapsedByDefault?: boolean | undefined;
}

// Custom comparator - only re-render when task or expansion props actually changed
function sortableTaskCardPropsAreEqual(
  prevProps: SortableTaskCardProps,
  nextProps: SortableTaskCardProps
): boolean {
  return (
    prevProps.task === nextProps.task &&
    prevProps.onClick === nextProps.onClick &&
    prevProps.expanded === nextProps.expanded &&
    prevProps.onToggle === nextProps.onToggle &&
    prevProps.collapsedByDefault === nextProps.collapsedByDefault
  );
}

export const SortableTaskCard = memo(function SortableTaskCard({
  task,
  onClick,
  expanded,
  onToggle,
  collapsedByDefault,
}: SortableTaskCardProps) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
    isOver,
  } = useSortable({ id: task.id });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    zIndex: isDragging ? 50 : undefined,
  };

  const handleClick = useCallback(() => {
    onClick();
  }, [onClick]);

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={cn(
        'touch-none transition-all duration-200',
        isDragging && 'dragging-placeholder opacity-40 scale-[0.98]',
        isOver && !isDragging && 'ring-2 ring-primary/30 ring-offset-2 ring-offset-background rounded-xl'
      )}
      {...attributes}
      {...listeners}
    >
      <TaskCard
        task={task}
        onClick={handleClick}
        expanded={expanded}
        onToggle={onToggle}
        collapsedByDefault={collapsedByDefault}
      />
    </div>
  );
}, sortableTaskCardPropsAreEqual);
