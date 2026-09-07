include "root" {
  path = find_in_parent_folders()
}

terraform {
  source = "../../../../modules/aws/resource/security_group"
}

locals {
  env = read_terragrunt_config(find_in_parent_folders("env.hcl"))
}

inputs = {
  environment = local.env.locals.environment
  vpc_id      = local.env.locals.vpc_id
  description = "payments nonprod app SG"
  ingress_rules = [
    {
      description = "allow_bastion_ssh_ingress"
      from_port   = 22
      to_port     = 22
      cidr_blocks = "10.0.0.0/16"
    },
  ]
}
