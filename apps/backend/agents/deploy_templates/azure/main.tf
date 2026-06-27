terraform {
  required_version = ">= 1.5"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.0" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

variable "subscription_id" { type = string }
variable "location" {
  type    = string
  default = "uksouth"
}
variable "image" {
  type    = string
  default = "" # acr/app:tag — empty in phase-1 (infra only)
}
variable "prefix" {
  type    = string
  default = "ttt"
}

resource "random_password" "db" {
  length  = 20
  special = false
}
resource "random_string" "suffix" {
  length  = 5
  upper   = false
  special = false
}

resource "azurerm_resource_group" "rg" {
  name     = "${var.prefix}-rg"
  location = var.location
}

resource "azurerm_container_registry" "acr" {
  name                = "${var.prefix}acr${random_string.suffix.result}"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  sku                 = "Basic"
  admin_enabled       = true
}

resource "azurerm_postgresql_flexible_server" "pg" {
  name                          = "${var.prefix}-pg-${random_string.suffix.result}"
  resource_group_name           = azurerm_resource_group.rg.name
  location                      = azurerm_resource_group.rg.location
  version                       = "15"
  administrator_login           = "ttt"
  administrator_password        = random_password.db.result
  sku_name                      = "B_Standard_B1ms"
  storage_mb                    = 32768
  zone                          = "1"
  public_network_access_enabled = true
}
resource "azurerm_postgresql_flexible_server_database" "db" {
  name      = "ttt"
  server_id = azurerm_postgresql_flexible_server.pg.id
}
resource "azurerm_postgresql_flexible_server_firewall_rule" "azure" {
  name             = "allow-azure"
  server_id        = azurerm_postgresql_flexible_server.pg.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

# Serverless containers (no VM quota; Azure Cache for Redis is retiring, so Redis
# runs as a sidecar container co-located with the app at localhost:6379).
resource "azurerm_container_app_environment" "env" {
  name                = "${var.prefix}-env"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
}

resource "azurerm_container_app" "app" {
  count                        = var.image == "" ? 0 : 1
  name                         = "${var.prefix}-app"
  resource_group_name          = azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  revision_mode                = "Single"

  registry {
    server               = azurerm_container_registry.acr.login_server
    username             = azurerm_container_registry.acr.admin_username
    password_secret_name = "acr-pwd"
  }
  secret {
    name  = "acr-pwd"
    value = azurerm_container_registry.acr.admin_password
  }
  secret {
    name  = "db-url"
    value = "postgresql://ttt:${random_password.db.result}@${azurerm_postgresql_flexible_server.pg.fqdn}:5432/ttt?sslmode=require"
  }

  template {
    min_replicas = 1
    max_replicas = 1
    container {
      name   = "redis"
      image  = "docker.io/library/redis:7-alpine"
      cpu    = 0.25
      memory = "0.5Gi"
    }
    container {
      name   = "app"
      image  = "${azurerm_container_registry.acr.login_server}/${var.image}"
      cpu    = 0.75
      memory = "1.5Gi"
      env {
        name        = "DATABASE_URL"
        secret_name = "db-url"
      }
      env {
        name  = "REDIS_HOST"
        value = "localhost"
      }
      env {
        name  = "REDIS_PORT"
        value = "6379"
      }
      env {
        name  = "REDIS_SSL"
        value = "false"
      }
    }
  }
  ingress {
    external_enabled = true
    target_port      = 8080
    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }
}

output "acr_login_server" { value = azurerm_container_registry.acr.login_server }
output "url" {
  value = var.image == "" ? "" : "https://${azurerm_container_app.app[0].ingress[0].fqdn}"
}
