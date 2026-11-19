import subprocess
import sys

from scripts.run_ui import main as run_ui
from src.core.config import ROOT


def main() -> None:
    with subprocess.Popen([sys.executable, "-m", "src.api.main"], cwd=ROOT) as api:
        try:
            run_ui()
        finally:
            api.terminate()


if __name__ == "__main__":
    main()
