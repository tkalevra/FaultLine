"""FaultLine — thin Python client for the FaultLine v1 HTTP API.

    from faultline import Client
    fl = Client(api_key="flk_…", base_url="https://your-host", user_id="<seat-uuid>")
    fl.remember("My sister Diane is a nurse in Ottawa.")
    print(fl.recall("what does my sister do?").memory)
"""

from .client import Client
from .errors import (
    AuthError,
    BadRequestError,
    FaultLineError,
    ForbiddenError,
    ProvisioningError,
    QuotaExceededError,
    RateLimitedError,
    ReadOnlyError,
)
from .models import ForgetResult, LearnResult, RecallResult, WriteResult

__all__ = [
    "Client",
    "FaultLineError",
    "AuthError",
    "ForbiddenError",
    "BadRequestError",
    "QuotaExceededError",
    "ReadOnlyError",
    "RateLimitedError",
    "ProvisioningError",
    "RecallResult",
    "WriteResult",
    "ForgetResult",
    "LearnResult",
]

__version__ = "0.1.0"
