"""Environment variable loading from .env files."""
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_ENV = os.path.join(_SCRIPT_DIR, "..", "..", ".env")


def load_env(path=None):
    """Parse a .env file and set os.environ. Skips comments and blank lines."""
    path = path or _DEFAULT_ENV
    if not os.path.isfile(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                val = val.strip().strip("'\"")
                os.environ.setdefault(key.strip(), val)


def require(var_name):
    """Get env var or exit with error."""
    val = os.environ.get(var_name, "").strip()
    if not val:
        print(f"Error: {var_name} not set. Check .env or export it.", file=sys.stderr)
        sys.exit(1)
    return val


def get(var_name, default=""):
    """Get env var with fallback."""
    return os.environ.get(var_name, default).strip() or default
