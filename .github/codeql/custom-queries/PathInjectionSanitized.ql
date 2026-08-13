/**
 * @name Uncontrolled data used in path expression (sanitizer-aware)
 * @description Accessing paths influenced by users can allow an attacker to access unexpected resources.
 * @kind path-problem
 * @problem.severity error
 * @security-severity 7.5
 * @sub-severity high
 * @precision high
 * @id py/path-injection-sanitized
 * @tags correctness
 *       security
 *       external/cwe/cwe-022
 *       external/cwe/cwe-023
 *       external/cwe/cwe-036
 *       external/cwe/cwe-073
 *       external/cwe/cwe-099
 */

import python
import semmle.python.dataflow.new.DataFlow
import semmle.python.ApiGraphs
import semmle.python.security.dataflow.PathInjectionQuery
import semmle.python.security.dataflow.PathInjectionCustomizations
import PathInjectionFlow::PathGraph

/**
 * Barriers for path injection in this repo.
 *
 * `safe_spec_component` (server/specpath.py) validates a request-supplied path
 * component with a `fullmatch` against a restrictive allow-list and RAISES
 * rather than sanitising, so anything downstream of it is a component that
 * cannot escape its root. It is deliberately a fullmatch and not an ad-hoc
 * `if "/" in value` check: CodeQL models the former as a sanitizer and not the
 * latter, so a hand-rolled guard hardens the code without ever clearing the
 * alert.
 *
 * `os.path.basename` strips directory parts outright, which is the same
 * guarantee by a different route.
 *
 * `contained_path` (server/specpath.py) is the same idea for a whole PATH
 * rather than one component: it resolves the candidate and returns it only if
 * the RESOLVED form is inside one of the roots it was given, raising
 * otherwise. That constrains which path is reached, which is the property a
 * barrier has to establish. The resolve happens inside the helper on purpose,
 * so there is no window in which a resolved-but-unchecked path exists in the
 * caller for flow to escape through.
 *
 * `within_roots` (server/specpath.py, #1278) is `contained_path` with the
 * ValueError mapped to HTTP 403, and nothing else. It is registered by name
 * as well as by its wrapped call because it is the form every route uses, and
 * a barrier the routes do not actually call is a barrier that does nothing.
 *
 * Deliberately NOT registered, and worth naming because they are the tempting
 * wrong answers: `exists()`, `is_file()`, `is_dir()`. They report whether a
 * path is THERE, never whether it is ALLOWED. `/etc/passwd` exists.
 *
 * Deliberately NOT registered, and this one is the load-bearing omission:
 * **the project-registry lookup**, i.e. `projects[project_id]["path"]` where
 * `projects` came from `load_projects()`, and `resolve_project_path`. #1278
 * made the registry a genuine trust boundary (`add_project` and
 * `update_project` now confine through `within_roots`), so registering it
 * would finally have been TRUTHFUL. It was measured on this tree anyway,
 * because VALIDATION.md says to, and it clears exactly nothing: the barrier
 * matches 93 nodes, and the alert count is 119 with it and 119 without it.
 *
 * The reason is worth recording, because the arithmetic in #1278 says
 * otherwise. Those sinks are each reached by ~14 sources, of which the
 * registry lookup is one; the others are `spec_id` and `task_id` route
 * parameters that never touch the registry. Removing one source from a sink
 * that has thirteen more does not clear the sink. The "~42 alerts clear in one
 * edit" estimate came from counting sinks per SOURCE, which counts each sink
 * once per source that reaches it.
 *
 * So it stays out. A barrier that clears nothing is not free: it is another
 * claim in this file that a future reader has to re-verify.
 *
 * Deliberately NOT registered: `Path.resolve()`. Resolving does not confine a
 * path, it only canonicalises one, and `Path("/srv") / "../../etc"` resolves
 * happily to `/etc`. Treating it as a barrier would silence the exact bug
 * AIFactory#1056 was about.
 */
class SpecPathSanitizer extends PathInjection::Sanitizer {
  SpecPathSanitizer() {
    this = API::moduleImport("os").getMember("path").getMember("basename").getACall()
    or
    exists(DataFlow::CallCfgNode call, string name |
      name in [
          "safe_spec_component", "_safe_spec_component", "contained_path", "within_roots"
        ] and
      (
        call.getFunction().asExpr().(Name).getId() = name or
        call.getFunction().asExpr().(Attribute).getName() = name
      ) and
      this = call
    )
    or
    // The validator's own parameter. Without this, the `str(value)` and the
    // regex probe INSIDE safe_spec_component re-report the very flow the
    // barrier exists to clear -- the helper would indict itself.
    exists(Function f |
      f.getName() in [
          "safe_spec_component", "_safe_spec_component", "contained_path", "within_roots"
        ] and
      this.(DataFlow::ParameterNode).getParameter() = f.getArg(0)
    )
  }
}

from PathInjectionFlow::PathNode source, PathInjectionFlow::PathNode sink
where PathInjectionFlow::flowPath(source, sink)
select sink.getNode(), source, sink, "This path depends on a $@.", source.getNode(),
  "user-provided value"
