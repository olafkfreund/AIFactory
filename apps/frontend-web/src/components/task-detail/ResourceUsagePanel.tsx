/**
 * ResourceUsagePanel — Live CPU / RAM display for a running task.
 *
 * Consumes: GET /api/tasks/{id}/resource-usage
 * Response:
 *   { running: boolean; pid: number|null; cpuPercent: number;
 *     memoryMb: number; memoryPercent: number; sampledAt: string }
 *
 * Polls every 3 seconds while the component is mounted.
 * Interval is cleared on unmount to prevent memory leaks.
 *
 * NOTE: The /resource-usage endpoint is being built in parallel (issue #277).
 * This component codes against the contract above and gracefully degrades
 * to an empty state if the endpoint is not yet merged.
 */

import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Cpu, MemoryStick, Activity } from 'lucide-react';
import { get } from '../../lib/api-client';
import { cn } from '../../lib/utils';

interface ResourceUsageData {
  running: boolean;
  pid: number | null;
  cpuPercent: number;
  memoryMb: number;
  memoryPercent: number;
  sampledAt: string;
}

interface ResourceUsagePanelProps {
  taskId: string;
  /** Whether to actively poll (true while task detail is open). Default true. */
  active?: boolean;
}

const POLL_INTERVAL_MS = 3000;

function GaugeBar({
  value,
  className,
}: {
  value: number;
  className?: string;
}) {
  const pct = Math.min(100, Math.max(0, value));
  const color =
    pct >= 90 ? 'bg-destructive' :
    pct >= 70 ? 'bg-yellow-500' :
    'bg-primary';

  return (
    <div className={cn('h-1.5 w-full rounded-full bg-muted overflow-hidden', className)}>
      <div
        className={cn('h-full rounded-full transition-all duration-500', color)}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

export function ResourceUsagePanel({ taskId, active = true }: ResourceUsagePanelProps) {
  const { t } = useTranslation(['tasks']);
  const [data, setData] = useState<ResourceUsageData | null>(null);
  const [loading, setLoading] = useState(true);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchData = () => {
    get<ResourceUsageData>(`/tasks/${encodeURIComponent(taskId)}/resource-usage`)
      .then((result) => {
        if (result.success && result.data) {
          setData(result.data);
        }
        // On error, keep stale data rather than clearing it
      })
      .catch(() => {})
      .finally(() => { setLoading(false); });
  };

  useEffect(() => {
    if (!active) return;

    // Immediate first fetch
    fetchData();

    // Set up polling
    intervalRef.current = setInterval(fetchData, POLL_INTERVAL_MS);

    return () => {
      if (intervalRef.current !== null) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
    // taskId is a primitive, safe as dep
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId, active]);

  if (loading) {
    return (
      <div className="space-y-3 animate-pulse">
        <div className="h-4 w-24 bg-muted rounded" />
        <div className="h-2 w-full bg-muted rounded-full" />
        <div className="h-4 w-32 bg-muted rounded" />
        <div className="h-2 w-full bg-muted rounded-full" />
      </div>
    );
  }

  // Not running / no data yet
  if (!data || !data.running) {
    return (
      <div className="flex items-center gap-2 py-3 text-sm text-muted-foreground">
        <Activity className="h-4 w-4 text-muted-foreground/40" />
        <span>{t('tasks:resourceUsage.notRunning')}</span>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* PID badge */}
      {data.pid !== null && (
        <p className="text-xs text-muted-foreground font-mono">
          {t('tasks:resourceUsage.pid', { pid: data.pid })}
        </p>
      )}

      {/* CPU */}
      <div className="space-y-1.5">
        <div className="flex items-center justify-between text-xs">
          <span className="text-foreground flex items-center gap-1.5">
            <Cpu className="h-3.5 w-3.5 text-muted-foreground" />
            {t('tasks:resourceUsage.cpu')}
          </span>
          <span className="font-mono text-muted-foreground">
            {data.cpuPercent.toFixed(1)}%
          </span>
        </div>
        <GaugeBar value={data.cpuPercent} />
      </div>

      {/* RAM */}
      <div className="space-y-1.5">
        <div className="flex items-center justify-between text-xs">
          <span className="text-foreground flex items-center gap-1.5">
            <MemoryStick className="h-3.5 w-3.5 text-muted-foreground" />
            {t('tasks:resourceUsage.memory')}
          </span>
          <span className="font-mono text-muted-foreground">
            {data.memoryMb.toFixed(0)} MB ({data.memoryPercent.toFixed(1)}%)
          </span>
        </div>
        <GaugeBar value={data.memoryPercent} />
      </div>

      {/* Sampled-at */}
      <p className="text-xs text-muted-foreground/50 text-right">
        {t('tasks:resourceUsage.sampledAt', {
          time: new Date(data.sampledAt).toLocaleTimeString([], {
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
          }),
        })}
      </p>
    </div>
  );
}
