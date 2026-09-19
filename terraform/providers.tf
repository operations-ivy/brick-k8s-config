terraform {
  required_version = ">= 1.5"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.31"
    }
  }
}

variable "kubeconfig_path" {
  description = "Path to the kubeconfig for the chucks-wisdom k3s cluster"
  type        = string
  default     = "~/.kube/chuck-config"
}

provider "kubernetes" {
  config_path = var.kubeconfig_path
}
