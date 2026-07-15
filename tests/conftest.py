"""Make the app and the speedhive library importable for host test runs.

In the Docker image speedhive-tools is pip-installed from the submodule; on a
dev host without that install, fall back to the submodule source tree.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import speedhive  # noqa: F401
except ImportError:
    sys.path.insert(0, str(ROOT / "speedhive-tools" / "src"))
