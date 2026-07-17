# aws

> Source: curated best practices | 2026

---

# AWS - Least-privilege IAM, secure defaults, and core services

Building on AWS safely means least-privilege IAM (roles over long-lived keys), private-by-default networking, encryption everywhere, and infrastructure expressed as code. The recurring failure modes are over-broad IAM policies (`*` actions/resources), public S3 buckets, security groups open to `0.0.0.0/0`, and static access keys committed or leaked. This skill covers IAM roles and policies, S3 hardening, VPC/security-group design, secrets management, and secure defaults for common services.

## When to Activate

Use when the task involves AWS:
- IAM roles, policies, or credentials
- S3 buckets, encryption, or access control
- VPC, subnets, security groups, networking
- Compute (EC2/ECS/Lambda/App Runner/EKS) or RDS setup
- Secrets management (Secrets Manager / SSM), KMS

## Patterns and Best Practices

### IAM — least privilege, roles not keys

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:GetObject", "s3:PutObject"],
    "Resource": "arn:aws:s3:::acme-uploads/*",
    "Condition": { "Bool": { "aws:SecureTransport": "true" } }
  }]
}
```

Grant specific actions on specific resource ARNs — never `"Action": "*"` on `"Resource": "*"`. Attach policies to **roles** and let compute assume them (EC2 instance profile, ECS task role, Lambda execution role, IRSA for EKS, OIDC for CI). Long-lived IAM user access keys are the top credential-leak vector — avoid them; if unavoidable, rotate and scope tightly. Use `Condition` blocks (source IP, MFA, TLS) to tighten further.

### S3 — private, encrypted, TLS-only

```hcl
resource "aws_s3_bucket" "uploads" { bucket = "acme-uploads" }

resource "aws_s3_bucket_public_access_block" "uploads" {
  bucket                  = aws_s3_bucket.uploads.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true         # all four → no accidental public exposure
}

resource "aws_s3_bucket_server_side_encryption_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  rule { apply_server_side_encryption_by_default { sse_algorithm = "aws:kms" } }
}

resource "aws_s3_bucket_versioning" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  versioning_configuration { status = "Enabled" }   # recover from deletes/ransomware
}
```

Serve private objects via **pre-signed URLs** or CloudFront with Origin Access Control — never a public bucket policy. Enforce TLS with a bucket policy denying `aws:SecureTransport=false`.

### VPC and security groups — private by default

```hcl
resource "aws_security_group" "app" {
  name   = "app"
  vpc_id = aws_vpc.main.id
  egress {                                   # explicit egress
    from_port = 0; to_port = 0; protocol = "-1"; cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group_rule" "db_from_app" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.db.id
  source_security_group_id = aws_security_group.app.id   # reference SG, not a CIDR
}
```

Put databases and internal services in **private subnets** (no route to an internet gateway); reach them via NAT for egress only. Reference source security groups instead of CIDR ranges. Never open SSH/DB ports to `0.0.0.0/0` — use SSM Session Manager for shell access (no bastion, no open port 22).

### RDS — encrypted, private, TLS

```hcl
resource "aws_db_instance" "main" {
  engine                  = "postgres"
  engine_version          = "16"
  storage_encrypted       = true            # KMS encryption at rest
  publicly_accessible     = false           # private subnet only
  multi_az                = true            # HA failover for prod
  backup_retention_period = 7
  deletion_protection     = true
  # credentials from Secrets Manager, not hardcoded:
  manage_master_user_password = true         # AWS-managed rotation in Secrets Manager
}
```

### Secrets — never static, never in code

```python
import boto3, json
secret = json.loads(
    boto3.client("secretsmanager").get_secret_value(SecretId="prod/db")["SecretString"]
)
```

Store credentials in Secrets Manager (rotation) or SSM Parameter Store (SecureString). Applications read them at runtime via their IAM role. Never commit secrets, bake them into AMIs/images, or pass them on a command line.

### Observability and cost

Enable CloudTrail (audit), GuardDuty (threat detection), and Config (drift/compliance) org-wide. Tag resources (`Environment`, `Owner`, `CostCenter`) for cost allocation and cleanup. Set budget alarms.

## Anti-patterns

- IAM policies with `"Action": "*"` / `"Resource": "*"` — over-privileged blast radius.
- Long-lived IAM user access keys instead of assumed roles / OIDC.
- Public S3 buckets or disabled public-access-block — the classic data-leak headline.
- Security groups open to `0.0.0.0/0` on SSH (22) / DB (5432/3306) ports.
- Hardcoded credentials in code, env files, AMIs, or Terraform `.tfvars` in git.
- Databases in public subnets / `publicly_accessible = true`.
- Unencrypted storage (S3/EBS/RDS) — encrypt at rest with KMS by default.
- No CloudTrail / no MFA on root / no budget alarms.
