# Production overrides. Apply with:
#   terraform workspace select prod || terraform workspace new prod
#   terraform apply -var-file=envs/prod.tfvars

aws_region           = "us-east-1"
owner                = "platform"
db_instance_class    = "db.t4g.small"
db_storage_gb        = 100
db_multi_az          = true
redis_node_type      = "cache.t4g.small"
web_desired_count    = 3
worker_desired_count = 2
app_image_tag        = "latest"  # override at apply time via CI -var
