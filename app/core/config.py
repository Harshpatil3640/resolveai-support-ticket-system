
import os
from pathlib import Path

from dotenv import load_dotenv


# Locate the project root directory.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Load environment variables from the project root.
load_dotenv(PROJECT_ROOT / ".env")


# Load the JWT secret.
SECRET_KEY = os.getenv("SECRET_KEY")

if not SECRET_KEY or len(SECRET_KEY) < 32:
    raise RuntimeError(
        "SECRET_KEY is missing or too short. "
        "Configure it in the project .env file."
    )


# JWT configuration.
ALGORITHM = "HS256"

try:
    ACCESS_TOKEN_EXPIRE_MINUTES = int(
        os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30")
    )
except ValueError as exc:
    raise RuntimeError(
        "ACCESS_TOKEN_EXPIRE_MINUTES must be an integer."
    ) from exc

if ACCESS_TOKEN_EXPIRE_MINUTES <= 0:
    raise RuntimeError(
        "ACCESS_TOKEN_EXPIRE_MINUTES must be positive."
    )
