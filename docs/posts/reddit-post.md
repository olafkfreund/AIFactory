# Reddit post templates

Three subreddit-tuned drafts for the same story: an autonomous coder that cannot record a passing test unless a real runner actually ran. Pick one, post as a text/self post, and reply to questions from the account that posted. Lead with the mechanism, not the product.

---

## r/programming

**Title:** We built an autonomous coder that can't mark a test green unless a real runner actually ran

**Body:**

Autonomous coding agents almost all share one weakness: they grade their own homework. The agent reports the tests passed and you take its word, because checking is expensive. We wanted to remove the word entirely.

Two design choices do most of the work:

- **Job-native build.** Every task builds inside its own throwaway Kubernetes Job. The Job is created for that task, refreshed to the current tip of the target repo's main branch, does the work, opens its own pull request, and is then destroyed. No long-lived worker, no shared workspace, no state carried between tasks.
- **Tamper-evident test-evidence gate.** Inside the Job, a subtask's tests can be marked passing only if a real test command actually ran and produced that result. The green checkbox is bound to observed execution, not to the agent's claim. If no runner ran, there is no pass to record.

The part that convinced me it works was a failure. Minutes before a clean run on a `clamp()` helper, the same pipeline built a `slugify()` helper that compiled and looked right but failed one of twelve test verdicts on a unicode edge case. The gate capped it at the lowest assurance level and auto-filed a handback instead of certifying it. Same machinery produced an honest pass and an honest fail on the same day.

One real gap the run surfaced: the verify verdict is computed correctly but its auto-post back onto the PR is still gated by a fix we're tracking as an open issue. Naming it rather than hiding it.

Full write-up (technical, no signup) is in the AIFactory blog.

**FAQ**

- *Isn't this just CI?* CI runs after a human opens a PR. Here the agent opens the PR, and the gate lives inside the agent's own build so it can't self-report a pass it never ran. CI is still welcome on top.
- *Can the agent disable the gate?* The pass is bound to execution evidence, so removing the runner removes the pass — there is nothing to fake, only nothing to record.
- *What model?* The coder is model-agnostic; the honesty property is structural, not a prompt.

---

## r/MachineLearning

**Title:** [P] Structurally preventing an LLM coding agent from claiming tests passed when they didn't

**Body:**

A recurring reliability problem with LLM coding agents: the agent's self-report is unverifiable. It says the tests are green and there's no cheap oracle to confirm it, so the failure mode is confident overclaiming.

We took a structural approach rather than a prompt-based one. The agent runs inside a per-task, disposable Kubernetes Job refreshed to the current main branch. A test-evidence gate binds a passing-test result to the observed execution of a real test runner: no run, no recordable pass. The optimistic failure mode is closed by construction, not by asking the model nicely.

A concrete example of it working against us: a `slugify()` build looked correct but failed one of twelve test verdicts on a unicode edge case. The verification layer capped it at the floor assurance level and filed an automatic handback rather than certifying it. On a separate `clamp()` task the same day it correctly reported a clean pass — nine tests kept, none rejected, a mutation probe killed, stable across three runs — and correctly reported the higher assurance levels as not_run because a pure function has no API or integration lane to exercise. Untested dimensions are reported as gaps, never as passes.

Honest caveat: in that run the verdict is computed correctly but its auto-post back to the PR is gated by a fix we track as an open issue.

Write-up in the AIFactory blog. Happy to discuss the evidence-binding design.

**FAQ**

- *Why not just trust eval metrics?* Metrics you can't trace to execution are the problem. This binds the metric to the run.
- *Is the honesty prompt-engineered?* No — it's a gate on execution evidence, so it holds regardless of what the model says.

---

## r/devops

**Title:** Every task builds in its own throwaway Kubernetes Job and can't fake a green test

**Body:**

Sharing an architecture that might interest the CI/CD crowd. Our autonomous coder doesn't use a persistent build worker. Every task gets its own ephemeral Kubernetes Job:

- created per task, refreshed to the current tip of the target repo's main branch
- does the build, opens its own pull request
- destroyed afterward — nothing persists between tasks except the commit and the PR

No shared workspace means one build can't poison the next, and "refreshed to current main" means no stale-base drift. On top of that, a tamper-evident test-evidence gate makes a passing-test result impossible unless a real test runner actually executed inside the Job.

It earns its keep on failures. A `slugify()` build looked fine but failed a unicode edge-case verdict; the gate capped it at the floor assurance level and auto-filed a handback instead of shipping it. Running agents also stream their terminals live into a cockpit, so you watch the build and the test run happen rather than reconstruct them from logs after.

One honest gap from the live run: the verdict computes correctly but its auto-post back onto the PR is gated by a fix we track as an open issue.

Technical write-up is on the AIFactory blog.

**FAQ**

- *Job-per-task overhead?* Real, but the isolation and current-main guarantees are worth it for autonomous runs where drift is the silent killer.
- *How is this different from an ephemeral CI runner?* The runner is standard; the difference is the agent lives inside it and the evidence gate is inside the agent's build, so it can't self-certify.
