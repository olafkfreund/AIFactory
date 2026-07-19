# LinkedIn post template

---

Most autonomous coding agents are trusted on their word. They report a green test suite and you believe them, because checking is expensive. We built AIFactory so that word costs nothing and the evidence costs everything.

Two design choices carry it:

Every task builds inside its own throwaway Kubernetes Job — created for the task, refreshed to the current tip of the target repo's main branch, opens its own pull request, then destroyed. No long-lived worker, no shared workspace, no state drifting between builds.

Inside that Job, a tamper-evident test-evidence gate makes a passing-test result impossible unless a real test runner actually ran. The green checkbox is bound to observed execution, not to the agent's claim.

The proof was a failure. Minutes before a clean run on a small helper, the same pipeline built another that compiled, looked correct, and failed one test verdict on a unicode edge case. The gate refused to certify it, capped it at the lowest assurance level, and auto-filed a fix. A pipeline that can be honest about a pass is only worth trusting if it is equally willing to be honest about a failure.

We are also naming the rough edge the same run surfaced: the verdict is computed correctly but its auto-post back to the pull request is still gated by a fix we track as an open issue. Tests that refuse to lie, on both sides of the story.

A live walkthrough of the full run is available on request.

#AutonomousAgents #AICoding #SoftwareEngineering #Kubernetes #DevOps #SoftwareTesting #LLM #Reliability
