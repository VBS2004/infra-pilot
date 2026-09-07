inputs = {
  cluster_name    = "auth-nonprod"
  cluster_version = "1.29"
  subnet_ids      = ["subnet-0auth001", "subnet-0auth002"]
  enable_irsa     = true
}
