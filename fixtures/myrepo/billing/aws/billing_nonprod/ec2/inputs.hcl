inputs = {
  ami_id        = "ami-0c55b159cbfafe1f0"
  instance_type = "t3.large"
  subnet_id     = "subnet-0billing001"
  vpc_id        = "vpc-0billing001"
  security_group_ids = ["sg-0billing001"]
  enable_monitoring  = true
  workload_subnets = [
    {
      name              = "app-1a"
      cidr              = "10.170.0.0/22"
      availability_zone = "ap-south-1a"
    },
  ]
}
