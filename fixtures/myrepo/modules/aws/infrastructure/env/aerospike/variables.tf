variable "name" {
  type = string
  description = "aerospike cluster name"
}

variable "subnet_ids" {
  type = list(string)
}

variable "vpc_id" {
  type = string
}
