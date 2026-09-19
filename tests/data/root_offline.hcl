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
    terraform {
      required_providers {
        aws = { source = "hashicorp/aws", version = "~> 5.0" }
      }
    }
    provider "aws" {
      region                      = "${local.env_cfg.region}"
      access_key                  = "dummy"
      secret_key                  = "dummy"
      skip_credentials_validation = true
      skip_requesting_account_id  = true
      skip_metadata_api_check     = true
    }
  EOF
}

generate "backend" {
  path      = "backend.tf"
  if_exists = "overwrite"
  contents  = "terraform {\n  backend \"local\" {}\n}\n"
}

inputs = read_terragrunt_config("${get_terragrunt_dir()}/inputs.hcl").inputs
