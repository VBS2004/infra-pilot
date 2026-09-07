variable "name" {
  type = string
  description = "auroramysql cluster name"
}

variable "subnet_ids" {
  type = list(string)
}

variable "vpc_id" {
  type = string
}
