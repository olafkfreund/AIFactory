# security

> Source: curated best practices | 2026

---

# Security - assume every input is hostile and every secret will leak

Security is not a feature you bolt on; it is a set of habits applied at every trust boundary. Most breaches are not exotic — they are an unvalidated input concatenated into a query, a secret committed to git, a missing authorization check, or a dependency with a known CVE nobody patched. The defensive posture is simple to state: validate what crosses a trust boundary, never trust the client, parameterize everything that hits an interpreter, keep secrets out of code and logs, and check who is allowed to do what on every request — not just whether they logged in.

## When to Activate

Use when code touches untrusted input, secrets, auth, or external systems:
- handling user input, request bodies, query params, file uploads, headers
- building SQL, shell commands, HTML, file paths, or LLM prompts from variables
- anything involving passwords, tokens, API keys, or PII
- adding or updating a dependency
- writing an endpoint that reads or mutates data a user should not universally access

## Principles and Practices

**Know the top risks (OWASP-flavored).** Injection (SQL, command, LDAP, prompt), broken access control (the #1 real-world breach — users acting on resources they do not own), identification/auth failures, security misconfiguration, vulnerable dependencies, cryptographic failures (plaintext secrets, weak hashing), and SSRF (server fetching attacker-controlled URLs). Most of these are prevented by the practices below.

**Validate at trust boundaries.** A trust boundary is anywhere data crosses from less-trusted to more-trusted: HTTP handler, message consumer, file import, CLI arg. Validate there — type, length, range, format, allowlist — and reject early. Validation deeper in the code is a bonus, not a substitute. Prefer allowlists ("must be one of these") over blocklists ("must not contain these"); blocklists always miss a case.

**Parameterize queries — never concatenate.** This kills SQL injection dead.

```python
# WRONG — attacker sends name = "'; DROP TABLE users; --"
db.execute(f"SELECT * FROM users WHERE name = '{name}'")
# RIGHT — driver escapes it, data can never become code
db.execute("SELECT * FROM users WHERE name = %s", (name,))
```

Same rule for shell (`subprocess.run([cmd, arg])`, never `shell=True` with interpolation), for HTML (use the template engine's auto-escaping, never string-build markup), and for file paths (resolve and confirm the result stays inside the allowed base dir — block `../` traversal).

**Secrets never in code, argv, or logs.** No API keys, passwords, or tokens in source, config committed to git, or command-line arguments (argv is visible to every process via `ps` and often lands in shell history and CI logs). Pass secrets via environment variables or a secrets manager, read them from files with tight permissions, and scrub them from log output. Rotate on exposure. Add a pre-commit secret scanner so a key never reaches history in the first place.

```
# WRONG: curl -H "Authorization: Bearer sk_live_abc123"   # visible in ps/history
# RIGHT: pass via env; the process reads os.environ["API_TOKEN"]
```

**Authn vs authz — check both, every time.** Authentication answers "who are you"; authorization answers "are you allowed to do this to this resource". A logged-in user is authenticated; that does not mean they may read `/orders/12345` belonging to someone else. Check ownership/role on every request against the specific object, server-side. Broken access control is the most common serious bug because the happy-path code works — the missing check only shows when someone changes the ID in the URL.

**Hash passwords, encrypt secrets in transit and at rest.** Use a slow, salted password hash (bcrypt, scrypt, argon2) — never MD5/SHA-1, never plaintext. TLS for everything over the wire. Do not invent crypto; use the platform's vetted library with sane defaults.

**Dependency hygiene.** Every dependency is code you did not write running with your privileges. Pin versions, run an automated vulnerability scanner (dependabot, `npm audit`, `pip-audit`, `govulncheck`) in CI, and patch known CVEs promptly. Fewer dependencies = smaller attack surface; prefer stdlib over a transitive tree of 200 packages for a one-liner.

**Fail closed.** When an auth check, feature flag, or validation errors out, deny — do not fall through to "allow". Default to the safe state.

**Do not leak information in errors.** Return generic messages to clients ("invalid credentials", not "no such user" vs "wrong password" — the difference lets attackers enumerate accounts). Log the detail server-side; show the user nothing exploitable. Never return stack traces or SQL errors to the client.

**Rate-limit and lock out.** Login, password reset, and expensive endpoints need rate limiting to blunt brute-force and abuse.

## Anti-patterns

- Building SQL/shell/HTML/paths by string concatenation of user input.
- Trusting client-side validation — it is a UX nicety; re-validate on the server.
- Secrets in source, in argv, in commit history, or printed to logs.
- Checking that a user is logged in but not that they own the resource they are touching.
- Storing passwords with a fast hash or in plaintext; rolling your own crypto.
- Blocklist filters ("strip `<script>`") instead of allowlists / proper escaping.
- Ignoring dependency CVE alerts, or adding a heavy dependency for trivial work.
- Verbose error messages that reveal stack traces, SQL, or whether an account exists.
- Fetching a URL supplied by the user without restricting host/scheme (SSRF).
