# AIFactory coding skills

A curated, provider-neutral library of coding playbooks. Each skill is a
markdown file the coder agent reads as context, so the same idioms and best
practices apply whether the build runs on Claude, Gemini, or a self-hosted
Ollama model.

## How it is used

1. At build time `skills_service.suggest_skills(task_description)` scores every
   skill against the spec (keyword + synonym matching) and writes the top
   matches to `task_metadata.json` as `suggestedSkills`.
2. The planner reviews them against the actual code and confirms the relevant
   ones as `selectedSkills` (or the build auto-falls-back to the suggestions, so
   skills are always applied).
3. `agent_skill_context` loads up to 5 selected skills and writes
   `skill_context.md` into the spec dir; the coder reads it as context.

## Layout

`skills/<category>/<name>.md`. The first prose paragraph is the routing
description the matcher scores; `## When to Activate` bullets are the triggers.

- `languages/` — python, typescript, go, rust, java, csharp, ruby, php, kotlin, cpp
- `backend/` — fastapi, django, express, spring-boot, rails, dotnet, nestjs
- `frontend/` — react, vue, nextjs, svelte, tailwind, angular
- `data/` — sql-postgres, mongodb, redis, sqlalchemy
- `infra/` — docker, kubernetes, terraform, github-actions, nix, aws
- `quality/` — testing, security, error-handling, api-design, clean-code, performance, git-workflow

## Adding a skill

Drop a new `skills/<category>/<name>.md` following the format above. It is
discovered automatically (the loader scans this directory; override the location
with `APP_SKILLS_PATH`). Keep one clear job per skill and prefer concrete worked
examples over abstract rules.
