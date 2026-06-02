/**
 * TokenUsagePanel — Per-category token breakdown for a running / completed task.
 *
 * Consumes:  GET /api/tasks/{id}/token-usage
 * Response:  { version, turns, model, maxTokens, totalInputTokens, outputTokens,
 *              totalTokens, totalCostUsd, pctOfWindow,
 *              categories: Array<{ key, label, color, tokens, pctOfWindow, costUsd }>,
 *              updatedAt }
 *
 * Empty-state is shown when the endpoint returns no data yet.
 */

import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Coins, TrendingUp } from 'lucide-react';
import { get } from '../../lib/api-client';
import { cn } from '../../lib/utils';

interface TokenCategory {
  key: string;
  label: string;
  color: string;
  tokens: number;
  pctOfWindow: number;
  costUsd: number;
}

interface TokenUsageData {
  categories: TokenCategory[];
  totalTokens: number;
  maxTokens: number;
  totalCostUsd: number;
  pctOfWindow: number;
}

interface TokenUsagePanelProps {
  taskId: string;
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function formatCost(n: number): string {
  if (n === 0) return '$0.00';
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

export function TokenUsagePanel({ taskId }: TokenUsagePanelProps) {
  const { t } = useTranslation(['tasks']);
  const [data, setData] = useState<TokenUsageData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);

    get<TokenUsageData>(`/tasks/${encodeURIComponent(taskId)}/token-usage`)
      .then((result) => {
        if (cancelled) return;
        if (result.success && result.data) {
          setData(result.data);
        } else {
          setData(null);
        }
      })
      .catch(() => {
        if (!cancelled) setData(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => { cancelled = true; };
  }, [taskId]);

  if (loading) {
    return (
      <div className="space-y-2 animate-pulse">
        <div className="h-4 w-32 bg-muted rounded" />
        <div className="h-4 w-48 bg-muted rounded" />
        <div className="h-4 w-40 bg-muted rounded" />
      </div>
    );
  }

  if (!data || !data.categories || data.categories.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-6 text-center gap-2">
        <Coins className="h-8 w-8 text-muted-foreground/40" />
        <p className="text-sm text-muted-foreground">
          {t('tasks:tokenUsage.empty')}
        </p>
      </div>
    );
  }

  const windowPct = data.maxTokens > 0
    ? Math.min(100, Math.round((data.totalTokens / data.maxTokens) * 100))
    : 0;

  return (
    <div className="space-y-4">
      {/* Summary row */}
      <div className="flex items-center justify-between text-sm">
        <span className="text-muted-foreground flex items-center gap-1.5">
          <TrendingUp className="h-4 w-4" />
          {formatTokens(data.totalTokens)}
          {data.maxTokens > 0 && (
            <span className="text-xs text-muted-foreground/70">
              {' '}/ {formatTokens(data.maxTokens)} ({windowPct}%)
            </span>
          )}
        </span>
        <span className="font-mono text-xs text-muted-foreground">
          {formatCost(data.totalCostUsd)}
        </span>
      </div>

      {/* Context window bar */}
      {data.maxTokens > 0 && (
        <div className="h-1.5 w-full rounded-full bg-muted overflow-hidden">
          <div
            className={cn(
              'h-full rounded-full transition-all',
              windowPct >= 90 ? 'bg-destructive' :
              windowPct >= 70 ? 'bg-yellow-500' :
              'bg-primary'
            )}
            style={{ width: `${windowPct}%` }}
          />
        </div>
      )}

      {/* Per-category breakdown */}
      <div className="space-y-2">
        {data.categories.map((cat) => {
          const catPct = data.totalTokens > 0
            ? Math.round((cat.tokens / data.totalTokens) * 100)
            : 0;
          return (
            <div key={cat.key} className="space-y-1">
              <div className="flex items-center justify-between text-xs">
                <span className="text-foreground">{cat.label}</span>
                <span className="font-mono text-muted-foreground">
                  {formatTokens(cat.tokens)} &middot; {catPct}% &middot; {formatCost(cat.costUsd)}
                </span>
              </div>
              <div className="h-1 w-full rounded-full bg-muted overflow-hidden">
                <div
                  className="h-full rounded-full transition-all"
                  style={{ width: `${catPct}%`, backgroundColor: cat.color }}
                />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
