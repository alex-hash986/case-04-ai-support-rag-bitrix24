import subprocess
import sys

from src.core.config import ROOT, Settings


def main() -> None:
    subprocess.run([
        sys.executable, "-m", "streamlit", "run", str(ROOT / "src/ui/app.py"),
        "--server.address=127.0.0.1", f"--server.port={Settings().ui_port}",
        "--server.headless=true", "--browser.gatherUsageStats=false",
    ], check=True, cwd=ROOT)


if __name__ == "__main__":
    main()
