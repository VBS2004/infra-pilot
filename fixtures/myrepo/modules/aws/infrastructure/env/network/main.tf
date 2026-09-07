resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = var.enable_dns_hostnames
  tags = { Name = "${terraform.workspace}-vpc" }
}

resource "aws_subnet" "workload" {
  for_each = { for s in var.workload_subnets : s.name => s }
  vpc_id            = aws_vpc.this.id
  cidr_block        = each.value.cidr
  availability_zone = each.value.availability_zone
  tags = { Name = each.value.name }
}
