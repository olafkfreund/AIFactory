/**
 * Theme constants
 * Gruvbox is the default color theme (warm, retro-groove, earthy).
 * Additional palettes can be switched to from Settings > Appearance.
 */

import type { ColorThemeDefinition } from '../types/settings';

export const COLOR_THEMES: ColorThemeDefinition[] = [
  {
    id: 'gruvbox',
    name: 'Gruvbox',
    description: 'Retro groove — warm, earthy tones',
    previewColors: { bg: '#fbf1c7', accent: '#d65d0e', darkBg: '#282828', darkAccent: '#fabd2f' }
  },
  {
    id: 'shadcn',
    name: 'Mira (shadcn)',
    description: 'Clean neutral base with a yellow accent — shadcn/ui',
    previewColors: { bg: '#ffffff', accent: '#ffc800', darkBg: '#0a0a0a', darkAccent: '#f0b500' }
  },
];
