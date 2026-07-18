"""ทำให้ `import pipeline...` ใช้ได้ในเทสต์ โดยไม่ต้อง pip install -e ."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
