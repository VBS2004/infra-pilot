resource "random_string" "name_suffix" {
  length  = 6
  special = false
  upper   = false
}

resource "aws_security_group" "this" {
  name        = "payments-${var.environment}-sg-${random_string.name_suffix.result}"
  description = var.description
  vpc_id      = var.vpc_id

  dynamic "ingress" {
    for_each = var.ingress_rules
    content {
      description = ingress.value["description"]
      from_port   = ingress.value["from_port"]
      to_port     = ingress.value["to_port"]
      protocol    = "tcp"
      cidr_blocks = ingress.value["cidr_blocks"]
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.default_tags, {
    Name = "payments-${var.environment}-sg"
  })
}

locals {
  default_tags = {
    Project     = "payments"
    Environment = var.environment
  }
}
