# svelte

> Source: curated best practices | 2026

---

# Svelte - Svelte 5 runes and SvelteKit

This skill equips the coder to build Svelte 5 components using the runes reactivity model (`$state`, `$derived`, `$effect`, `$props`, `$bindable`), reusable logic in `.svelte.ts` modules, and SvelteKit for routing, load functions, and form actions. It enforces the runes rules (fine-grained signals, derived-not-manual, effects only for external sync), accessible markup that the Svelte compiler will lint, and Vitest + Testing Library + Playwright tests. The legacy `$:` reactive statements, `export let` props, and stores-for-everything patterns are avoided in favor of runes.

## When to Activate

Use when building UI with Svelte:
- Files are `.svelte` or `.svelte.ts`, or repo has `svelte.config.js` / SvelteKit `src/routes`
- Task mentions runes (`$state`, `$derived`, `$effect`), SvelteKit, load functions, form actions
- `package.json` lists `svelte` >= 5
- Building Svelte components or SvelteKit apps

## Patterns and Best Practices

### Component with runes

```svelte
<script lang="ts">
  let { label, disabled = false }: { label: string; disabled?: boolean } = $props();

  let count = $state(0);
  let doubled = $derived(count * 2); // recomputes automatically, no manual sync

  function increment() {
    count += 1;
  }
</script>

<button onclick={increment} {disabled}>
  {label}: {count} (×2 = {doubled})
</button>
```

`$state` makes a value reactive; `$derived` computes from it; assignment (`count += 1`) triggers updates — no `set` calls, no immutability ceremony.

### Effects for external synchronization only

```svelte
<script lang="ts">
  let width = $state(0);
  $effect(() => {
    const onResize = () => (width = window.innerWidth);
    onResize();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize); // cleanup
  });
</script>
```

Do not use `$effect` to compute one piece of state from another — use `$derived`.

### Reusable reactive logic in `.svelte.ts`

```ts
// counter.svelte.ts
export function createCounter(initial = 0) {
  let count = $state(initial);
  return {
    get count() { return count; },
    increment: () => (count += 1),
    reset: () => (count = initial),
  };
}
```

```svelte
<script lang="ts">
  import { createCounter } from './counter.svelte.ts';
  const counter = createCounter();
</script>
<button onclick={counter.increment}>{counter.count}</button>
```

### Two-way binding with `$bindable`

```svelte
<!-- Input.svelte -->
<script lang="ts">
  let { value = $bindable('') }: { value?: string } = $props();
</script>
<input bind:value />

<!-- parent -->
<Input bind:value={name} />
```

### SvelteKit: routing, load, and form actions

```ts
// src/routes/posts/+page.server.ts — runs on the server
import type { PageServerLoad, Actions } from './$types';

export const load: PageServerLoad = async ({ fetch }) => {
  const posts = await fetch('/api/posts').then((r) => r.json());
  return { posts };
};

export const actions: Actions = {
  create: async ({ request }) => {
    const data = await request.formData();
    const title = String(data.get('title') ?? '').trim();
    if (!title) return { error: 'Title required' };
    await db.post.create({ title });
    return { success: true };
  },
};
```

```svelte
<!-- src/routes/posts/+page.svelte -->
<script lang="ts">
  let { data, form } = $props(); // data from load, form from action
</script>

{#each data.posts as post (post.id)}
  <li>{post.title}</li>
{/each}

<form method="POST" action="?/create">
  <label for="title">Title</label>
  <input id="title" name="title" />
  <button>Create</button>
  {#if form?.error}<p role="alert">{form.error}</p>{/if}
</form>
```

Form actions work without JS; add `use:enhance` for progressive enhancement.

### Accessibility basics

- Use `<label for>` / `id`; Svelte's compiler warns on a11y violations — fix them, don't suppress.
- Bind handlers to real `<button>`/`<a>`; provide `alt` on images.
- Use `{#each list as item (item.id)}` with a keyed block for stable, correct updates.

### Testing (Vitest + Testing Library + Playwright)

```ts
import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { expect, test } from 'vitest';
import Counter from './Counter.svelte';

test('increments', async () => {
  render(Counter, { props: { label: 'Count' } });
  await userEvent.click(screen.getByRole('button'));
  expect(screen.getByText(/Count: 1/)).toBeInTheDocument();
});
```

Use Playwright for SvelteKit routes and form actions end-to-end.

## Anti-patterns

- Do not use legacy `export let` props or `$:` reactive statements in Svelte 5 — use `$props` and `$derived`.
- Do not use `$effect` to derive state — `$derived` is correct and glitch-free.
- Do not reach for a store when a `$state` rune or a `.svelte.ts` module covers it.
- Do not suppress Svelte's a11y compiler warnings — resolve them.
- Do not omit the `(item.id)` key in `{#each}` for lists that reorder or filter.
- Do not register listeners in `$effect` without returning a cleanup function.
- Do not mutate `$props()` values directly — use `$bindable` for two-way flow.
