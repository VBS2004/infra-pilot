variable "name" {
  type = string
  description = "bastion name"
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}
