variable "ami_id" {
  type        = string
  description = "AMI to launch."
}

variable "instance_type" {
  type    = string
  default = "t3.medium"
}

variable "subnet_id" {
  type = string
}

variable "security_group_ids" {
  type    = list(string)
  default = []
}

variable "key_name" {
  type    = string
  default = null
}

variable "environment" {
  type = string
}

variable "extra_volumes" {
  type    = list(map(string))
  default = []
}
