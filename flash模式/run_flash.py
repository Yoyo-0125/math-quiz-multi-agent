import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODES_DIR = PROJECT_ROOT / "codes"
if str(CODES_DIR) not in sys.path:
    sys.path.insert(0, str(CODES_DIR))

from run_pipeline import main


if __name__ == "__main__":
    main()
