"""
Example FastAPI application with auditry observability middleware.

This example demonstrates:
- Basic middleware setup
- Business event configuration
- User authentication integration
- Custom redaction patterns
"""

from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

# Import auditry components
from auditry import (
    BusinessEventConfig,
    ObservabilityConfig,
    configure_logging,
    get_logger,
)
from auditry.fastapi import create_middleware

# Configure structured logging
configure_logging(level="INFO")
logger = get_logger(__name__)

# Create FastAPI app
app = FastAPI(title="Example API", version="1.0.0")

# Configure observability
config = ObservabilityConfig(
    service_name="example-api",
    correlation_id_header="X-Request-ID",
    payload_size_limit=50000,  # 50KB limit
    log_request_headers=True,
    log_response_headers=False,
    log_query_params=True,
    # Custom redaction patterns
    additional_redaction_patterns=["email", "phone"],
    # Business event configuration
    business_events={
        "POST /users": BusinessEventConfig(
            event_type="user.created",
            extract_from_request=["name", "email"],
            extract_from_response=["id", "created_at"],
        ),
        "GET /users/{user_id}": BusinessEventConfig(
            event_type="user.retrieved",
            extract_from_path=["user_id"],
        ),
        "PUT /users/{user_id}": BusinessEventConfig(
            event_type="user.updated",
            extract_from_path=["user_id"],
            extract_from_request=["name", "email"],
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


class UserResponse(BaseModel):
    id: str
    name: str
    email: str
    created_at: str


# Simple auth dependency
security = HTTPBearer()


async def get_current_user(
    request: Request,
    # Depends() has to be evaluated in the default: that is how FastAPI
    # resolves the dependency per request, so B008 does not apply here.
    credentials: HTTPAuthorizationCredentials = Depends(security),  # noqa: B008
) -> str:
    """Extract user from token and set in request state."""
    # In a real app, you would validate the token
    if credentials.credentials != "valid-token":
        raise HTTPException(status_code=401, detail="Invalid token")

    # Set user_id in request state for logging
    request.state.user_id = "user-123"
    return "user-123"


# Routes
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "example-api"}


@app.post("/users", response_model=UserResponse)
async def create_user(
    user: UserCreate,
    current_user: str = Depends(get_current_user),
):
    """Create a new user (password will be redacted in logs)."""
    logger.info("Creating new user", user_name=user.name)

    # Simulate user creation
    return UserResponse(
        id="user-456",
        name=user.name,
        email=user.email,
        created_at="2024-01-01T00:00:00Z",
    )


@app.get("/users/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: str,
    current_user: str = Depends(get_current_user),
):
    """Get user by ID."""
    logger.info("Retrieving user", user_id=user_id)

    # Simulate user retrieval
    return UserResponse(
        id=user_id,
        name="John Doe",
        email="john@example.com",
        created_at="2024-01-01T00:00:00Z",
    )


@app.put("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    user: UserCreate,
    current_user: str = Depends(get_current_user),
):
    """Update user information."""
    logger.info("Updating user", user_id=user_id)

    # Simulate user update
    return UserResponse(
        id=user_id,
        name=user.name,
        email=user.email,
        created_at="2024-01-01T00:00:00Z",
    )


@app.get("/error")
async def trigger_error():
    """Endpoint that triggers an error for testing."""
    raise ValueError("This is a test error")


if __name__ == "__main__":
    import uvicorn

    # Run the application
    uvicorn.run(app, host="0.0.0.0", port=8000)
