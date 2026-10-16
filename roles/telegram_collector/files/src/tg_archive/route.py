"""Read Tailscale's current peer path; output contains no peer inventory."""

import asyncio
import ipaddress
import json
import shutil
import subprocess


def classify_path(snapshot, host, lan_cidr):
    peers = [
        peer for peer in snapshot.get("Peer", {}).values() if host in peer.get("TailscaleIPs", [])
    ]
    if len(peers) != 1 or not peers[0].get("Online", False):
        return None
    address = peers[0].get("CurAddr", "")
    if not address:
        return "relay"
    # Tailscale endpoint uses host:port; IPv6 has brackets.
    endpoint = (
        address[1 : address.index("]")] if address.startswith("[") else address.rsplit(":", 1)[0]
    )
    ip = ipaddress.ip_address(endpoint)
    return "lan" if ip in ipaddress.ip_network(lan_cidr) else "direct"


def allowed_path(policy, current):
    return (
        current in {"lan"}
        if policy == "lan"
        else (
            current in {"lan", "direct"}
            if policy == "direct"
            else current in {"lan", "direct", "relay"}
        )
    )


class RouteGuard:
    def __init__(self, media):
        self.media = media
        self.current = None

    async def allowed(self):
        if getattr(self.media, "storage", "sftp") == "local":
            self.current = "local"
            return True
        executable = shutil.which("tailscale")
        if not executable:
            self.current = None
            return False
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [executable, "status", "--json"],
                capture_output=True,
                timeout=15,
                check=True,
            )
            self.current = classify_path(
                json.loads(result.stdout), self.media.sftp_host, self.media.lan_cidr
            )
        except (OSError, ValueError, subprocess.SubprocessError, KeyError):
            self.current = None
        return allowed_path(self.media.transfer_path, self.current)
