variable "vpc_cidr" {
  type = string
}

variable "enable_dns_hostnames" {
  type = bool
  default = true
}

variable "workload_subnets" {
  type = list(object({
    name              = string
    cidr              = string
    availability_zone = string
  }))
  default = []
}
