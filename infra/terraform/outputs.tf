output "api_url" {
  value = "http://${aws_lb.api.dns_name}"
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "db_endpoint" {
  value = aws_db_instance.main.address
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "improve_schedule" {
  value = aws_scheduler_schedule.improve.schedule_expression
}
