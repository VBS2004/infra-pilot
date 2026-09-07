locals {
  env_cfg = yamldecode(file(find_in_parent_folders("config.yml")))
}

terraform {
  source = "${get_parent_terragrunt_dir()}/modules//aws/infrastructure/${local.env_cfg.terraform_module}/${basename(get_original_terragrunt_dir())}/"
}

generate "provider" {
  path      = "provider.tf"
  if_exists = "overwrite"
  contents  = <<-EOF
    provider "aws" {
      region = "${local.env_cfg.region}"
    }
  EOF
}

remote_state {
  backend = "s3"
  config = {
    bucket = "tf-state-${local.env_cfg.account_id}"
    key    = "${path_relative_to_include()}/terraform.tfstate"
    region = local.env_cfg.region
  }
}
