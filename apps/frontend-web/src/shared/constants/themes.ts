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
    description: 'Retro groove — warm, earthy tones · AIFactory yellow accent',
    previewColors: { bg: '#fbf1c7', accent: '#b57614', darkBg: '#282828', darkAccent: '#fabd2f' }
  },
];
