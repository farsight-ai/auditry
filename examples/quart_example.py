"""
Example Quart application with auditry observability middleware.

This example demonstrates:
- Basic middleware setup
- Business event configuration
- User authentication integration
- Custom redaction patterns
"""

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel
from quart import Quart, abort, jsonify, request
from quart_schema import QuartSchema, validate_request, validate_response

# Import auditry components
from auditry import (
    BusinessEventConfig,
    ObservabilityConfig,
    configure_logging,
    get_logger,
)
from auditry.quart import create_middleware

# Configure structured logging
configure_logging(level="INFO")
logger = get_logger(__name__)

# Create Quart app
app = Quart(__name__)
QuartSchema(app)

# Configure observability
config = ObservabilityConfig(
    service_name="example-quart-api",
    correlation_id_header="X-Correlation-ID",
    payload_size_limit=50000,  # 50KB limit
    log_request_headers=True,
    log_response_headers=False,
    log_query_params=True,
    # Custom redaction patterns
    additional_redaction_patterns=["email", "phone", "ssn"],
    # Business event configuration
    business_events={
        "POST /api/users": BusinessEventConfig(
            event_type="user.created",
            extract_from_request=["name", "email"],
            extract_from_response=["id", "created_at"],
        ),
        "GET /api/users/{user_id}": BusinessEventConfig(
            event_type="user.retrieved",
            extract_from_path=["user_id"],
        ),
        "PUT /api/users/{user_id}": BusinessEventConfig(
            event_type="user.updated",
            extract_from_path=["user_id"],
            extract_from_request=["name", "email"],
        ),
        "DELETE /api/users/{user_id}": BusinessEventConfig(
            event_type="user.deleted",
            extract_from_path=["user_id"],
        ),
    },
)

# Apply observability middleware
app = create_middleware(app, config)


# Models
class UserCreate(BaseModel):
    name: str
    email: str
    password: str  # Will be redacted in logs
    phone: Optional[str] = None  # Will be redacted due to custom pattern
    ssn: Optional[str] = None  # Will be redacted due to custom pattern


class UserResponse(BaseModel):
    id: str
    name: str
    email: str
    created_at: str


@dataclass
class AuthenticatedUser:
    """User model for authenticated requests."""

    id: str
    email: str
    role: str = "user"


# Auth middleware
@app.before_request
async def authenticate():
    """Simple authentication middleware."""
    # Skip auth for health check
    if request.path == "/health":
        return

    # Check for authorization header
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        abort(401, "Authorization required")

    token = auth_header.split(" ")[1]

    # In a real app, you would validate the JWT token
    if token != "valid-token":
        abort(401, "Invalid token")

    # Set authenticated user on request
    # This will be picked up by the observability middleware
    request.current_user = AuthenticatedUser(
        id="user-123",
        email="user@example.com",
        role="admin",
    )


# Routes
@app.route("/health", methods=["GET"])
async def health_check():
    """Health check endpoint."""
    return jsonify({"status": "healthy", "service": "example-quart-api"})


@app.route("/api/users", methods=["POST"])
@validate_request(UserCreate)
@validate_response(UserResponse)
async def create_user(data: UserCreate):
    """Create a new user (password will be redacted in logs)."""
    logger.info("Creating new user", user_name=data.name)

    # Simulate user creation
    return UserResponse(
        id="user-456",
        name=data.name,
        email=data.email,
        created_at="2024-01-01T00:00:00Z",
    )


@app.route("/api/users/<user_id>", methods=["GET"])
@validate_response(UserResponse)
async def get_user(user_id: str):
    """Get user by ID."""
    logger.info("Retrieving user", user_id=user_id)

    # Simulate user retrieval
    if user_id == "404":
        abort(404, "User not found")

    return UserResponse(
        id=user_id,
        name="John Doe",
        email="john@example.com",
        created_at="2024-01-01T00:00:00Z",
    )


@app.route("/api/users/<user_id>", methods=["PUT"])
@validate_request(UserCreate)
@validate_response(UserResponse)
async def update_user(user_id: str, data: UserCreate):
    """Update user information."""
    logger.info("Updating user", user_id=user_id)

    # Simulate user update
    return UserResponse(
        id=user_id,
        name=data.name,
        email=data.email,
        created_at="2024-01-01T00:00:00Z",
    )


@app.route("/api/users/<user_id>", methods=["DELETE"])
async def delete_user(user_id: str):
    """Delete a user."""
    logger.info("Deleting user", user_id=user_id)

    # Simulate user deletion
    return jsonify({"message": f"User {user_id} deleted successfully"})


@app.route("/api/error", methods=["GET"])
async def trigger_error():
    """Endpoint that triggers an error for testing."""
    raise ValueError("This is a test error")


@app.route("/api/search", methods=["GET"])
async def search_users():
    """Search users with query parameters (will be logged)."""
    query = request.args.get("q", "")
    limit = request.args.get("limit", "10")

    logger.info("Searching users", query=query, limit=limit)

    return jsonify(
        {
            "results": [],
            "query": query,
            "limit": limit,
            "total": 0,
        }
    )


if __name__ == "__main__":
    # Run the application
    app.run(host="0.0.0.0", port=8000, debug=True)
