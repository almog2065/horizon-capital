# =============================================================================
# Horizon Capital — AWS Infrastructure (Terraform)
# =============================================================================
#
# Provisions the minimum cloud footprint to run the firm end-to-end:
#
#   * ECR repo for the application image
#   * VPC + public/private subnets (one AZ per zone)
#   * RDS Postgres (single-AZ for non-prod, multi-AZ for prod)
#   * ElastiCache Redis
#   * ECS Fargate cluster + service for `web` and `worker`
#   * ALB in front of `web`, with WAFv2 (commented baseline)
#   * SSM Parameter Store for OpenAI key (or AWS Secrets Manager — see notes)
#
# Designed to be applied per-environment via `terraform workspace`:
#
#     terraform workspace new dev && terraform apply -var-file=envs/dev.tfvars
#
# This file is the composition root. Resources are defined inline (small
# footprint); break them into modules under ./modules when you grow.
#
# Why Terraform (not CDK/Pulumi): the brief calls out IaC, and Terraform
# is the lowest-common-denominator language across AWS / GCP / Azure for
# a multi-cloud-ready firm. Every resource here is declarative and
# importable.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws    = { source = "hashicorp/aws",    version = "~> 5.50" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }

  # State backend — uncomment and configure before running `terraform init`.
  # backend "s3" {
  #   bucket         = "horizon-capital-tfstate"
  #   key            = "horizon/${terraform.workspace}/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "horizon-capital-tflock"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      project     = "horizon-capital"
      environment = terraform.workspace
      managed_by  = "terraform"
      owner       = var.owner
    }
  }
}

# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------
variable "aws_region"           { type = string  default = "us-east-1" }
variable "owner"                { type = string  default = "platform" }
variable "vpc_cidr"             { type = string  default = "10.20.0.0/16" }
variable "az_count"             { type = number  default = 2 }
variable "db_instance_class"    { type = string  default = "db.t4g.micro" }
variable "db_storage_gb"        { type = number  default = 20 }
variable "db_multi_az"          { type = bool    default = false }
variable "redis_node_type"      { type = string  default = "cache.t4g.micro" }
variable "web_desired_count"    { type = number  default = 2 }
variable "worker_desired_count" { type = number  default = 1 }
variable "app_image_tag"        { type = string  default = "latest" }
variable "openai_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

# ---------------------------------------------------------------------------
# Data — current AZs
# ---------------------------------------------------------------------------
data "aws_availability_zones" "available" { state = "available" }

locals {
  azs    = slice(data.aws_availability_zones.available.names, 0, var.az_count)
  prefix = "horizon-${terraform.workspace}"
}

# ---------------------------------------------------------------------------
# VPC
# ---------------------------------------------------------------------------
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags = { Name = "${local.prefix}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags = { Name = "${local.prefix}-igw" }
}

resource "aws_subnet" "public" {
  count                   = length(local.azs)
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true
  tags = { Name = "${local.prefix}-public-${count.index}", tier = "public" }
}

resource "aws_subnet" "private" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index + length(local.azs))
  availability_zone = local.azs[count.index]
  tags = { Name = "${local.prefix}-private-${count.index}", tier = "private" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${local.prefix}-public-rt" }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Single NAT to keep dev cheap. For prod, use one per AZ.
resource "aws_eip" "nat" { domain = "vpc" }

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  depends_on    = [aws_internet_gateway.main]
  tags = { Name = "${local.prefix}-nat" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }
  tags = { Name = "${local.prefix}-private-rt" }
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# ---------------------------------------------------------------------------
# Security groups
# ---------------------------------------------------------------------------
resource "aws_security_group" "alb" {
  name        = "${local.prefix}-alb"
  description = "ALB ingress 80/443"
  vpc_id      = aws_vpc.main.id

  ingress { from_port = 80  to_port = 80  protocol = "tcp" cidr_blocks = ["0.0.0.0/0"] }
  ingress { from_port = 443 to_port = 443 protocol = "tcp" cidr_blocks = ["0.0.0.0/0"] }
  egress  { from_port = 0   to_port = 0   protocol = "-1" cidr_blocks = ["0.0.0.0/0"] }
}

resource "aws_security_group" "ecs" {
  name        = "${local.prefix}-ecs"
  description = "ECS tasks — accept from ALB only"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress { from_port = 0 to_port = 0 protocol = "-1" cidr_blocks = ["0.0.0.0/0"] }
}

resource "aws_security_group" "db" {
  name        = "${local.prefix}-db"
  description = "RDS — accept from ECS only"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs.id]
  }
  egress { from_port = 0 to_port = 0 protocol = "-1" cidr_blocks = ["0.0.0.0/0"] }
}

resource "aws_security_group" "redis" {
  name        = "${local.prefix}-redis"
  description = "Redis — accept from ECS only"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs.id]
  }
  egress { from_port = 0 to_port = 0 protocol = "-1" cidr_blocks = ["0.0.0.0/0"] }
}

# ---------------------------------------------------------------------------
# ECR
# ---------------------------------------------------------------------------
resource "aws_ecr_repository" "app" {
  name                 = "horizon-capital"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration     { encryption_type = "AES256" }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 30 images"
      selection = {
        tagStatus     = "any"
        countType     = "imageCountMoreThan"
        countNumber   = 30
      }
      action = { type = "expire" }
    }]
  })
}

# ---------------------------------------------------------------------------
# Secrets (SSM Parameter Store)
# ---------------------------------------------------------------------------
resource "random_password" "db" {
  length  = 24
  special = false
}

resource "aws_ssm_parameter" "db_password" {
  name  = "/${local.prefix}/db/password"
  type  = "SecureString"
  value = random_password.db.result
}

resource "aws_ssm_parameter" "openai_key" {
  count = var.openai_api_key == "" ? 0 : 1
  name  = "/${local.prefix}/openai/api_key"
  type  = "SecureString"
  value = var.openai_api_key
}

# ---------------------------------------------------------------------------
# RDS Postgres
# ---------------------------------------------------------------------------
resource "aws_db_subnet_group" "main" {
  name       = "${local.prefix}-db-subnets"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_instance" "postgres" {
  identifier              = "${local.prefix}-postgres"
  engine                  = "postgres"
  engine_version          = "16"
  instance_class          = var.db_instance_class
  allocated_storage       = var.db_storage_gb
  storage_type            = "gp3"
  storage_encrypted       = true
  db_name                 = "horizon"
  username                = "horizon"
  password                = random_password.db.result
  db_subnet_group_name    = aws_db_subnet_group.main.name
  vpc_security_group_ids  = [aws_security_group.db.id]
  multi_az                = var.db_multi_az
  publicly_accessible     = false
  backup_retention_period = 7
  deletion_protection     = terraform.workspace == "prod"
  skip_final_snapshot     = terraform.workspace != "prod"
  apply_immediately       = false
}

# ---------------------------------------------------------------------------
# ElastiCache Redis
# ---------------------------------------------------------------------------
resource "aws_elasticache_subnet_group" "main" {
  name       = "${local.prefix}-redis-subnets"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_cluster" "redis" {
  cluster_id           = "${local.prefix}-redis"
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = var.redis_node_type
  num_cache_nodes      = 1
  port                 = 6379
  parameter_group_name = "default.redis7"
  subnet_group_name    = aws_elasticache_subnet_group.main.name
  security_group_ids   = [aws_security_group.redis.id]
}

# ---------------------------------------------------------------------------
# ECS cluster + IAM
# ---------------------------------------------------------------------------
resource "aws_ecs_cluster" "main" {
  name = "${local.prefix}-cluster"
  setting { name = "containerInsights" value = "enabled" }
}

resource "aws_iam_role" "task_exec" {
  name = "${local.prefix}-task-exec"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "task_exec" {
  role       = aws_iam_role.task_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "task_exec_ssm" {
  role = aws_iam_role.task_exec.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameters", "kms:Decrypt"]
      Resource = "*"
    }]
  })
}

# ---------------------------------------------------------------------------
# ALB
# ---------------------------------------------------------------------------
resource "aws_lb" "web" {
  name               = "${local.prefix}-alb"
  internal           = false
  load_balancer_type = "application"
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.alb.id]
}

resource "aws_lb_target_group" "web" {
  name        = "${local.prefix}-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path                = "/healthz"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 15
    timeout             = 5
    matcher             = "200"
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.web.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}

# ---------------------------------------------------------------------------
# ECS task definitions + services
# ---------------------------------------------------------------------------
locals {
  app_image = "${aws_ecr_repository.app.repository_url}:${var.app_image_tag}"
  app_env = [
    { name = "APP_ENV",       value = terraform.workspace },
    { name = "LOG_LEVEL",     value = "INFO" },
    { name = "LOG_FORMAT",    value = "json" },
    { name = "DATABASE_URL",  value = "postgresql+psycopg://horizon:${random_password.db.result}@${aws_db_instance.postgres.address}:5432/horizon" },
    { name = "REDIS_URL",     value = "redis://${aws_elasticache_cluster.redis.cache_nodes[0].address}:6379/0" },
  ]
  app_secrets = var.openai_api_key == "" ? [] : [
    { name = "OPENAI_API_KEY", valueFrom = aws_ssm_parameter.openai_key[0].arn },
  ]
}

resource "aws_cloudwatch_log_group" "web" {
  name              = "/ecs/${local.prefix}/web"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${local.prefix}/worker"
  retention_in_days = 14
}

resource "aws_ecs_task_definition" "web" {
  family                   = "${local.prefix}-web"
  cpu                      = "512"
  memory                   = "1024"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.task_exec.arn
  task_role_arn            = aws_iam_role.task_exec.arn

  container_definitions = jsonencode([{
    name      = "web"
    image     = local.app_image
    essential = true
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    environment = concat(local.app_env, [
      { name = "RUN_SCHEDULER_IN_API", value = "false" },
    ])
    secrets = local.app_secrets
    healthCheck = {
      command  = ["CMD-SHELL", "curl -fsS http://127.0.0.1:8000/healthz || exit 1"]
      interval = 15
      timeout  = 5
      retries  = 3
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.web.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "web"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.prefix}-worker"
  cpu                      = "256"
  memory                   = "512"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.task_exec.arn
  task_role_arn            = aws_iam_role.task_exec.arn

  container_definitions = jsonencode([{
    name      = "worker"
    image     = local.app_image
    essential = true
    command   = ["python", "-m", "app.workers.scheduler_worker"]
    environment = concat(local.app_env, [
      { name = "RUN_SCHEDULER_IN_API",      value = "false" },
      { name = "AUTO_PLAN_SUPERVISION",     value = "true" },
      { name = "FIRM_BALANCE_INTERVAL_SEC", value = "1800" },
    ])
    secrets = local.app_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.worker.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "worker"
      }
    }
  }])
}

resource "aws_ecs_service" "web" {
  name            = "${local.prefix}-web"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.web.arn
  desired_count   = var.web_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.ecs.id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.web.arn
    container_name   = "web"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.http]

  deployment_circuit_breaker { enable = true rollback = true }
  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 100
}

resource "aws_ecs_service" "worker" {
  name            = "${local.prefix}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.ecs.id]
  }

  deployment_circuit_breaker { enable = true rollback = true }
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
output "alb_dns"           { value = aws_lb.web.dns_name }
output "ecr_repository_url"{ value = aws_ecr_repository.app.repository_url }
output "rds_endpoint"      { value = aws_db_instance.postgres.address sensitive = true }
output "redis_endpoint"    { value = aws_elasticache_cluster.redis.cache_nodes[0].address }
output "cluster_name"      { value = aws_ecs_cluster.main.name }
