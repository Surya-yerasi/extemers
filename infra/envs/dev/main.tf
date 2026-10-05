module "calculator" {
  source = "../../modules/lambda_http_api"

  name         = "calculator-dev"
  package_path = var.package_path
  handler      = "calculator.handler.lambda_handler"
  routes       = ["POST /calculate", "GET /health"]

  environment_variables = {
    APP_ENVIRONMENT         = "dev"
    POWERTOOLS_SERVICE_NAME = "calculator"
    POWERTOOLS_LOG_LEVEL    = "INFO"
  }
}
