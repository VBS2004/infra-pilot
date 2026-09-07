resource "aws_eks_cluster" "this" {
  name    = var.cluster_name
  version = var.cluster_version
  vpc_config { subnet_ids = var.subnet_ids }
}
