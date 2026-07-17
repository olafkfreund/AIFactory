# ruby

> Source: curated best practices | 2026

---

# Ruby - Expressive, convention-driven application code

This skill equips the coder to write idiomatic Ruby 3.3+ that reads like well-formed prose: small methods, blocks and enumerables over manual loops, duck typing, and clear objects with single responsibilities. It assumes Bundler for dependencies, RuboCop for lint+format, RSpec (or Minitest) for tests, and keyword arguments for clarity. It favors the standard library and expressive Enumerable methods.

## When to Activate

Use when the task involves Ruby:
- Writing or modifying `.rb` files, gems, or Rails/Sinatra apps
- Building web backends, CLIs, scripts, or libraries in Ruby
- Anything referencing `Gemfile`, `bundle`, `rspec`, `rubocop`, `rake`

## Idioms and Best Practices

**Project layout** (gem/bundler convention):
```
myapp/
  Gemfile
  lib/myapp.rb
  lib/myapp/parser.rb
  spec/parser_spec.rb
  Rakefile
```

**Enumerable is the workhorse** - prefer it to index loops:
```ruby
names   = users.select(&:active?).map(&:name)
total   = lines.sum(&:amount)
by_role = users.group_by(&:role)
```
Use `each_with_object`, `reduce`, `filter_map`, `tally`, `partition` where they express intent directly. `filter_map` replaces `map { }.compact`.

**Small methods, guard clauses** over nested conditionals:
```ruby
def charge(order)
  return :skipped unless order.payable?
  raise ArgumentError, "no total" if order.total.nil?

  gateway.charge(order.total)
end
```

**Keyword arguments** for anything with more than one or two parameters - self-documenting at call sites:
```ruby
def create_user(name:, email:, role: :member)
  ...
end
create_user(name: "Ada", email: "a@x.io")
```

**Objects with a single responsibility.** Prefer plain Ruby objects (POROs) and small classes; use modules for shared behavior (`include`) and namespacing. Keep state minimal and use `attr_reader` (not `attr_accessor`) unless mutation is intended.

**Symbols for identifiers/keys**, strings for data. Freeze string constants (`FOO = "bar".freeze`) or rely on `# frozen_string_literal: true` at the top of every file.

**Error handling:** rescue specific classes, never a bare `rescue` (which catches `StandardError` broadly - and `rescue Exception` is worse, catching signals):
```ruby
begin
  parse(input)
rescue JSON::ParserError => e
  raise ConfigError, "bad config: #{e.message}"
end
```
Define a small custom error hierarchy per gem: `class ConfigError < StandardError; end`. Use `ensure` for cleanup.

**Blocks for resource management:**
```ruby
File.open(path, "r:UTF-8") do |f|
  f.each_line { |line| process(line) }
end   # file closed automatically
```

**Nil safety:** use the safe-navigation operator `&.` and `fetch` with defaults; prefer `nil?`/`present?` over truthiness confusion. `Hash#fetch(:key)` raises on missing keys - use it when absence is a bug.

**Testing (RSpec):**
```ruby
RSpec.describe Parser do
  describe "#parse" do
    it "parses a valid number" do
      expect(described_class.new.parse("42")).to eq(42)
    end

    it "raises on junk" do
      expect { described_class.new.parse("x") }.to raise_error(ConfigError)
    end
  end
end
```
Keep examples focused, use `let` for lazy setup, `subject` for the object under test, and factories/fixtures sparingly. Prefer real objects to mocks unless isolating a boundary.

**Rails specifics** (when applicable): fat models / skinny controllers is dated - extract service objects and query objects; use scopes; avoid N+1 with `includes`; keep callbacks minimal; use strong parameters.

**Tooling:** RuboCop for lint and formatting (`rubocop -A` to autocorrect), Bundler for deps, Rake for tasks. Follow the community style guide RuboCop encodes.

## Anti-patterns

- `rescue` with no class (swallows all `StandardError`) or `rescue Exception`.
- `for x in ...` loops instead of `each`/Enumerable methods.
- `attr_accessor` everywhere, exposing mutable state needlessly.
- Monkey-patching core classes in application code.
- Long methods with deep nesting instead of guard clauses and extraction.
- Using `String` keys and `Symbol` keys interchangeably in the same hash.
- `map { }.compact` instead of `filter_map`; manual counting instead of `tally`.
- Metaprogramming (`method_missing`, `define_method`) when a plain method is clearer.
