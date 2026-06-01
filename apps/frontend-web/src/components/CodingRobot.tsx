/**
 * CodingRobot — a small, playful-but-tasteful robot that appears on a task card
 * while its **coding** phase is active and waves goodbye when it ends.
 *
 * Purely presentational: driven by a single `active` boolean (the card passes
 * `executionProgress.phase === 'coding'`). Enter = pop-in bounce + glow; idle =
 * gentle bob + occasional blink; exit = a quick wave + fade-out via AnimatePresence.
 */

import { AnimatePresence, motion } from 'motion/react';
import { Bot, Sparkles } from 'lucide-react';

interface CodingRobotProps {
  /** True while the task is in its coding phase. */
  active: boolean;
  /** Optional accessible / tooltip label. */
  label?: string;
}

export function CodingRobot({ active, label = 'AI is coding…' }: CodingRobotProps) {
  return (
    <AnimatePresence mode="wait">
      {active && (
        <motion.span
          key="coding-robot"
          title={label}
          aria-label={label}
          role="img"
          className="relative inline-flex h-5 w-5 items-center justify-center align-middle"
          // Enter: spring pop-in with a little overshoot
          initial={{ scale: 0, opacity: 0, rotate: -35 }}
          animate={{ scale: 1, opacity: 1, rotate: 0 }}
          // Exit: a quick goodbye wave, then shrink + fade
          exit={{
            rotate: [0, 18, -12, 16, 0],
            scale: [1, 1.1, 0.4],
            opacity: [1, 1, 0],
            transition: { duration: 0.55, ease: 'easeInOut' },
          }}
          transition={{ type: 'spring', stiffness: 520, damping: 16 }}
        >
          {/* Soft pulsing glow ring (uses the theme primary = Gruvbox yellow) */}
          <motion.span
            className="absolute inset-0 rounded-full bg-primary/40 blur-[3px]"
            animate={{ scale: [1, 1.7, 1], opacity: [0.55, 0, 0.55] }}
            transition={{ duration: 1.7, repeat: Infinity, ease: 'easeInOut' }}
          />
          {/* The bot itself: gentle bob + occasional blink */}
          <motion.span
            className="relative text-primary"
            animate={{ y: [0, -2.5, 0], opacity: [1, 1, 1, 1, 0.35, 1] }}
            transition={{
              y: { duration: 1.4, repeat: Infinity, ease: 'easeInOut' },
              opacity: { duration: 3.2, repeat: Infinity, times: [0, 0.6, 0.7, 0.85, 0.9, 1] },
            }}
          >
            <Bot className="h-4 w-4" strokeWidth={2.25} />
          </motion.span>
          {/* A tiny sparkle that twinkles top-right */}
          <motion.span
            className="absolute -right-1 -top-1 text-accent"
            animate={{ scale: [0, 1, 0], rotate: [0, 90, 180], opacity: [0, 1, 0] }}
            transition={{ duration: 2.2, repeat: Infinity, ease: 'easeInOut', delay: 0.4 }}
          >
            <Sparkles className="h-2 w-2" />
          </motion.span>
        </motion.span>
      )}
    </AnimatePresence>
  );
}
