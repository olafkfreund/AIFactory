# nextjs

> Source: curated best practices | 2026

---

# Next.js - App Router with Server Components (Next 15)

This skill equips the coder to build Next.js 15 applications on the App Router, defaulting to React Server Components for data fetching and pushing `'use client'` to the leaves, using Server Actions for mutations, the `app/` file conventions (`layout`, `page`, `loading`, `error`, `route`), streaming with Suspense, and the caching model (`fetch` cache, `revalidatePath`/`revalidateTag`, `dynamic`/`revalidate` route segment config). It enforces metadata for SEO, correct data colocation, and Playwright end-to-end plus Vitest unit tests. Fetching in client components when a server component would do, and leaking secrets to the client, are avoided.

## When to Activate

Use when building UI with Next.js:
- Repo has `next.config.*` and an `app/` directory
- Task mentions App Router, Server Components, Server Actions, `page.tsx`, `layout.tsx`, route handlers
- `package.json` lists `next` >= 14 (target 15 patterns)
- Building full-stack React apps with routing, SSR/SSG/ISR, or edge functions

## Patterns and Best Practices

### Server Component data fetching (default)

```tsx
// app/posts/page.tsx — a Server Component, runs on the server, can be async
export default async function PostsPage() {
  const posts = await fetch('https://api.example.com/posts', {
    next: { revalidate: 60 }, // ISR: cache 60s
  }).then((r) => r.json());

  return (
    <ul>
      {posts.map((p: Post) => (
        <li key={p.id}>{p.title}</li>
      ))}
    </ul>
  );
}
```

Server Components can query the DB directly and read secrets — none of that ships to the browser. Add `'use client'` only where you need state, effects, or event handlers.

### Client component at the leaf

```tsx
'use client';
import { useState } from 'react';

export function LikeButton({ initial }: { initial: number }) {
  const [likes, setLikes] = useState(initial);
  return <button onClick={() => setLikes((n) => n + 1)}>Likes: {likes}</button>;
}
```

### File conventions

```
app/
  layout.tsx      // shared shell, <html>/<body>, providers
  page.tsx        // route UI
  loading.tsx     // Suspense fallback for the segment
  error.tsx       // error boundary ('use client')
  not-found.tsx   // 404 UI
  posts/[id]/page.tsx   // dynamic route → params.id
  api/health/route.ts   // route handler (GET/POST)
```

### Server Actions for mutations

```tsx
// app/actions.ts
'use server';
import { revalidatePath } from 'next/cache';

export async function createPost(formData: FormData) {
  const title = String(formData.get('title') ?? '').trim();
  if (!title) return { error: 'Title required' };
  await db.post.create({ data: { title } });
  revalidatePath('/posts'); // refresh cached data
}
```

```tsx
// component — progressive enhancement, works without JS
import { createPost } from './actions';
export function NewPost() {
  return (
    <form action={createPost}>
      <label htmlFor="title">Title</label>
      <input id="title" name="title" />
      <button>Create</button>
    </form>
  );
}
```

Use `useActionState` (client) to surface returned errors and pending state.

### Streaming with Suspense

```tsx
import { Suspense } from 'react';
export default function Page() {
  return (
    <>
      <Header />
      <Suspense fallback={<SkeletonList />}>
        <SlowComments /> {/* async server component streams in when ready */}
      </Suspense>
    </>
  );
}
```

### Caching and rendering control

```tsx
export const dynamic = 'force-dynamic'; // opt out of caching for this segment
export const revalidate = 3600;          // or ISR window in seconds
// Per-fetch: { cache: 'no-store' } or { next: { tags: ['posts'] } }
// Invalidate a tag from an action: revalidateTag('posts')
```

### Metadata (SEO)

```tsx
import type { Metadata } from 'next';
export const metadata: Metadata = {
  title: 'Posts',
  description: 'Latest posts',
};
// Dynamic: export async function generateMetadata({ params }) { ... }
```

### Accessibility and images

- Use `next/link` for client navigation and `next/image` for optimized, sized images (prevents layout shift, requires `alt`).
- Provide `<label>`s and semantic landmarks in layouts.

### Testing

```ts
// Vitest for pure logic + client components (RTL)
// Playwright for routes, SSR output, and server actions end-to-end:
import { test, expect } from '@playwright/test';
test('creates a post', async ({ page }) => {
  await page.goto('/posts');
  await page.getByLabel('Title').fill('Hello');
  await page.getByRole('button', { name: 'Create' }).click();
  await expect(page.getByText('Hello')).toBeVisible();
});
```

## Anti-patterns

- Do not add `'use client'` to a whole page just to use one interactive widget — isolate the client leaf.
- Do not fetch data in a client component (`useEffect`) when a Server Component can fetch it directly.
- Do not import server-only modules or read secrets in client components — they ship to the browser.
- Do not mutate data in route handlers you could express as a Server Action with `revalidatePath`.
- Do not forget to revalidate cache after a mutation — stale UI results.
- Do not use `<img>`/`<a>` where `next/image`/`next/link` give optimization and prefetch.
- Do not block the whole page on one slow query — wrap it in `<Suspense>` and stream.
