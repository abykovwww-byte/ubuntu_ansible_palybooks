"""Bounded multi-port lab receiver and first/middle/last ACL/NAT smoke checks."""
import argparse
import http.client
import ipaddress
import json
import re
import selectors
import socket
import sys
import threading
import time
import uuid


def parse_ports(ranges):
    ports = set()
    for start, end in ranges:
        if type(start) is not int or type(end) is not int or not 10000 <= start <= end <= 20999:
            raise ValueError("policy listener range outside 10000..20999")
        new = set(range(start, end + 1))
        if ports & new:
            raise ValueError("overlapping policy listener ranges")
        ports.update(new)
    return sorted(ports)


def reply(path, peer, local):
    match = re.fullmatch(r"/policy-probe/([a-f0-9]{32})", path)
    if not match:
        raise ValueError("invalid probe path")
    return json.dumps({"nonce": match[1], "peer": peer[0], "local": local[0], "port": local[1]}).encode()


class Receiver:
    """One selector thread, bounded clients/bytes/idle time; no thread per port."""
    def __init__(self, ranges, bind="0.0.0.0"):
        self.selector = selectors.DefaultSelector()
        self.listeners = []
        self.clients = {}
        self.error = None
        self.stop = threading.Event()
        try:
            for port in parse_ports(ranges):
                sock = socket.socket()
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind((bind, port))
                    sock.listen(16)
                    sock.setblocking(False)
                    self.selector.register(sock, selectors.EVENT_READ)
                    self.listeners.append(sock)
                except Exception:
                    sock.close()
                    raise
        except Exception:
            self.close()
            raise
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def drop(self, sock):
        self.selector.unregister(sock)
        sock.close()
        self.clients.pop(sock, None)

    def run(self):
        try:
            while not self.stop.is_set():
                for key, events in self.selector.select(.1):
                    sock = key.fileobj
                    if key.data is None:
                        conn, _ = sock.accept()
                        conn.setblocking(False)
                        if len(self.clients) >= 128:
                            conn.close()
                            continue
                        data = {"received": b"", "out": b"", "expires": time.monotonic() + 2}
                        self.clients[conn] = data
                        self.selector.register(conn, selectors.EVENT_READ, data)
                        continue
                    data = key.data
                    try:
                        if events & selectors.EVENT_WRITE:
                            sent = sock.send(data["out"])
                            data["out"] = data["out"][sent:]
                            if not data["out"]:
                                self.drop(sock)
                        else:
                            chunk = sock.recv(1024)
                            data["received"] += chunk
                            if not chunk or len(data["received"]) > 1024:
                                self.drop(sock)
                            elif b"\r\n\r\n" in data["received"]:
                                method, path, _ = data["received"].split(b"\r\n", 1)[0].decode("ascii").split()
                                if method != "GET":
                                    raise ValueError("GET only")
                                payload = reply(path, sock.getpeername(), sock.getsockname())
                                data["out"] = (f"HTTP/1.1 200 OK\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload)
                                self.selector.modify(sock, selectors.EVENT_WRITE, data)
                    except BlockingIOError:
                        pass
                    except (OSError, ValueError, UnicodeError):
                        self.drop(sock)
                for sock, data in list(self.clients.items()):
                    if time.monotonic() > data["expires"]:
                        self.drop(sock)
        except Exception as exc:
            self.error = type(exc).__name__

    def close(self):
        self.stop.set()
        if hasattr(self, "thread"):
            self.thread.join(timeout=3)
        for sock in list(self.clients):
            self.drop(sock)
        for sock in self.listeners:
            self.selector.unregister(sock)
            sock.close()
        self.listeners.clear()
        self.selector.close()


def fetch(target, port, timeout=2):
    nonce = uuid.uuid4().hex
    conn = http.client.HTTPConnection(target, port, timeout=timeout)
    try:
        conn.request("GET", "/policy-probe/" + nonce)
        response = conn.getresponse()
        raw = response.read(2049)
        if response.status != 200 or len(raw) > 2048:
            raise ValueError("unexpected probe response")
        result = json.loads(raw)
        if result.get("nonce") != nonce:
            raise ValueError("probe nonce mismatch")
        return result
    finally:
        conn.close()


def check(plan):
    receiver = plan["traffic"]["receiver"]
    client = plan["traffic"]["client"]
    vip = plan["traffic"]["dnat_vip"]
    for value in (receiver, client, vip):
        addr = ipaddress.ip_address(value)
        if addr.version != 4 or not addr.is_private:
            raise ValueError("policy probes are limited to private lab IPv4")
    # A healthy control path is required before any deny observation.
    control = fetch(receiver, 8080)
    if control.get("local") != receiver or control.get("peer") != client:
        raise ValueError("control path/address translation differs from plan")
    results = []
    for case in plan["probes"]:
        if case["target"] not in {receiver, vip} or not 10000 <= case["port"] <= 30999:
            raise ValueError("probe outside the declared lab tuple range")
        observed, passed, inconclusive = None, False, False
        try:
            observed = fetch(case["target"], case["port"])
            expected_port = 8080 if case["kind"] == "dnat" else case["port"]
            passed = (case["expect"] == "allow" and observed.get("peer") == case["peer"] and
                      observed.get("local") == receiver and observed.get("port") == expected_port)
        except (TimeoutError, socket.timeout):
            if case["expect"] == "drop":
                after = fetch(receiver, 8080)
                passed = after.get("local") == receiver and after.get("peer") == client
                # Timeout alone cannot prove which firewall rule caused the drop.
                inconclusive = True
        except (OSError, ValueError, http.client.HTTPException):
            pass
        results.append({**case, "tuple_matches": passed, "observed": observed,
                        "rule_hit_verified": False, "deny_attribution_inconclusive": inconclusive})
    return {"ok": all(r["tuple_matches"] for r in results), "rule_hits_verified": False,
            "results": results, "note": "Confirm matching rule counters in MNGT; timeout alone is not deny-rule proof."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "listeners"])
    args = parser.parse_args()
    plan = json.load(sys.stdin)
    if args.action == "listeners":
        # Run INSIDE receiver: establishes listeners independently of a firewall drop.
        ports = parse_ports(plan["receiver_ranges"])
        for port in ports:
            result = fetch("127.0.0.1", port)
            if result.get("port") != port:
                raise ValueError("listener mismatch")
        print(json.dumps({"listener_count": len(ports), "ok": True}))
    else:
        result = check(plan)
        print(json.dumps(result))
        raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
