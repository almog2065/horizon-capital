# Dev environment overrides. Apply with:
#   terraform workspace select dev || terraform workspace new dev
#   terraform apply -var-file=envs/dev.tfvars

aws_region           = "us-east-1"
owner                = "platform"
db_instance_class    = "db.t4g.micro"
db_storage_gb        = 20
db_multi_az          = false
redis_node_type      = "cache.t4g.micro"
web_desired_count    = 1
worker_desired_count = 1
app_image_tag        = "latest"
