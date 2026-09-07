variable "name" {
  type = string
  description = "common name"
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}
