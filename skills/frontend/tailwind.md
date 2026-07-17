# tailwind

> Source: curated best practices | 2026

---

# Tailwind CSS - Utility-first styling with Tailwind 4

This skill equips the coder to style UI with Tailwind CSS 4, using its CSS-first configuration (`@import "tailwindcss"` and `@theme` in CSS instead of `tailwind.config.js`), design tokens via CSS variables, responsive/state/dark-mode variants, container queries, and component extraction through framework components (not premature `@apply`). It enforces mobile-first ordering, semantic color tokens, accessible focus styles, and consistent spacing/typography scales. Arbitrary-value soup, deeply nested `@apply` abstractions, and fighting the design system are avoided.

## When to Activate

Use when styling UI with Tailwind:
- Repo has `tailwindcss` in `package.json` and `@import "tailwindcss"` (v4) or `tailwind.config.*` (v3)
- Task mentions Tailwind utility classes, responsive design, dark mode, design tokens
- Building or restyling any HTML/JSX/Vue/Svelte markup with utility classes

## Patterns and Best Practices

### Tailwind 4 CSS-first setup

```css
/* app.css — v4 replaces most of tailwind.config.js with CSS */
@import "tailwindcss";

@theme {
  --color-brand: oklch(0.62 0.19 255);
  --color-brand-fg: oklch(0.98 0 0);
  --font-sans: "Inter", system-ui, sans-serif;
  --spacing: 0.25rem; /* base spacing unit */
}

/* custom variant example */
@custom-variant hocus (&:hover, &:focus-visible);
```

Tokens defined in `@theme` become utilities automatically: `bg-brand`, `text-brand-fg`, `font-sans`.

### Mobile-first responsive design

```html
<!-- unprefixed = smallest; each prefix layers the next breakpoint up -->
<div class="grid grid-cols-1 gap-4
            sm:grid-cols-2
            lg:grid-cols-3
            xl:grid-cols-4">
  <!-- cards -->
</div>
```

Start with the base (mobile) styles, then add `sm: md: lg: xl: 2xl:` overrides. Never write `max-*` variants when a mobile-first `min-*` order reads cleaner.

### State variants and accessible focus

```html
<button
  class="rounded bg-brand px-4 py-2 text-brand-fg
         hover:bg-brand/90
         focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand
         disabled:cursor-not-allowed disabled:opacity-50">
  Save
</button>
```

Always keep a visible `focus-visible` style — never `outline-none` without a replacement. Use opacity modifiers (`bg-brand/90`) instead of separate color tokens for hover shades.

### Dark mode

```css
/* v4: opt into class-based dark mode via a custom variant */
@custom-variant dark (&:where(.dark, .dark *));
```

```html
<div class="bg-white text-gray-900
            dark:bg-gray-900 dark:text-gray-100">
  Theme-aware surface
</div>
```

Prefer semantic tokens (`bg-surface`, `text-muted`) mapped in `@theme` so dark mode is a token swap, not per-element `dark:` sprawl.

### Container queries (built into v4)

```html
<div class="@container">
  <div class="grid grid-cols-1 @md:grid-cols-2 @lg:grid-cols-3">
    <!-- responds to the container's width, not the viewport -->
  </div>
</div>
```

### Component extraction — reuse via components, not @apply

```tsx
// Extract repetition into a framework component, keeping utilities inline
function Card({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
      {children}
    </div>
  );
}
```

Reach for `@apply` only for a genuinely shared primitive that has no component home (e.g. a global `.prose` tweak):

```css
@layer components {
  .btn { @apply rounded px-4 py-2 font-medium; }
}
```

### Conditional classes

```tsx
import clsx from 'clsx'; // or tailwind-merge for conflict resolution
<button className={clsx('btn', isActive ? 'bg-brand text-white' : 'bg-gray-100')} />
```

Use `tailwind-merge` when composing class strings that may conflict (`px-2` vs `px-4`) so the last wins predictably.

### Consistency and accessibility

- Stick to the spacing/typography scale (`p-4`, `text-lg`) — avoid arbitrary values like `p-[13px]` unless a design demands it.
- Maintain WCAG AA contrast; verify token pairs (e.g. `text-muted` on `bg-surface`).
- Use `sr-only` for visually hidden but screen-reader-available text.

## Anti-patterns

- Do not fill markup with arbitrary values (`w-[437px]`, `text-[#3b82f6]`) — use scale tokens and `@theme` colors.
- Do not `@apply` dozens of utilities to recreate a component — extract a framework component instead.
- Do not remove focus outlines (`outline-none`) without providing a `focus-visible` replacement.
- Do not scatter `dark:` on every element — map semantic tokens once in `@theme`.
- Do not concatenate dynamic class strings that Tailwind can't statically detect (e.g. `` `text-${color}-500` ``) — the class gets purged; use full class names or a lookup map.
- Do not fight the design system with one-off inline `style=` — extend the theme.
- Do not use `max-*` breakpoint variants when mobile-first `min-*` ordering is clearer.
