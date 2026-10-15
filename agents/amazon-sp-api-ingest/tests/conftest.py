import sys
from pathlib import Path

# The scripts live one folder up and are not a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
