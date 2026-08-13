/**
 * @name Uncontrolled command line (sanitizer-aware)
 * @description Using externally controlled strings in a command line allows a malicious user to change the meaning of the command.
 * @kind path-problem
 * @problem.severity error
 * @security-severity 9.8
 * @sub-severity high
 * @precision high
 * @id py/command-line-injection-sanitized
 * @tags correctness
 *       security
 *       external/cwe/cwe-078
 *       external/cwe/cwe-088
 */

import python
import semmle.python.dataflow.new.DataFlow
import semmle.python.ApiGraphs
import semmle.python.security.dataflow.CommandInjectionQuery
import semmle.python.security.dataflow.CommandInjectionCustomizations
import CommandInjectionFlow::PathGraph

/**
 * The same swap as `PathInjectionSanitized.ql` next door, for the same reason,
 * stated in `codeql-config.yml`: stock CodeQL recognises a validator defined
 * AND called in the same module, but does not follow one imported from
 * `services/`. The validators below live in `services/argv_safety.py` and
 * every caller imports them, so the command-injection alerts could not clear
 * however completely the argv was asserted.
 *
 * "Add a barrier" is also what someone would do to silence a real finding, so
 * each name is here for a stated reason, and the reasons are the whole
 * justification. Every subprocess call on these paths uses the list form and
 * never `shell=True`, which is what makes these properties sufficient: with no
 * shell, the only way a value changes the meaning of a command is by being
 * read as an option.
 *
 * - `assert_safe_git_ref` is an allowlist: the value must `fullmatch`
 *   `[A-Za-z0-9][A-Za-z0-9_.@/+^~{}-]{0,254}`, so it cannot begin with `-` and
 *   cannot be parsed by git as an option. That is load bearing rather than
 *   tidy: `git log --output=<file>` makes an unvalidated ref an arbitrary file
 *   write, which is what routes/changelog.py was exposed to. It also rejects
 *   an embedded `..`, because callers join refs into `a..b` ranges and an
 *   embedded separator would let one field rewrite the range it lands in.
 * - `assert_not_option` rejects a leading `-` (and NUL) and returns the value.
 *   It is registered only because it is used on values that are already
 *   positional operands of a list-form argv -- a ripgrep pattern after `--`, a
 *   git pathspec after `--`, a resolved absolute directory. It does NOT make a
 *   value safe to interpolate into a shell string or to use as the program
 *   name, and its docstring says so.
 * - `bounded_count` returns an `int` in 1..N, so no caller-supplied string
 *   reaches argv at all.
 * - `_validate_name` (services/terminal_worktree_service.py) requires
 *   `^[a-z0-9][a-z0-9_-]*$` and now returns the checked name. Leading
 *   alphanumeric, no separators: it can be neither an option nor a path
 *   escape. It was widened to a barrier only after the pattern was tightened
 *   in this same change -- the old `^[a-z0-9-_]+$` matched `-force`.
 *
 * Deliberately NOT registered: any "this executable exists" check. Confirming
 * that a caller-supplied launcher path names a real binary does not constrain
 * WHICH program runs, so registering it would hide a genuine finding. The one
 * place that took a launcher from a request body (`customPath` in
 * routes/worktree_tools.py) had the field removed instead.
 */
class ArgvSanitizer extends CommandInjection::Sanitizer {
  ArgvSanitizer() {
    exists(DataFlow::CallCfgNode call, string name |
      name in [
          "assert_safe_git_ref", "assert_not_option", "bounded_count", "_validate_name"
        ] and
      (
        call.getFunction().asExpr().(Name).getId() = name or
        call.getFunction().asExpr().(Attribute).getName() = name
      ) and
      this = call
    )
    or
    // The body of the validator IS the check: barrier its first parameter so
    // the regex inside the helper does not re-fire the alert the barrier
    // exists to clear. Mirrors the containment-helper handling in
    // PathInjectionSanitized.ql.
    exists(Function f |
      f.getName() in ["assert_safe_git_ref", "assert_not_option"] and
      this.(DataFlow::ParameterNode).getParameter() = f.getArg(0)
    )
  }
}

from CommandInjectionFlow::PathNode source, CommandInjectionFlow::PathNode sink
where CommandInjectionFlow::flowPath(source, sink)
select sink.getNode(), source, sink, "This command line depends on a $@.", source.getNode(),
  "user-provided value"
