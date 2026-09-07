resource "aws_instance" "this" {
  ami           = var.ami_id
  instance_type = var.instance_type
  subnet_id     = var.subnet_id
  vpc_security_group_ids = var.security_group_ids
  monitoring = var.enable_monitoring
  tags = { Name = "${terraform.workspace}-ec2" }
}

resource "aws_subnet" "workload" {
  for_each = { for s in var.workload_subnets : s.name => s }
  vpc_id            = var.vpc_id
  cidr_block        = each.value.cidr
  availability_zone = each.value.availability_zone
  tags = { Name = each.value.name }
}
