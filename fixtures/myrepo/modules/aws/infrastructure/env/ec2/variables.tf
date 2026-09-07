variable "instance_type" {
  type = string
  default = "t3.micro"
}

variable "ami_id" {
  type = string
}

variable "subnet_id" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "security_group_ids" {
  type = list(string)
  default = []
}

variable "workload_subnets" {
  type = list(object({
    name              = string
    cidr              = string
    availability_zone = string
  }))
  default = []
}

variable "key_name" {
  type = string
  default = ""
}

variable "enable_monitoring" {
  type = bool
  default = false
}
