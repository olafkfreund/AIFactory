# AWS managed-web-app template (reference): App Runner + RDS Postgres + ElastiCache
# Redis. No EKS. Mirrors the GCP/Azure templates; CI runs `terraform apply` with
# AWS creds in repo secrets (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY) + a remote
# state backend (S3). Not exercised in the showcase — provided as the third cloud.
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}
provider "aws" { region = var.region }
variable "region" {
  type    = string
  default = "eu-west-2"
}
variable "image" {
  type = string # ECR image ref built by CI
}

resource "random_password" "db" {
  length  = 20
  special = false
}

resource "aws_db_instance" "pg" {
  identifier           = "ttt-pg"
  engine               = "postgres"
  engine_version       = "15"
  instance_class       = "db.t3.micro"
  allocated_storage    = 20
  db_name              = "ttt"
  username             = "ttt"
  password             = random_password.db.result
  skip_final_snapshot  = true
  publicly_accessible  = false
}

resource "aws_elasticache_cluster" "redis" {
  cluster_id           = "ttt-redis"
  engine               = "redis"
  node_type            = "cache.t3.micro"
  num_cache_nodes      = 1
  parameter_group_name = "default.redis7"
}

# App Runner with a VPC connector so it can reach private RDS + ElastiCache.
resource "aws_apprunner_service" "app" {
  service_name = "ttt-app"
  source_configuration {
    image_repository {
      image_identifier      = var.image
      image_repository_type = "ECR"
      image_configuration {
        port = "8080"
        runtime_environment_variables = {
          DATABASE_URL = "postgresql://ttt:${random_password.db.result}@${aws_db_instance.pg.address}:5432/ttt"
          REDIS_HOST   = aws_elasticache_cluster.redis.cache_nodes[0].address
          REDIS_PORT   = "6379"
        }
      }
    }
  }
}
output "url" { value = "https://${aws_apprunner_service.app.service_url}" }
