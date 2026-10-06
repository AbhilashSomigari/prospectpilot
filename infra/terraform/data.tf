resource "aws_ecr_repository" "app" {
  name                 = var.project
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 20 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }
      action       = { type = "expire" }
    }]
  })
}

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

# Postgres 16; pgvector is available on RDS and enabled by the first Alembic migration.
resource "aws_db_instance" "main" {
  identifier                   = local.name
  engine                       = "postgres"
  engine_version               = "16"
  instance_class               = var.db_instance_class
  allocated_storage            = var.db_allocated_storage
  storage_type                 = "gp3"
  storage_encrypted            = true
  db_name                      = "prospectpilot"
  username                     = "prospectpilot"
  password                     = random_password.db.result
  db_subnet_group_name         = aws_db_subnet_group.main.name
  vpc_security_group_ids       = [aws_security_group.db.id]
  publicly_accessible          = false
  backup_retention_period      = 7
  deletion_protection          = var.environment == "prod"
  skip_final_snapshot          = var.environment != "prod"
  final_snapshot_identifier    = var.environment == "prod" ? "${local.name}-final" : null
  performance_insights_enabled = true
}

resource "aws_secretsmanager_secret" "database_url" {
  name = "${local.name}/DATABASE_URL"
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = "postgresql+psycopg://prospectpilot:${random_password.db.result}@${aws_db_instance.main.address}:5432/prospectpilot"
}

# Values for these are set out-of-band (console/CLI), never in Terraform state or git.
resource "aws_secretsmanager_secret" "anthropic_api_key" {
  name = "${local.name}/ANTHROPIC_API_KEY"
}

resource "aws_secretsmanager_secret" "api_key" {
  name = "${local.name}/API_KEY"
}
