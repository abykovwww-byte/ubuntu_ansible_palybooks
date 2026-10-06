"""Root lab bootstrap; proxy itself runs unprivileged with no capabilities."""

import json
from pathlib import Path
import subprocess
import time

from security_lab.http_policy import HTTPJobPolicy, Lease
from security_lab.nft_policy import render_job_firewall


root = Path("/job")
policy = HTTPJobPolicy.model_validate_json((root / "policy.json").read_text("utf-8"))
lease = Lease.model_validate_json((root / "lease.json").read_text("utf-8"))
subprocess.run(["nft", "-f", "-"], input=render_job_firewall(policy, lease, time.time()), text=True, check=True)
subprocess.run(["setpriv", "--reuid=1000", "--regid=1000", "--clear-groups", "--bounding-set=-all",
                "mitmdump", "--quiet", "--listen-host", "0.0.0.0", "--listen-port", "8080",
                "--set", "confdir=/job/ca", "--set", "ssl_verify_upstream_trusted_ca=/job/upstream-ca.pem",
                "--set", "sl_policy=/job/policy.json", "--set", "sl_lease=/job/lease.json",
                "-s", "/app/proxy_addon.py"], check=True)
