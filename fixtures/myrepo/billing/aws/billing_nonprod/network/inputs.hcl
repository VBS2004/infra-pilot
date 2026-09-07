inputs = {
  vpc_cidr             = "10.170.0.0/16"
  enable_dns_hostnames = true
  workload_subnets = [
    {
      name              = "app-1a"
      cidr              = "10.170.0.0/22"
      availability_zone = "ap-south-1a"
    },
  ]
}
