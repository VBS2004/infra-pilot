output "instance_id" {
  value = aws_instance.this.id
}

output "private_ip" {
  value = aws_instance.this.private_ip
}

output "name_suffix" {
  value = random_string.name_suffix.result
}
