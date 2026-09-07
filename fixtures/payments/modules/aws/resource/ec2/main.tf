resource "random_string" "name_suffix" {
  length  = 6
  special = false
  upper   = false
}

resource "aws_instance" "this" {
  ami                    = var.ami_id
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = var.security_group_ids
  key_name               = var.key_name

  metadata_options {
    http_tokens = "required"
  }

  dynamic "ebs_block_device" {
    for_each = var.extra_volumes
    content {
      device_name = ebs_block_device.value["device_name"]
      volume_size = ebs_block_device.value["size"]
      encrypted   = true
    }
  }

  tags = merge(local.default_tags, {
    Name = "payments-${var.environment}-ec2-${random_string.name_suffix.result}"
  })
}

locals {
  default_tags = {
    Project     = "payments"
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}
