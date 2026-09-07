inputs = {
  ami_id        = "ami-0c55b159cbfafe1f0"
  instance_type = "t3.medium"
  subnet_id     = "subnet-0auth001"
  vpc_id        = "vpc-0auth001"
  security_group_ids = ["sg-0auth001"]
  enable_monitoring  = false
  workload_subnets = [
    {
      name              = "app-1a"
      cidr              = "10.160.0.0/22"
      availability_zone = "ap-south-1a"
    },
  ]
}
