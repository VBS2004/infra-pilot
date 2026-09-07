variable "name" {
  type = string
  description = "centraltools name"
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}
