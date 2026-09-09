"""Shared test configuration.

The integration lives under custom_components/, which is not an installed
package, so the repository root goes on the import path.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
