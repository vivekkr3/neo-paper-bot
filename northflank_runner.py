"""Always-on paper-only scheduler for container hosting. No broker order calls."""
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / 'runtime'
IST = ZoneInfo('Asia/Kolkata')
START, END = time(8, 55), time(15, 35)
LOG = logging.getLogger('northflank')


def in_window(stamp):
    return stamp.weekday() < 5 and START <= stamp.time() < END


def has_volume(path):
    # A Linux container volume is a mount at this exact path (including bind mounts).
    try:
        return any(row.split()[4] == str(path.resolve())
                   for row in Path('/proc/self/mountinfo').read_text().splitlines())
    except (OSError, IndexError):
        return False


def validate_storage():
    RUNTIME.mkdir(exist_ok=True)
    if not has_volume(RUNTIME):
        if os.environ.get('ALLOW_EPHEMERAL_PAPER') != '1':
            raise RuntimeError('Mount a persistent volume at /app/runtime, or explicitly set ALLOW_EPHEMERAL_PAPER=1 for temporary testing.')
        LOG.warning('TEMPORARY PAPER MODE: container replacement may erase orders, positions, and all P&L; do not use these results as a continuous track record.')
    probe = RUNTIME / '.write-check'
    probe.write_text('ok'); probe.unlink()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    os.umask(0o077)
    os.chdir(ROOT)
    if not os.environ.get('NEO_CONSUMER_KEY', '').strip():
        raise RuntimeError('Set NEO_CONSUMER_KEY in Northflank runtime variables')
    validate_storage()
    stop = threading.Event()
    child = None
    def shutdown(*_):
        stop.set()
        if child is not None and child.poll() is None:
            child.terminate()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    if '--preflight' in sys.argv:
        return subprocess.call([sys.executable, '-u', 'bot.py', '--mode', 'paper', '--preflight'])
    if len(sys.argv) != 1:
        raise SystemExit('Only --preflight is supported; live trading is unavailable')
    LOG.info('PAPER scheduler online; daily launch 08:55 IST weekdays; no real orders')
    completed = None
    warned = None
    while not stop.is_set():
        stamp = datetime.now(IST)
        day = stamp.date().isoformat()
        calendar = json.loads((ROOT / 'calendar.json').read_text())
        covered = calendar['valid_from'] <= day <= calendar['valid_through']
        if not covered and warned != day:
            LOG.error('No calendar coverage for %s; update calendar.json before trading', day)
            warned = day
        if covered and in_window(stamp) and day != completed:
            LOG.info('Starting paper engine for %s', day)
            child = subprocess.Popen([sys.executable, '-u', 'bot.py', '--mode', 'paper'])
            if stop.is_set() and child.poll() is None:
                child.terminate()
            rc = child.wait()
            child = None
            if rc == 0:
                completed = day
                LOG.info('Paper session ended; waiting for next weekday')
            else:
                LOG.error('Paper engine exit code %s; retry in 60 seconds if still within session', rc)
                stop.wait(60)
        else:
            stop.wait(15)
    return 0


if __name__ == '__main__':
    sys.exit(main())
