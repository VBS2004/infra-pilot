variable "cluster_name"    { type = string }
variable "cluster_version" { type = string
  default = "1.29" }
variable "subnet_ids"      { type = list(string) }
variable "enable_irsa"     { type = bool
  default = true }
