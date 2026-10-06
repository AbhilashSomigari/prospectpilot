variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "prospectpilot"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "image_tag" {
  description = "Tag of the prospectpilot image in ECR"
  type        = string
  default     = "latest"
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_allocated_storage" {
  type    = number
  default = 20
}

variable "api_desired_count" {
  type    = number
  default = 1
}

variable "worker_desired_count" {
  type    = number
  default = 1
}

variable "llm_provider" {
  description = "anthropic | openai | ollama | mock"
  type        = string
  default     = "anthropic"
}

variable "improve_schedule" {
  description = "EventBridge Scheduler expression for the nightly self-improvement round"
  type        = string
  default     = "cron(0 3 * * ? *)"
}

variable "improve_trials" {
  type    = number
  default = 2
}

variable "allowed_api_cidrs" {
  description = "CIDRs allowed to reach the API load balancer"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}
