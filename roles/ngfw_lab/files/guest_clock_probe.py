#!/usr/bin/env python3
"""Read one guest clock sample; no process enumeration, root or mutation needed."""
import json
from pathlib import Path
import time


def sample():
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    monotonic_before = time.monotonic_ns()
    epoch = time.time_ns()
    monotonic_after = time.monotonic_ns()
    return {'guest_boot_id': boot, 'guest_epoch_ns': epoch,
            'guest_monotonic_ns': (monotonic_before + monotonic_after) // 2}


if __name__ == '__main__':
    print(json.dumps(sample(), sort_keys=True))
