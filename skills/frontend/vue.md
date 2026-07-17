# vue

> Source: curated best practices | 2026

---

# Vue - Vue 3 Composition API with `<script setup>`

This skill equips the coder to build Vue 3 single-file components using `<script setup>` and the Composition API, with typed props/emits via `defineProps`/`defineEmits`, reactive state through `ref`/`reactive`/`computed`, composables for reusable logic, and Pinia for app-level state. It enforces the reactivity rules (`.value` in script, auto-unwrap in template), proper cleanup with `onUnmounted`, accessible template markup, and Vitest + Vue Test Utils tests. Options API, mutating props, and losing reactivity by destructuring reactive objects are avoided.

## When to Activate

Use when building UI with Vue:
- Files are `.vue` SFCs or import from `vue` / `pinia` / `vue-router`
- Task mentions Composition API, `ref`, `reactive`, `computed`, composables, Pinia
- `package.json` lists `vue` >= 3
- Building Vue SPAs, component libraries, or Nuxt components (framework routing aside)

## Patterns and Best Practices

### Component structure with `<script setup>` and TypeScript

```vue
<script setup lang="ts">
import { ref, computed } from 'vue';

const props = defineProps<{ label: string; disabled?: boolean }>();
const emit = defineEmits<{ submit: [value: string] }>();

const text = ref('');
const canSubmit = computed(() => text.value.trim().length > 0 && !props.disabled);

function onSubmit() {
  if (canSubmit.value) emit('submit', text.value);
}
</script>

<template>
  <form @submit.prevent="onSubmit">
    <label :for="'field'">{{ label }}</label>
    <input id="field" v-model="text" />
    <button :disabled="!canSubmit">Save</button>
  </form>
</template>
```

### Reactivity fundamentals

```ts
import { ref, reactive, computed, watch } from 'vue';

const count = ref(0);          // primitive → ref, access via count.value in script
count.value++;                  // template auto-unwraps: {{ count }}

const state = reactive({ items: [] as Item[] }); // object → reactive
state.items.push(newItem);

const total = computed(() => state.items.reduce((s, i) => s + i.price, 0));

watch(count, (next, prev) => console.log(next, prev)); // side effects only
```

Destructuring a `reactive` object loses reactivity — use `toRefs` when you must destructure:

```ts
import { toRefs } from 'vue';
const { items } = toRefs(state); // items stays reactive
```

### Composables (reusable logic)

```ts
// useCounter.ts
import { ref } from 'vue';
export function useCounter(initial = 0) {
  const count = ref(initial);
  const increment = () => count.value++;
  return { count, increment };
}
```

```ts
// composable with lifecycle + cleanup
import { ref, onMounted, onUnmounted } from 'vue';
export function useMouse() {
  const x = ref(0), y = ref(0);
  const update = (e: MouseEvent) => { x.value = e.pageX; y.value = e.pageY; };
  onMounted(() => window.addEventListener('mousemove', update));
  onUnmounted(() => window.removeEventListener('mousemove', update));
  return { x, y };
}
```

### App state with Pinia

```ts
import { defineStore } from 'pinia';
import { ref, computed } from 'vue';

export const useCartStore = defineStore('cart', () => {
  const items = ref<Item[]>([]);
  const total = computed(() => items.value.reduce((s, i) => s + i.price, 0));
  function add(item: Item) { items.value.push(item); }
  return { items, total, add };
});
```

Use the setup-store form above; call `useCartStore()` inside components.

### Data fetching

```ts
const data = ref<User | null>(null);
const error = ref<Error | null>(null);
const loading = ref(true);

async function load() {
  try { data.value = await fetchUser(); }
  catch (e) { error.value = e as Error; }
  finally { loading.value = false; }
}
onMounted(load);
```

For caching/refetching, use `@tanstack/vue-query`. With `<Suspense>`, a component's top-level `await` in `setup` integrates with a fallback.

### Accessibility basics

- Bind labels to inputs with `:for` / `id`; use `v-model` on native controls.
- Prefer `@click` on real `<button>`/`<a>`, not `<div>`.
- Use `aria-live` regions for async status; keep keyboard focus visible.
- `v-for` requires a stable `:key`.

### Testing (Vitest + Vue Test Utils)

```ts
import { mount } from '@vue/test-utils';
import { expect, test } from 'vitest';
import Field from './Field.vue';

test('emits submit with typed value', async () => {
  const wrapper = mount(Field, { props: { label: 'Name' } });
  await wrapper.find('input').setValue('Ada');
  await wrapper.find('form').trigger('submit');
  expect(wrapper.emitted('submit')?.[0]).toEqual(['Ada']);
});
```

Use Playwright for full end-to-end flows.

## Anti-patterns

- Do not mutate props — emit an event and let the parent update state.
- Do not destructure a `reactive` object directly — you lose reactivity; use `toRefs`.
- Do not mix Options API and Composition API in new code — standardize on `<script setup>`.
- Do not forget `.value` in script code (templates auto-unwrap, script does not).
- Do not run side effects in `computed` — computeds must be pure; use `watch`/`watchEffect`.
- Do not omit `:key` on `v-for`, and never use the index for reorderable lists.
- Do not register global event listeners without removing them in `onUnmounted`.
