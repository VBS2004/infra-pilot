variable "environment" {
  type = string
}

variable "description" {
  type    = string
  default = "Managed by terraform"
}

variable "vpc_id" {
  type = string
}

variable "ingress_rules" {
  type    = list(map(string))
  default = []
}
