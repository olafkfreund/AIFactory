# react

> Source: curated best practices | 2026

---

# React - Modern component architecture with React 19

This skill equips the coder to build React 19 applications using function components and hooks exclusively, with the modern data-fetching primitives (`use`, Actions, `useActionState`, `useOptimistic`), automatic batching, and the compiler-friendly patterns that let React Compiler memoize for you. It enforces colocation of state, derived-not-stored values, stable keys, accessible markup, and Vitest + React Testing Library tests that assert behavior through the DOM rather than implementation details. Class components, legacy lifecycle methods, and manual `useMemo`/`useCallback` micro-optimization are avoided in favor of clear, declarative code.

## When to Activate

Use when building UI with React:
- Files import from `react` / `react-dom`, or use `.jsx`/`.tsx` with JSX
- Task mentions components, hooks, `useState`/`useEffect`, context, Suspense
- `package.json` lists `react` >= 18 (target 19 patterns)
- Building SPAs, design-system components, or interactive widgets (not Next.js-specific routing — see nextjs.md)

## Patterns and Best Practices

### Component structure and typed props

```tsx
type ButtonProps = {
  variant?: 'primary' | 'ghost';
  onClick?: () => void;
  children: React.ReactNode;
};

export function Button({ variant = 'primary', onClick, children }: ButtonProps) {
  return (
    <button type="button" className={`btn btn-${variant}`} onClick={onClick}>
      {children}
    </button>
  );
}
```

Keep components small and single-purpose. Derive values during render instead of syncing them into state with an effect:

```tsx
// Good: derived, no effect, always correct
const fullName = `${firstName} ${lastName}`;
const visibleItems = items.filter((i) => i.active);
```

### State: useState, reducer, and context

```tsx
// Local state
const [count, setCount] = useState(0);
setCount((c) => c + 1); // functional update — safe under batching

// Complex transitions → reducer
type Action = { type: 'add'; text: string } | { type: 'remove'; id: string };
function reducer(state: Todo[], action: Action): Todo[] {
  switch (action.type) {
    case 'add': return [...state, { id: crypto.randomUUID(), text: action.text }];
    case 'remove': return state.filter((t) => t.id !== action.id);
  }
}
const [todos, dispatch] = useReducer(reducer, []);
```

Context for cross-cutting state only (theme, auth). Split value and dispatch contexts to limit re-renders, or reach for Zustand/Jotai when prop-drilling gets deep.

### Data fetching with `use` and Suspense (React 19)

```tsx
import { use, Suspense } from 'react';

function UserProfile({ userPromise }: { userPromise: Promise<User> }) {
  const user = use(userPromise); // suspends until resolved
  return <h1>{user.name}</h1>;
}

function Page() {
  const userPromise = fetchUser(); // start early, pass the promise down
  return (
    <Suspense fallback={<Spinner />}>
      <UserProfile userPromise={userPromise} />
    </Suspense>
  );
}
```

For anything beyond trivial fetches, use TanStack Query for caching, retries, and invalidation.

### Forms with Actions and useActionState

```tsx
import { useActionState } from 'react';

async function saveName(prev: string, formData: FormData): Promise<string> {
  const name = formData.get('name') as string;
  if (!name.trim()) return 'Name is required';
  await api.save(name);
  return 'Saved';
}

function NameForm() {
  const [message, formAction, isPending] = useActionState(saveName, '');
  return (
    <form action={formAction}>
      <label htmlFor="name">Name</label>
      <input id="name" name="name" />
      <button disabled={isPending}>{isPending ? 'Saving…' : 'Save'}</button>
      {message && <p role="status">{message}</p>}
    </form>
  );
}
```

`useOptimistic` gives instant UI feedback while the action is in flight.

### Effects — only for external synchronization

```tsx
useEffect(() => {
  const ctrl = new AbortController();
  window.addEventListener('resize', onResize, { signal: ctrl.signal });
  return () => ctrl.abort(); // always clean up
}, []);
```

If an effect only computes state from props, delete it and compute during render instead.

### Accessibility basics

- Label every input (`htmlFor`/`id` or `aria-label`).
- Use semantic elements: `<button>` for actions, `<a>` for navigation.
- Announce async results with `role="status"` / `aria-live`.
- Keep visible focus outlines; manage focus after route or dialog changes.

### Testing (Vitest + React Testing Library)

```tsx
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, test } from 'vitest';

test('increments on click', async () => {
  render(<Counter />);
  await userEvent.click(screen.getByRole('button', { name: /add/i }));
  expect(screen.getByText('1')).toBeInTheDocument();
});
```

Query by role/label/text (what users perceive), never by test-id unless nothing else works. Use Playwright for cross-page end-to-end flows.

## Anti-patterns

- Do not store derived data in state and sync it with `useEffect` — compute it during render.
- Do not use array index as `key` for reorderable lists — use a stable id.
- Do not sprinkle `useMemo`/`useCallback` everywhere "for performance"; let React Compiler handle it and reach for them only after measuring.
- Do not write new class components or use `componentWillMount`-era lifecycles.
- Do not mutate state objects/arrays in place — return new references.
- Do not fetch in an effect when you can pass a promise to `use` or use TanStack Query.
- Do not put non-serializable, frequently-changing values in a single context that wraps the whole app.
