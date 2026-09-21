"""
Simple example demonstrating auditry usage with FastAPI.

This example shows the unified ObservabilityMiddleware with:
- structlog for structured logging
- Automatic correlation ID tracking
- Request/response logging and redaction
- Service name in all logs

Run with: uvicorn examples.simple_app:app --reload
"""

from fastapi import FastAPI, Request

from auditry import (
    BusinessEventConfig,
    ObservabilityConfig,
    ObservabilityMiddleware,
    configure_logging,
    get_correlation_id,
    get_logger,
)

# Configure structured logging at startup
configure_logging(level="INFO")

# Create logger (will automatically include correlation IDs)
logger = get_logger(__name__)

# Create FastAPI app
app = FastAPI(title="Simple Observability Example")

# Add the unified observability middleware (handles both correlation IDs and logging)
app.add_middleware(
    ObservabilityMiddleware,
    config=ObservabilityConfig(
        service_name="example-api",  # REQUIRED
        correlation_id_header="X-Correlation-ID",  # Optional: customize header name
        log_request_headers=True,
        log_response_headers=False,
        payload_size_limit=10_240,  # 10KB
        # Tag business events for analytics (optional)
        business_events={
            "POST /folders": BusinessEventConfig(
                event_type="folder.created",
                extract_from_request=["name", "parent_id"],
            ),
            "DELETE /folders/{folder_id}": BusinessEventConfig(
                event_type="folder.deleted",
                extract_from_path=["folder_id"],
            ),
        },
    ),
)


@app.get("/")
async def root():
    """Simple endpoint that returns a greeting."""
    logger.info("Root endpoint called")
    return {"message": "Hello World", "correlation_id": get_correlation_id()}


@app.get("/users/{user_id}")
async def get_user(user_id: str, request: Request):
    """Example endpoint with user authentication simulation."""
    # Simulate setting user_id from authentication
    # In real code, this would come from your auth dependency
    request.state.user_id = f"user_{user_id}"

    logger.info(f"Fetching user {user_id}")

    return {
        "user_id": user_id,
        "name": "John Doe",
        "email": "john@example.com",
        "correlation_id": get_correlation_id(),
    }


@app.post("/sensitive")
async def post_sensitive(data: dict):
    """Example endpoint that handles sensitive data (will be redacted in logs)."""
    logger.info("Processing sensitive data")

    # These fields will be automatically redacted in logs:
    # password, api_key, token, secret, authorization, ssn, credit_card, etc.

    return {"status": "processed", "correlation_id": get_correlation_id()}


@app.get("/error")
async def error_endpoint():
    """Example endpoint that raises an error (will be logged with full context)."""
    logger.warning("About to raise an error")
    raise ValueError("This is a test error")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
