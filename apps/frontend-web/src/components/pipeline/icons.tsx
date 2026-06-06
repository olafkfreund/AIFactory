/**
 * Hand-drawn pipeline icons (inline SVG, stroke = currentColor so each inherits
 * its stage color). Deliberately characterful — a friendly robot head, a folded
 * plan page, an Erlenmeyer flask — to match the Plan → Code → Test mockup.
 */
import type { SVGProps } from 'react';

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

const base = (size: number) => ({
  width: size,
  height: size,
  viewBox: '0 0 48 48',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 2,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
});

/** Plan: a document with a folded corner + text lines. */
export function PlanDocIcon({ size = 28, ...rest }: IconProps) {
  return (
    <svg {...base(size)} {...rest}>
      <path d="M13 6h15l7 7v29H13z" />
      <path d="M28 6v7h7" />
      <path d="M18 22h12M18 28h12M18 34h8" className="doc-lines" />
    </svg>
  );
}

/** Code: a rounded robot head — antenna dot, two round eyes, a mouth grille. */
export function RobotHeadIcon({ size = 28, ...rest }: IconProps) {
  return (
    <svg {...base(size)} {...rest} className={`robot ${rest.className ?? ''}`}>
      {/* antenna */}
      <line x1="24" y1="7" x2="24" y2="12" />
      <circle cx="24" cy="5.5" r="1.8" fill="currentColor" stroke="none" className="robot-antenna" />
      {/* head */}
      <rect x="9" y="12" width="30" height="24" rx="7" />
      {/* eyes */}
      <circle cx="18.5" cy="22" r="3.2" className="robot-eye" />
      <circle cx="29.5" cy="22" r="3.2" className="robot-eye" />
      {/* mouth grille */}
      <path d="M17 29h14" />
      <path d="M20 29v3M24 29v3M28 29v3" />
    </svg>
  );
}

/** Test: an Erlenmeyer flask with liquid + rising bubbles. */
export function FlaskIcon({ size = 28, ...rest }: IconProps) {
  return (
    <svg {...base(size)} {...rest}>
      <path d="M20 6h8M21 6v12L12 36a4 4 0 0 0 3.6 6h16.8A4 4 0 0 0 36 36l-9-18V6" />
      {/* liquid */}
      <path d="M16.5 30h15l3.5 6.6a3 3 0 0 1-2.7 4.4H15.7a3 3 0 0 1-2.7-4.4z"
        fill="currentColor" fillOpacity="0.18" stroke="none" />
      <path d="M16.5 30h15" />
      {/* bubbles */}
      <circle cx="22" cy="36" r="1.1" fill="currentColor" stroke="none" className="bubble bubble-1" />
      <circle cx="26.5" cy="38" r="0.9" fill="currentColor" stroke="none" className="bubble bubble-2" />
      <circle cx="24" cy="34" r="0.8" fill="currentColor" stroke="none" className="bubble bubble-3" />
    </svg>
  );
}

/** A small terminal window — one per active agent/subtask. */
export function TerminalIcon({ size = 20, ...rest }: IconProps) {
  return (
    <svg {...base(size)} {...rest} strokeWidth={2.4}>
      <rect x="5" y="9" width="38" height="30" rx="4" />
      <path d="M13 19l5 4-5 4" />
      <line x1="24" y1="31" x2="33" y2="31" className="term-cursor" />
    </svg>
  );
}

/** Broadcast/“MCP fetch” signal — concentric arcs from a dot. */
export function SignalIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...base(size)} {...rest} strokeWidth={2.2}>
      <circle cx="14" cy="34" r="2.4" fill="currentColor" stroke="none" />
      <path d="M14 26a8 8 0 0 1 8 8" className="sig sig-1" />
      <path d="M14 19a15 15 0 0 1 15 15" className="sig sig-2" />
      <path d="M14 12a22 22 0 0 1 22 22" className="sig sig-3" />
    </svg>
  );
}
