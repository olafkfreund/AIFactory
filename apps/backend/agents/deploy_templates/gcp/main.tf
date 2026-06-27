terraform {
  required_version = ">= 1.5"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

variable "project_id" { type = string }
variable "region" { type = string }
variable "image" { type = string } # full Artifact Registry image ref (built by CI)

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "random_password" "db" {
  length  = 20
  special = false
}

# --- Serverless VPC Access connector: lets Cloud Run reach private Memorystore ---
resource "google_vpc_access_connector" "conn" {
  name          = "ttt-conn"
  region        = var.region
  network       = "default"
  ip_cidr_range = "10.8.0.0/28"
  min_instances = 2
  max_instances = 3
}

# --- Memorystore Redis (basic, smallest) ---
resource "google_redis_instance" "cache" {
  name               = "ttt-redis"
  tier               = "BASIC"
  memory_size_gb     = 1
  region             = var.region
  authorized_network = "default"
  redis_version      = "REDIS_7_0"
}

# --- Cloud SQL Postgres (smallest tier) ---
resource "google_sql_database_instance" "pg" {
  name             = "ttt-pg"
  database_version = "POSTGRES_15"
  region           = var.region
  settings {
    tier              = "db-f1-micro"
    availability_type = "ZONAL"
    ip_configuration { ipv4_enabled = true } # public IP; Cloud Run connects via the Cloud SQL socket
  }
  deletion_protection = false
}

resource "google_sql_database" "app" {
  name     = "ttt"
  instance = google_sql_database_instance.pg.name
}

resource "google_sql_user" "app" {
  name     = "ttt"
  instance = google_sql_database_instance.pg.name
  password = random_password.db.result
}

# --- Cloud Run service ---
resource "google_cloud_run_v2_service" "app" {
  name     = "ttt-app"
  location = var.region
  deletion_protection = false

  template {
    vpc_access {
      connector = google_vpc_access_connector.conn.id
      egress    = "PRIVATE_RANGES_ONLY"
    }
    volumes {
      name = "cloudsql"
      cloud_sql_instance { instances = [google_sql_database_instance.pg.connection_name] }
    }
    containers {
      image = var.image
      ports { container_port = 8080 }
      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
      env {
        name  = "DATABASE_URL"
        value = "postgresql://ttt:${random_password.db.result}@/ttt?host=/cloudsql/${google_sql_database_instance.pg.connection_name}"
      }
      env {
        name  = "REDIS_HOST"
        value = google_redis_instance.cache.host
      }
      env {
        name  = "REDIS_PORT"
        value = tostring(google_redis_instance.cache.port)
      }
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.app.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "url" { value = google_cloud_run_v2_service.app.uri }
