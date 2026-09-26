variable "aws_region" {
  description = "AWS region for the production stack."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "Prefix used for AWS resource names."
  type        = string
  default     = "ai-qa-api"
}

variable "environment" {
  description = "Deployment environment."
  type        = string
  default     = "prod"
}

variable "vpc_cidr" {
  description = "CIDR range for the VPC."
  type        = string
  default     = "10.40.0.0/16"
}

variable "availability_zones" {
  description = "At least two AZs are required for the production VPC."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b", "us-east-1c"]

  validation {
    condition     = length(var.availability_zones) >= 2
    error_message = "Provide at least two availability zones."
  }
}

variable "kubernetes_version" {
  description = "EKS Kubernetes version."
  type        = string
  default     = "1.31"
}

variable "container_image_tag" {
  description = "Tag to publish/use for the API image."
  type        = string
  default     = "0.8.0"
}

variable "node_instance_types" {
  description = "EC2 instance types for the EKS managed node group."
  type        = list(string)
  default     = [" t3.medium"]
}

variable "node_min_size" {
  type    = number
  default = 3
}

variable "node_desired_size" {
  type    = number
  default = 3
}

variable "node_max_size" {
  type    = number
  default = 10
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.medium"
}

variable "db_name" {
  type    = string
  default = "appdb"
}

variable "db_username" {
  type    = string
  default = "appuser"
}

variable "db_password" {
  description = "RDS password. Supply through tfvars or an environment variable; never commit it."
  type        = string
  sensitive   = true
}

variable "redis_node_type" {
  description = "ElastiCache node type."
  type        = string
  default     = "cache.t4g.micro"
}

variable "jwt_secret" {
  description = "JWT signing secret passed to the Kubernetes Secret."
  type        = string
  sensitive   = true
}

variable "llm_api_key" {
  description = "LLM provider API key passed to the Kubernetes Secret."
  type        = string
  sensitive   = true
}

variable "metrics_token" {
  description = "Token protecting the metrics endpoint."
  type        = string
  sensitive   = true
}

variable "tags" {
  description = "Tags applied to all AWS resources."
  type        = map(string)
  default = {
    Project   = "ai-qa-api"
    ManagedBy = "terraform"
  }
}
