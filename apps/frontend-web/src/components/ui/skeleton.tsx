import * as React from 'react';
import { cn } from '../../lib/utils';

/**
 * Skeleton — animated shimmer placeholder used while data loads.
 * Replaces bare spinners for a more modern, perceived-faster load.
 * The shimmer + reduced-motion handling live in `.skeleton` (index.css).
 */
function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('skeleton', className)} aria-hidden="true" {...props} />;
}

/**
 * SkeletonText — N shimmer lines, last line shortened for a natural look.
 */
function SkeletonText({
  lines = 3,
  className,
}: {
  lines?: number;
  className?: string;
}) {
  return (
    <div className={cn('space-y-2', className)} aria-hidden="true">
      {Array.from({ length: lines }).map((_, i) => (
        <Skeleton
          key={i}
          className={cn('h-3.5', i === lines - 1 ? 'w-2/3' : 'w-full')}
        />
      ))}
    </div>
  );
}

/**
 * TaskCardSkeleton — matches the shape of a Kanban task card so columns
 * don't jump when real cards replace the placeholders.
 */
function TaskCardSkeleton() {
  return (
    <div
      className="rounded-xl border border-border bg-card p-4 space-y-3"
      aria-hidden="true"
    >
      <div className="flex items-center justify-between gap-2">
        <Skeleton className="h-4 w-3/5" />
        <Skeleton className="h-5 w-14 rounded-full" />
      </div>
      <SkeletonText lines={2} />
      <div className="flex items-center gap-2 pt-1">
        <Skeleton className="h-5 w-16 rounded-full" />
        <Skeleton className="h-5 w-12 rounded-full" />
        <Skeleton className="ml-auto h-3 w-20" />
      </div>
    </div>
  );
}

/**
 * KanbanColumnSkeleton — a column header plus a few card skeletons.
 */
function KanbanColumnSkeleton({ cards = 2 }: { cards?: number }) {
  return (
    <div className="flex w-full flex-col gap-3" aria-hidden="true">
      <div className="flex items-center gap-2 px-1">
        <Skeleton className="h-4 w-24" />
        <Skeleton className="h-5 w-6 rounded-full" />
      </div>
      {Array.from({ length: cards }).map((_, i) => (
        <TaskCardSkeleton key={i} />
      ))}
    </div>
  );
}

export { Skeleton, SkeletonText, TaskCardSkeleton, KanbanColumnSkeleton };
