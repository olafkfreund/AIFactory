# php

> Source: curated best practices | 2026

---

# PHP - Typed, PSR-compliant modern application code

This skill equips the coder to write modern PHP 8.3+ with strict types, typed properties, constructor promotion, enums, and readonly value objects. It assumes Composer with PSR-4 autoloading, PSR-12 coding style, PHPStan/Psalm at a high level, and PHPUnit (or Pest) for tests. It favors dependency injection, immutability, and the standard library over reinventing helpers.

## When to Activate

Use when the task involves PHP:
- Writing or modifying `.php` files, Composer packages
- Building Laravel / Symfony apps, APIs, or libraries in PHP
- Anything referencing `composer.json`, PSR-4, PHPUnit, `phpstan`, enums

## Idioms and Best Practices

**Always declare strict types** as the first statement of every file:
```php
<?php

declare(strict_types=1);

namespace Acme\App;
```
This turns silent type coercion into `TypeError` - catch bugs early.

**Project layout** (Composer PSR-4):
```
myapp/
  composer.json          // "autoload": {"psr-4": {"Acme\\App\\": "src/"}}
  src/UserService.php
  tests/UserServiceTest.php
```

**Constructor property promotion + readonly** for value objects:
```php
final class Money
{
    public function __construct(
        public readonly int $amountCents,
        public readonly string $currency = 'USD',
    ) {}
}
```
`readonly` enforces immutability; promotion removes boilerplate.

**Type everything** - parameters, return types, and properties:
```php
public function totals(array $rows): array
{
    return array_reduce($rows, fn (array $acc, Row $r) => /* ... */ $acc, []);
}
```
Use union types (`int|string`), nullable (`?User`), and `never`/`void` returns. Document array shapes for static analyzers with `@param array<string, int>`.

**Enums** for fixed sets, backed when you need a scalar value:
```php
enum Status: string
{
    case Active = 'active';
    case Closed = 'closed';

    public function isTerminal(): bool => $this === self::Closed;
}
```

**Match over switch** (strict comparison, returns a value, no fallthrough):
```php
$label = match ($status) {
    Status::Active => 'open',
    Status::Closed => 'done',
};
```

**Dependency injection by constructor**, program to interfaces:
```php
final class UserService
{
    public function __construct(private readonly UserRepository $repo) {}
}
```
Let the framework container wire dependencies; avoid `new` for collaborators and global state / singletons.

**Error handling:** throw specific `Exception` subclasses; define a package hierarchy (`class ConfigException extends \RuntimeException {}`). Catch narrowly, chain the previous exception:
```php
try {
    $raw = file_get_contents($path);
} catch (\Throwable $e) {
    throw new ConfigException("read failed: {$path}", previous: $e);
}
```
Use `finally` for cleanup. Prefer exceptions to boolean/false returns for real failures.

**Null safety:** nullsafe operator `?->`, null coalescing `??` and `??=`:
```php
$name = $user?->profile?->name ?? 'unknown';
```

**Standard library first:** `array_map`/`array_filter`/`array_reduce`, `str_contains`/`str_starts_with`, `json_encode`/`json_decode(..., flags: JSON_THROW_ON_ERROR)`, `DateTimeImmutable` (never mutable `DateTime` for values), `SplStack`/`SplQueue`.

**Testing (PHPUnit):**
```php
final class UserServiceTest extends \PHPUnit\Framework\TestCase
{
    public function testFindsExistingUser(): void
    {
        $svc = new UserService(new InMemoryRepo());
        self::assertNotNull($svc->find('1'));
    }

    public function testRejectsBlankId(): void
    {
        $this->expectException(\InvalidArgumentException::class);
        (new UserService(new InMemoryRepo()))->find('');
    }
}
```
Use data providers for table cases, mocks for boundaries. Pest is a fine alternative with a terser syntax.

**Tooling:** Composer for deps, PHP-CS-Fixer or `phpcbf` for PSR-12 formatting, PHPStan/Psalm at level 8/max in CI, `composer test`.

## Anti-patterns

- Omitting `declare(strict_types=1)` and relying on coercion.
- Untyped properties/params/returns; using `mixed` as a default escape hatch.
- Suppressing errors with `@`; catching `\Throwable` and swallowing it.
- Mutable `DateTime` for value semantics - use `DateTimeImmutable`.
- Global state, `static` mutable data, and `new` for injected collaborators.
- String comparisons with `==` where `===` is meant (loose comparison bugs).
- Building SQL by string concatenation - use prepared statements / query builders.
- Reinventing array/string helpers that the stdlib already provides.
