include "root" {
  path = find_in_parent_folders()
}

terraform {
  source = "../../../../modules/aws/resource/ec2"
}

dependency "security_group" {
  config_path = "../security_group"
}

locals {
  env = read_terragrunt_config(find_in_parent_folders("env.hcl"))
  upstream_modules = {
    security_group = "security_group"
  }
}

inputs = {
  environment   = local.env.locals.environment
  subnet_id     = "subnet-0123456789"
  ami_id        = "ami-0abcdef123456"
  instance_type = "t3.large"

  # upstream wiring via remote-state cross-stack lookup
  security_group_ids = [
    module.remote_state.components[var.upstream_modules.security_group]["security_group_id"],
  ]

  # also expressible via the terragrunt dependency output
  fallback_sg = dependency.security_group.outputs.security_group_id
}
