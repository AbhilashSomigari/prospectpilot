# Nightly self-improvement round: EventBridge Scheduler → ECS RunTask (prospectpilot improve).
resource "aws_scheduler_schedule" "improve" {
  name                         = "${local.name}-nightly-improve"
  schedule_expression          = var.improve_schedule
  schedule_expression_timezone = "UTC"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.scheduler.arn
    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.improve.arn_without_revision
      launch_type         = "FARGATE"
      task_count          = 1
      network_configuration {
        subnets          = aws_subnet.public[*].id
        security_groups  = [aws_security_group.tasks.id]
        assign_public_ip = true
      }
    }
    retry_policy {
      maximum_retry_attempts = 1
    }
  }
}
