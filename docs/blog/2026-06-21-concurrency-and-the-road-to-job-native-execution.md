---
slug: concurrency-and-the-road-to-job-native-execution
title: "Concurrency, durable job-state, and the road to Job-native execution"
authors: [olaf]
tags: [ai-coding, devops, concurrency, kubernetes, multi-provider]
date: 2026-06-21
description: A week recap (2026-06-15 to 06-21). AIFactory went from a single in-pod instance to a concurrency-capable control plane — durable Postgres job-state, admission control, KEDA autoscaling and Nix-per-task — then landed the mechanisms for fully Job-native execution. The build/verify default flips are converging but are not live yet; this post is honest about exactly where the line is.
---

This was a concurrency week. AIFactory started it as a single-instance app that
ran one build at a time inside its own pod, and ended it with a control plane
that can run many builds at once across replicas, plus the plumbing for moving
the heavy work out of the web-server pod entirely and into per-task Kubernetes
Jobs. The last part is not finished — and the honest version of that is the most
useful thing in this post, so it gets its own section near the end.

{/* truncate */}

## RFC-0016: from one instance to many

The single-instance constraint was the real ceiling. One build at a time meant
the throughput of the whole factory was the throughput of one pod. RFC-0016 took
that apart in a few load-bearing pieces.

**Durable Postgres job-state (#668).** The admission cap, the FIFO queue and the
running set used to live in process memory. That's fine for one replica and
fatal for several — two replicas with two private in-memory queues will both
think they're under the cap and both admit, and the cap means nothing. So the
job-state moved into Postgres: `queued` / `running` rows in a shared table, with
a per-service advisory lock so the admission decision is serialized across every
replica. Crucially this is opt-in by configuration — the durable store only
activates when a real shared `DATABASE_URL` is set, so a laptop running SQLite
behaves exactly as before.

**Admission control + queue (#668).** On top of the durable state sits the
actual policy: a concurrency cap with a FIFO queue in front of it. Submit more
work than the cap allows and the overflow queues instead of either failing or
overwhelming the cluster. This is what makes "many builds at once" a bounded,
predictable thing rather than a thundering herd.

**KEDA autoscaling.** With state shared and admission bounded, the replica count
can follow the queue. KEDA scales the web-server deployment on queue depth — we
proved a 1→3 scale-out under load earlier in the rollout — so capacity tracks
demand instead of being pinned at one.

**Nix-per-task substrate (#197, #674).** The execution environment got leaner
and more reproducible at the same time. The Nix gate Jobs now mount a warm
`/nix-store` PVC so they don't re-fetch the world on every run, and the coder
image was shrunk to a thin control plane (#674) by dropping the baked-in
language toolchains — those come from the per-task Nix environment the
planner/coder declare in the contract, not from a fat image that has to contain
every language anyone might ever need.

The theme across all of it is the same one we keep coming back to: make the
shared thing authoritative (Postgres), make the policy explicit (admission +
queue), and keep the single-instance path untouched for anyone who isn't running
the concurrent topology.

## RFC-0017: the mechanisms for Job-native, multi-replica execution

RFC-0016 made the control plane concurrency-capable. RFC-0017 is about the two
things you need before the heavy work can actually leave the web-server pod and
run as its own Kubernetes Job, in a world where there's more than one web-server
replica.

**Redis-backed rmux transport (#684 / #681).** The live console (rmux) used to
assume the build was a subprocess of the same pod the browser's websocket landed
on. With multiple replicas that assumption breaks: your websocket can hit
replica B while the build runs on replica A, and you'd see nothing. #681 moves
the rmux transport onto Redis, so the live stream is decoupled from which replica
you're connected to. Any replica can serve any session's console.

**Job-native log streaming (#683 / #680).** The other half: when the build runs
as a Job instead of an in-pod subprocess, the logs have to come from the Job, not
from a child process the web-server owns. #680 adds a Job-native log streamer
that tails the Job's output into the same two sinks the in-pod path already feeds
— so the portal's Logs tab and the persisted log look identical whether the build
ran in-pod or as a Job. That symmetry is deliberate: it's what lets the backend
be swapped underneath without the UI or the stored artifacts noticing.

There was also a concurrency-safety fix worth naming: shared base-repo git
mutations are now serialized across builds (#669), and a Claude token pool feeds
concurrent builds (#670) so parallel runs don't contend over a single
credential.

## Round 5: getting the build Job's credentials and workspace right

A Job that runs the coder loop needs three things the in-pod subprocess got for
free: the right image, a populated workspace, and credentials. This week's round
of fixes worked through exactly those, in order, as each one surfaced the next:

- The build Job must use the **python-capable AIFactory image**, not the thin Nix
  gate image (#685) — the gate image can't run `run.py`.
- It must invoke `run.py` by its **absolute path inside the image** (#686), not as
  a bare `run.py` resolved against `/work`.
- Its **worktree must be populated** (git checkout + spec) before dispatch (#687),
  because the Job doesn't inherit the web-server's already-prepared workspace.
- And it must have the **build environment injected** — the Claude OAuth token and
  the SDK env (#688) — or the agent inside the Job can't authenticate.

Each of these was a real, specific failure caught by actually running the path,
not a hypothetical. That's the nature of moving execution across a process
boundary: everything the in-pod path took for granted has to be made explicit.

## Where the line actually is: the default flips are NOT live

Here's the honest part. The mechanisms above are merged. The **default execution
backend is still in-pod**, and the **verify path still runs on its safe in-pod
defaults**. The Job-native build/verify default flips (#671, #466) are
*converging*, not shipped.

In code, `AIFACTORY_BUILD_BACKEND` defaults to `subprocess` — today's in-pod
behaviour, unchanged. `kubejob` is opt-in. The module that selects the backend
describes the flip as "READY", and that's accurate: the plumbing exists and the
log-stream symmetry that was the last blocker is in place. But READY is not DONE.
Flipping the default is gated on validation we haven't passed yet — the most
recent build-Job bug is that `/work` in the Job has no `.git`, and the verify
path still needs to be re-validated end-to-end before it can run Job-native by
default.

So if you're reading this and wondering whether your builds are running as
Kubernetes Jobs right now: they are not, unless you've explicitly set
`AIFACTORY_BUILD_BACKEND=kubejob`. The default is the in-pod subprocess, and it
stays that way until the Job path has earned the flip by passing on real work.
We'd rather ship the mechanism behind a default-off flag and converge through
real bugs than flip the default and have it fail under you.

## The shape of next week

The work that's left is the validation, not the mechanism: populate the Job
worktree's `.git` so the build path is green end-to-end, re-validate the verify
path under the Job backend, and only then flip the defaults — one at a time,
behind the same evidence bar everything else clears. The control plane is ready
for concurrency. The execution layer is one honest validation pass away from
following it out of the pod.
