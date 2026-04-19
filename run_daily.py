"""Run scraper once immediately, then every day at 07:00 local."""
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import schedule

ROOT = Path(__file__).parent
PY = sys.executable
LOG = ROOT / "output" / "run_log.txt"
LOG.parent.mkdir(exist_ok=True)


def run_scraper():
    print(f"[{datetime.now().isoformat(timespec='seconds')}] starting scraper run")
    try:
        r = subprocess.run([PY, str(ROOT / "scraper.py")], capture_output=True, text=True, timeout=600)
        with LOG.open("a") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] exit={r.returncode}\n")
            if r.returncode != 0:
                f.write(f"  stderr: {r.stderr[:800]}\n")
        print(r.stdout[-500:])
    except Exception as e:
        with LOG.open("a") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] error: {e}\n")
        print(f"error: {e}")


def main():
    run_scraper()  # immediate run
    schedule.every().day.at("09:00").do(run_scraper)
    schedule.every().day.at("17:00").do(run_scraper)
    print("Scheduled daily runs at 09:00 and 17:00. Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
