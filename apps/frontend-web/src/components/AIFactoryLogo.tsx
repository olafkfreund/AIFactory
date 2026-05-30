/**
 * AIFactoryLogo — a compact, theme-aware brand mark.
 *
 * Replaces the old raster `logo.png` (a blue circuit-board image that clashed
 * with the Gruvbox palette). This is a vector mark: a factory glyph knocked out
 * of a primary→accent gradient badge, so it always matches the active theme
 * (Gruvbox yellow→orange in dark, orange in light) and stays crisp at any size.
 *
 * Size is controlled by the caller via `className` (e.g. `h-7 w-7`).
 */

import { Factory } from 'lucide-react';
import { cn } from '../lib/utils';

interface AIFactoryLogoProps {
  className?: string;
}

export function AIFactoryLogo({ className }: AIFactoryLogoProps) {
  return (
    <span
      role="img"
      aria-label="AIFactory"
      className={cn(
        'inline-flex items-center justify-center rounded-lg',
        'bg-gradient-to-br from-primary to-accent text-background',
        'ring-1 ring-primary/30 shadow-sm',
        className
      )}
    >
      <Factory className="h-3/5 w-3/5" strokeWidth={2.25} aria-hidden="true" />
    </span>
  );
}
