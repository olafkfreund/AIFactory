/**
 * @name Clear-text storage of sensitive information (sanitizer-aware)
 * @description Sensitive information stored without encryption or hashing can expose it to an
 *              attacker.
 * @kind path-problem
 * @problem.severity error
 * @security-severity 7.5
 * @precision high
 * @id py/clear-text-storage-sensitive-data-sanitized
 * @tags security
 *       external/cwe/cwe-312
 *       external/cwe/cwe-315
 *       external/cwe/cwe-359
 */

import python
private import semmle.python.dataflow.new.DataFlow
import CleartextStorageFlow::PathGraph
import semmle.python.security.dataflow.CleartextStorageQuery
import semmle.python.security.dataflow.CleartextStorageCustomizations

/**
 * The same swap as the four `*Sanitized.ql` files next door, for the same
 * reason stated in `codeql-config.yml`: stock CodeQL has no notion of an
 * encryption barrier defined in another module. `routes/settings.py`'s
 * `_write_json_store` writes `json.dumps(seal_profiles(data))`, and the value
 * reaching that write is AES-256-GCM ciphertext produced by
 * `crypto/secret_field.py` on the repo's existing `crypto.kms` backend -- but
 * the seal helpers are imported, so the alert could not clear however
 * completely the payload was encrypted (#1276, #1293).
 *
 * Registered: `seal`, `seal_profiles`, `seal_fields`
 * (`server/crypto/secret_field.py`). Each returns
 * `"enc.v1:" + urlsafe_b64encode(backend.encrypt(...))` for every credential
 * field it touches; a value that has been through one of them is ciphertext,
 * which is exactly the property CWE-312 asks about.
 *
 * THE BARRIER IS CONDITIONAL, and this is the load-bearing caveat. `seal()`
 * DEGRADES to returning the plaintext, with a one-time warning, when no KMS
 * backend was selected at all -- so registering it asserts that a key actually
 * reaches the process. Two things hold that up, and if either goes the
 * exclusion in `codeql-config.yml` must go with it:
 *   - `tests/helm/test_kms_key_always_wired.py` asserts the chart hands the
 *     pod a key on the default render and on every selectable backend, and
 *     that selecting a cloud backend without its key refuses to render at all
 *     (#1290). It runs in CI's `helm (P4 acceptance)` job.
 *   - `crypto.kms.encryption_is_required()` makes the fallback raise rather
 *     than write plaintext whenever a backend WAS selected -- the silent case
 *     is only "nobody configured anything", which is no worse than the
 *     `chmod 0600` that predated #1276.
 *
 * Deliberately NOT registered: `unseal`, `unseal_profiles`, `unseal_fields`.
 * They move ciphertext BACK to plaintext, and their output reaching a file
 * write is precisely the finding this rule exists to report. Registering them
 * would invert the query.
 */
class SealSanitizer extends CleartextStorage::Sanitizer {
  SealSanitizer() {
    exists(DataFlow::CallCfgNode call, string name |
      name in ["seal", "seal_profiles", "seal_fields"] and
      (
        call.getFunction().asExpr().(Name).getId() = name or
        call.getFunction().asExpr().(Attribute).getName() = name
      ) and
      this = call
    )
  }
}

from
  CleartextStorageFlow::PathNode source, CleartextStorageFlow::PathNode sink, string classification
where
  CleartextStorageFlow::flowPath(source, sink) and
  classification = source.getNode().(Source).getClassification()
select sink.getNode(), source, sink, "This expression stores $@ as clear text.", source.getNode(),
  "sensitive data (" + classification + ")"
