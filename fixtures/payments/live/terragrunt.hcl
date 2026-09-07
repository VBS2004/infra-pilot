remote_state {
  backend = "s3"
  config = {
    bucket = "payments-tfstate"
    key    = "${path_relative_to_include()}/terraform.tfstate"
    region = "ap-south-1"
  }
}

generate "provider" {
  path      = "provider.tf"
  if_exists = "overwrite"
  contents  = <<EOF
provider "aws" {
  region = "ap-south-1"
}
EOF
}
