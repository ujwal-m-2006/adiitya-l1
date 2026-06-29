
#!/usr/bin/env python3
import sys
from pathlib import Path

# Add the project root to the path
sys.path.insert(0, str(Path(__file__).parent))

from dashboard.app import app

if __name__ == "__main__":
    app.run()
