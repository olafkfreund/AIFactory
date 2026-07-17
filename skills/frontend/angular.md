# angular

> Source: curated best practices | 2026

---

# Angular - Standalone components and signals (Angular 18+)

This skill equips the coder to build Angular 18+ applications with standalone components (no NgModules), the signals reactivity model (`signal`, `computed`, `effect`, `input`/`output` signal APIs, `model`), the new built-in control flow (`@if`/`@for`/`@switch`), typed reactive forms, `HttpClient` with `provideHttpClient`, and lazy-loaded routes. It enforces `OnPush`/signal-driven change detection, dependency injection via `inject()`, RxJS only where streams add value, accessibility, and Jasmine/Karma or Vitest + Playwright testing. NgModules, `*ngIf`/`*ngFor` structural directives in new code, and constructor-based DI boilerplate are avoided.

## When to Activate

Use when building UI with Angular:
- Repo has `angular.json` and `@angular/core` in `package.json` (>= 17, target 18+)
- Task mentions standalone components, signals, `@if`/`@for`, reactive forms, `HttpClient`, RxJS
- Building Angular SPAs, component libraries, or enterprise dashboards

## Patterns and Best Practices

### Standalone component with signals

```ts
import { Component, ChangeDetectionStrategy, signal, computed, input, output } from '@angular/core';

@Component({
  selector: 'app-counter',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <label [for]="'step'">Step</label>
    <input id="step" type="number" [value]="step()" (input)="onStep($event)" />
    <p>Count: {{ count() }} — doubled: {{ doubled() }}</p>
    <button type="button" (click)="increment()">Add</button>
  `,
})
export class CounterComponent {
  readonly start = input(0);            // signal input
  readonly changed = output<number>();  // signal output
  protected step = signal(1);
  protected count = signal(0);
  protected doubled = computed(() => this.count() * 2);

  increment() {
    this.count.update((c) => c + this.step());
    this.changed.emit(this.count());
  }
  onStep(e: Event) {
    this.step.set(Number((e.target as HTMLInputElement).value));
  }
}
```

### Built-in control flow (replaces *ngIf / *ngFor)

```html
@if (user(); as u) {
  <p>Welcome {{ u.name }}</p>
} @else {
  <p>Please sign in</p>
}

@for (item of items(); track item.id) {
  <li>{{ item.title }}</li>
} @empty {
  <li>No items</li>
}
```

`track` is required in `@for` — it is the stable-key equivalent.

### Dependency injection with inject()

```ts
import { inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';

@Injectable({ providedIn: 'root' })
export class UserService {
  private http = inject(HttpClient); // no constructor boilerplate
  getUser(id: string) {
    return this.http.get<User>(`/api/users/${id}`);
  }
}
```

### Data fetching: HttpClient + signals

```ts
import { toSignal } from '@angular/core/rxjs-interop';

export class UsersComponent {
  private service = inject(UserService);
  // stream → signal; template reads users()
  readonly users = toSignal(this.service.list(), { initialValue: [] as User[] });
}
```

Provide `HttpClient` at bootstrap:

```ts
bootstrapApplication(AppComponent, {
  providers: [provideHttpClient(), provideRouter(routes)],
});
```

### Typed reactive forms

```ts
import { FormBuilder, Validators, ReactiveFormsModule } from '@angular/forms';

@Component({ standalone: true, imports: [ReactiveFormsModule], /* ... */ })
export class SignupComponent {
  private fb = inject(FormBuilder);
  form = this.fb.nonNullable.group({
    email: ['', [Validators.required, Validators.email]],
    age: [0, [Validators.min(18)]],
  });
  submit() {
    if (this.form.invalid) return;
    console.log(this.form.getRawValue()); // fully typed
  }
}
```

```html
<form [formGroup]="form" (ngSubmit)="submit()">
  <label for="email">Email</label>
  <input id="email" formControlName="email" />
  @if (form.controls.email.invalid && form.controls.email.touched) {
    <p role="alert">Valid email required</p>
  }
  <button [disabled]="form.invalid">Sign up</button>
</form>
```

### Lazy-loaded routes

```ts
export const routes: Routes = [
  { path: '', loadComponent: () => import('./home.component').then((m) => m.HomeComponent) },
  { path: 'users', loadChildren: () => import('./users/routes').then((m) => m.USER_ROUTES) },
];
```

### Accessibility basics

- Bind `<label for>` to control `id`; use `role="alert"` for validation messages.
- Use real interactive elements; add `aria-live` for async status.
- Keep `OnPush` change detection so the UI updates predictably from signals.

### Testing (Jasmine/Karma or Vitest + Playwright)

```ts
import { TestBed } from '@angular/core/testing';
import { CounterComponent } from './counter.component';

it('increments', () => {
  const fixture = TestBed.createComponent(CounterComponent);
  fixture.detectChanges();
  fixture.nativeElement.querySelector('button').click();
  fixture.detectChanges();
  expect(fixture.nativeElement.textContent).toContain('Count: 1');
});
```

Use Playwright for full end-to-end flows and routing.

## Anti-patterns

- Do not create NgModules for new features — use standalone components, directives, and pipes.
- Do not use `*ngIf`/`*ngFor` in new templates — use `@if`/`@for` with `track`.
- Do not manually `subscribe()` and store values — use `toSignal` or the `async` pipe (and unsubscribe if you must subscribe).
- Do not use `any`-typed `FormGroup`s — use `nonNullable` typed reactive forms.
- Do not default to `ChangeDetectionStrategy.Default` — use `OnPush` with signals.
- Do not inject via constructor boilerplate when `inject()` is cleaner and works in more contexts.
- Do not mutate signal values in place — use `.set()`/`.update()` with new references.
