import type { ITheme } from '@xterm/xterm';

/**
 * Builds an xterm.js theme from the app's live CSS variables so the terminal
 * tracks the active theme (Gruvbox / shadcn, light / dark) instead of being
 * hardcoded dark — which clashed badly in light mode.
 *
 * Background / foreground / cursor / selection are derived from CSS tokens;
 * the 16-color ANSI palette is curated per light/dark for legibility (token
 * vars don't cover the full ANSI range).
 */

function readVar(name: string): string {
  if (typeof document === 'undefined') return '';
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** `--background: "48 85% 88%"` -> `"hsl(48 85% 88%)"` (optionally with alpha). */
function hslVar(name: string, alpha?: number): string | undefined {
  const raw = readVar(name);
  if (!raw) return undefined;
  return alpha === undefined ? `hsl(${raw})` : `hsl(${raw} / ${alpha})`;
}

function isDark(): boolean {
  return typeof document !== 'undefined' && document.documentElement.classList.contains('dark');
}

// Legible ANSI palettes that read well on a dark vs light surface.
const ANSI_DARK = {
  black: '#1A1A1F',
  red: '#FF6B6B',
  green: '#87D687',
  yellow: '#E8C547',
  blue: '#6BB3FF',
  magenta: '#C792EA',
  cyan: '#89DDFF',
  white: '#E8E6E3',
  brightBlack: '#5C5C66',
  brightRed: '#FF8A8A',
  brightGreen: '#A5E6A5',
  brightYellow: '#F2DA7A',
  brightBlue: '#8AC4FF',
  brightMagenta: '#DEB3FF',
  brightCyan: '#A6E8FF',
  brightWhite: '#FFFFFF',
};

const ANSI_LIGHT = {
  black: '#3C3836',
  red: '#CC241D',
  green: '#79740E',
  yellow: '#B57614',
  blue: '#076678',
  magenta: '#8F3F71',
  cyan: '#427B58',
  white: '#7C6F64',
  brightBlack: '#928374',
  brightRed: '#9D0006',
  brightGreen: '#98971A',
  brightYellow: '#D79921',
  brightBlue: '#458588',
  brightMagenta: '#B16286',
  brightCyan: '#689D6A',
  brightWhite: '#3C3836',
};

export function getTerminalTheme(): ITheme {
  const dark = isDark();
  const ansi = dark ? ANSI_DARK : ANSI_LIGHT;

  // In dark mode prefer the deep popover surface for a premium near-black
  // terminal; in light mode use the card surface so it blends with the app.
  const background =
    (dark ? hslVar('--popover') : hslVar('--card')) ?? (dark ? '#0B0B0F' : '#FBF1C7');
  const foreground = hslVar('--foreground') ?? (dark ? '#E8E6E3' : '#3C3836');
  const cursor = hslVar('--primary') ?? (dark ? '#D6D876' : '#D65D0E');

  return {
    background,
    foreground,
    cursor,
    cursorAccent: background,
    selectionBackground: hslVar('--primary', 0.28) ?? 'rgba(214, 216, 118, 0.25)',
    selectionForeground: foreground,
    ...ansi,
  };
}
