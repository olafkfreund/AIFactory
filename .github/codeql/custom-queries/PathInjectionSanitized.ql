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
      name in ["safe_spec_component", "_safe_spec_component"] and
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
      f.getName() in ["safe_spec_component", "_safe_spec_component"] and
      this.(DataFlow::ParameterNode).getParameter() = f.getArg(0)
    )
  }
}

from PathInjectionFlow::PathNode source, PathInjectionFlow::PathNode sink
where PathInjectionFlow::flowPath(source, sink)
select sink.getNode(), source, sink, "This path depends on a $@.", source.getNode(),
  "user-provided value"
