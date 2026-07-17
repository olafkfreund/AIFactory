# terraform

> Source: curated best practices | 2026

---

# Terraform - Module structure, remote state, and safe changes

Terraform 1.9 manages infrastructure as declarative code. Production-grade Terraform means remote state with locking (never local state for shared infra), reusable modules with typed variables and explicit outputs, pinned provider versions, per-environment isolation, and a plan-review-apply discipline that catches destructive changes before they run. This skill covers module layout, state backends, variable typing and validation, and workflow safety.

## When to Activate

Use when the task involves Terraform:
- Writing or structuring modules (variables, outputs, resources)
- Configuring remote state backends and locking
- Managing environments (dev/staging/prod), workspaces
- Provider version pinning, `for_each`/`count`, data sources
- Reviewing a plan for destructive changes / drift

## Patterns and Best Practices

### Module structure — reusable, typed, documented

```
modules/
  network/
    main.tf          # resources
    variables.tf     # typed inputs with validation
    outputs.tf       # explicit outputs
    versions.tf      # required_version + provider constraints
environments/
  prod/
    main.tf          # calls modules, passes prod values
    backend.tf       # remote state config
  staging/
    main.tf
```

```hcl
# modules/network/variables.tf
variable "cidr_block" {
  type        = string
  description = "VPC CIDR range"
  validation {
    condition     = can(cidrhost(var.cidr_block, 0))
    error_message = "cidr_block must be a valid CIDR."
  }
}

variable "environment" {
  type = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be dev, staging, or prod."
  }
}

# modules/network/outputs.tf
output "vpc_id" {
  value       = aws_vpc.this.id
  description = "ID of the created VPC"
}
```

Type every variable, validate constraints, and expose only the outputs consumers need. A module with untyped `any` variables and no validation is a foot-gun.

### Remote state with locking — never local state for shared infra

```hcl
# environments/prod/backend.tf — S3 backend with native locking (TF 1.9+)
terraform {
  backend "s3" {
    bucket       = "acme-tfstate-prod"
    key          = "network/terraform.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true          # S3-native state locking (no DynamoDB table needed in 1.9+)
  }
}
```

Remote state gives the team a shared source of truth; locking prevents two `apply`s from corrupting state. Enable encryption at rest. State contains secrets (passwords, keys) in plaintext — restrict bucket access tightly and never commit state to git.

### Provider and version pinning

```hcl
# versions.tf
terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"          # pin minor; allow patch updates
    }
  }
}
```

Commit `.terraform.lock.hcl` so everyone resolves identical provider builds.

### Resource iteration — for_each over count

```hcl
# for_each keyed by a stable string → adding/removing one item doesn't reindex the rest
resource "aws_subnet" "this" {
  for_each          = var.subnets            # map(object({...}))
  vpc_id            = aws_vpc.this.id
  cidr_block        = each.value.cidr
  availability_zone = each.value.az
  tags              = { Name = each.key }
}
```

`count` reindexes on insert/delete (destroys/recreates unrelated resources); `for_each` with a map is stable.

### Workflow safety

```bash
terraform fmt -check && terraform validate     # format + syntax gate in CI
terraform plan -out=tfplan                     # produce a reviewable, applyable plan
terraform show -no-color tfplan                 # review — scan for "destroy"/"replace"
terraform apply tfplan                          # apply the EXACT reviewed plan
```

Always `plan -out` then `apply` that saved plan — applying without a saved plan can execute something different from what was reviewed. In CI, gate `apply` behind human approval for prod. Use `-target` only for surgical recovery, never as routine workflow.

### Secrets

Never hardcode credentials in `.tf` files or `terraform.tfvars` committed to git. Source them from environment (`TF_VAR_*`), a secrets manager data source, or the provider's ambient credentials. Mark sensitive outputs with `sensitive = true`.

## Anti-patterns

- Local state for shared/team infrastructure — corruption and lost updates; use a locked remote backend.
- Committing `terraform.tfstate` or secrets-bearing `.tfvars` to git — state holds plaintext secrets.
- Unpinned providers / missing `.terraform.lock.hcl` — non-reproducible plans.
- `apply` without a saved `-out` plan — you may apply something other than what you reviewed.
- `count` for named resources — reindexing destroys unrelated resources on insert/delete.
- One giant root module / monolithic state — slow plans, huge blast radius; split by environment and concern.
- Untyped `any` variables with no `validation` blocks.
- Routine reliance on `-target` — masks real drift and dependency issues.
