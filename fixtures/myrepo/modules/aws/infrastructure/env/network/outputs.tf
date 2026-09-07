output "vpc_id"     { value = aws_vpc.this.id }
output "subnet_ids" { value = [for s in aws_subnet.workload : s.id] }
