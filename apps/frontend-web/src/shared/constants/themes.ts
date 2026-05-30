/**
 * Theme constants
 * Gruvbox is the sole color theme for AIFactory (warm, retro-groove, earthy).
 */

import type { ColorThemeDefinition } from '../types/settings';

export const COLOR_THEMES: ColorThemeDefinition[] = [
  {
    id: 'gruvbox',
    name: 'Gruvbox',
    description: 'Retro groove — warm, earthy tones',
    previewColors: { bg: '#fbf1c7', accent: '#d65d0e', darkBg: '#282828', darkAccent: '#fabd2f' }
  },
];
