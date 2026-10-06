resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "app" {
  for_each          = toset(["api", "worker", "improve", "mailpit"])
  name              = "/ecs/${local.name}/${each.key}"
  retention_in_days = 30
}

locals {
  image = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
  common_env = [
    { name = "ENV", value = var.environment },
    { name = "LLM_PROVIDER", value = var.llm_provider },
    { name = "EMBEDDING_PROVIDER", value = "hash" },
    # Sending stays sandboxed in the cloud too: each task runs a Mailpit sidecar on localhost.
    { name = "SMTP_HOST", value = "localhost" },
    { name = "SMTP_PORT", value = "1025" },
    { name = "ALLOW_REAL_SEND", value = "false" },
    { name = "OTEL_ENABLED", value = "false" },
  ]
  common_secrets = [
    { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
    { name = "ANTHROPIC_API_KEY", valueFrom = aws_secretsmanager_secret.anthropic_api_key.arn },
    { name = "API_KEY", valueFrom = aws_secretsmanager_secret.api_key.arn },
  ]
  mailpit = {
    name      = "mailpit"
    image     = "axllent/mailpit:v1.21"
    essential = false
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app["mailpit"].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "mailpit"
      }
    }
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions = jsonencode([
    {
      name         = "api"
      image        = local.image
      essential    = true
      command      = ["sh", "-c", "prospectpilot db upgrade && prospectpilot serve --host 0.0.0.0 --port 8000"]
      portMappings = [{ containerPort = 8000, protocol = "tcp" }]
      environment  = local.common_env
      secrets      = local.common_secrets
      healthCheck = {
        command  = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"]
        interval = 15
        retries  = 3
        timeout  = 5
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.app["api"].name
          awslogs-region        = var.region
          awslogs-stream-prefix = "api"
        }
      }
    },
    local.mailpit,
  ])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions = jsonencode([
    {
      name        = "worker"
      image       = local.image
      essential   = true
      command     = ["prospectpilot", "worker"]
      environment = local.common_env
      secrets     = local.common_secrets
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.app["worker"].name
          awslogs-region        = var.region
          awslogs-stream-prefix = "worker"
        }
      }
    },
    local.mailpit,
  ])
}

# One-shot task launched nightly by EventBridge Scheduler.
resource "aws_ecs_task_definition" "improve" {
  family                   = "${local.name}-improve"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions = jsonencode([
    {
      name        = "improve"
      image       = local.image
      essential   = true
      command     = ["prospectpilot", "improve", "--trials", tostring(var.improve_trials)]
      environment = local.common_env
      secrets     = local.common_secrets
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.app["improve"].name
          awslogs-region        = var.region
          awslogs-stream-prefix = "improve"
        }
      }
    },
  ])
}

resource "aws_lb" "api" {
  name               = substr(local.name, 0, 32)
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id
}

resource "aws_lb_target_group" "api" {
  name        = substr("${local.name}-api", 0, 32)
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id
  health_check {
    path    = "/healthz"
    matcher = "200"
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_ecs_service" "api" {
  name            = "api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }
  depends_on = [aws_lb_listener.http]
}

resource "aws_ecs_service" "worker" {
  name            = "worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true
  }
}
