variable "name" {
  type = string
  description = "athena cluster name"
}

variable "subnet_ids" {
  type = list(string)
}

variable "vpc_id" {
  type = string
}
