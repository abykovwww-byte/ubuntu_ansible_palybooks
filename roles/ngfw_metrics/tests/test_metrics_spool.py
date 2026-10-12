import gzip
import json
import http.client
import http.server
from pathlib import Path
import sys
import subprocess
import socket
import tempfile
import time
import threading
import urllib.request
import urllib.error
import unittest

FILES = Path(__file__).resolve().parents[1] / "files"
sys.path.insert(0, str(FILES))
from metrics_spool import Spool, restore, validate_payload, make_handler, http_sender, BoundedHTTPServer
from influx_readback import compare

PAYLOAD = b'cpu,host=ngfw,cpu=cpu0 usage_idle=19.25,events=3i,label="a b" 1791792000000000001\n'


class DurableSpoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.path = self.base / "spool.sqlite"
        self.spool = Spool(self.path)
        self.spool.register("local", "http://test/local?precision=ns")
        self.spool.register("external", "http://test/remote?precision=ns")

    def tearDown(self):
        self.temp.cleanup()

    def test_offline_restart_replay_exact_bytes_and_independent_cursors(self):
        self.spool.accept(PAYLOAD)
        local, external = [], []
        def offline(_payload):
            raise ConnectionError("credential body should never be logged")
        self.assertEqual(self.spool.deliver_one("external", offline), -1)
        self.assertEqual(self.spool.deliver_one("local", local.append), len(PAYLOAD))
        restarted = Spool(self.path)
        self.assertEqual(restarted.deliver_one("local", local.append), 0)
        self.assertEqual(restarted.deliver_one("external", external.append), len(PAYLOAD))
        self.assertEqual(external, [PAYLOAD])
        self.assertEqual(local, external)
        self.assertEqual(restarted.status()["sinks"][0]["pending_bytes"], 0)

    def test_ack_lost_retry_keeps_point_identity(self):
        self.spool.accept(PAYLOAD)
        attempts = []
        def persisted_then_connection_lost(data):
            attempts.append(data)
            raise ConnectionError()
        self.spool.deliver_one("external", persisted_then_connection_lost)
        Spool(self.path).deliver_one("external", attempts.append)
        self.assertEqual(attempts, [PAYLOAD, PAYLOAD])
        # Influx idempotency depends on same measurement/tag set/timestamp/fields.
        self.assertEqual(len(set(attempts)), 1)

    def test_local_database_outage_does_not_block_external(self):
        self.spool.accept(PAYLOAD)
        def outage(_):
            raise TimeoutError()
        self.spool.deliver_one("local", outage)
        delivered = []
        self.spool.deliver_one("external", delivered.append)
        self.assertEqual(delivered, [PAYLOAD])
        self.assertEqual(next(s for s in self.spool.status()["sinks"] if s["name"] == "local")["pending_batches"], 1)

    def test_quota_refuses_without_ack_or_partial_write(self):
        spool = Spool(self.path, max_bytes=len(PAYLOAD))
        spool.accept(PAYLOAD)
        with self.assertRaises(BufferError):
            spool.accept(PAYLOAD)
        self.assertEqual(spool.status()["accepted_points"], 1)

    def test_missing_timestamp_rejected_without_write(self):
        with self.assertRaises(ValueError):
            self.spool.accept(b"cpu,host=x idle=9.0\n")
        self.assertEqual(self.spool.status()["accepted_bytes"], 0)

    def test_backup_restore_preserves_raw_and_checkpoints(self):
        self.spool.accept(PAYLOAD)
        self.spool.deliver_one("local", lambda _: None)
        backup = self.base / "backup.sqlite"
        self.spool.backup(backup)
        restored = self.base / "restored.sqlite"
        restore(backup, restored)
        recovered = Spool(restored)
        data = []
        self.assertEqual(recovered.deliver_one("local", data.append), 0)
        recovered.deliver_one("external", data.append)
        self.assertEqual(data, [PAYLOAD])
        with self.assertRaises(FileExistsError):
            restore(backup, restored)
        backup.write_bytes(b"corrupted")
        with self.assertRaises(ValueError):
            restore(backup, self.base / "bad.sqlite")

    def test_retention_never_purges_pending_sink(self):
        self.spool.accept(PAYLOAD, now=time.time()-91*86400)
        self.spool.deliver_one("local", lambda _: None)
        self.assertEqual(self.spool.prune(), 0)
        self.spool.deliver_one("external", lambda _: None)
        self.assertEqual(self.spool.prune(), 1)
        self.assertEqual(self.spool.status()["retained_bytes"], 0)
        with self.assertRaises(ValueError):
            self.spool.prune(30)

    def test_destination_change_requires_new_cursor(self):
        with self.assertRaises(ValueError):
            self.spool.register("external", "http://other/write?precision=ns")

    def test_sizing_uses_observed_completed_peak_minute(self):
        self.assertIsNone(self.spool.sizing()["peak_bytes_per_second"])
        self.spool.accept(PAYLOAD, now=time.time()-120)
        sizing = self.spool.sizing()
        self.assertEqual(sizing["peak_60s_bytes_per_second"], len(PAYLOAD)/60)
        self.assertGreaterEqual(sizing["queue_24h_payload_bytes_30pct_margin"], len(PAYLOAD)/60*86400*1.3)

    def test_process_exit_after_commit_preserves_batch(self):
        program = "import sys,os;sys.path.insert(0,sys.argv[1]);from metrics_spool import Spool;s=Spool(sys.argv[2]);s.accept(bytes.fromhex(sys.argv[3]));os._exit(17)"
        result = subprocess.run([sys.executable, "-c", program, str(FILES), str(self.path), PAYLOAD.hex()], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 17, result.stderr.decode(errors="replace"))
        recovered = Spool(self.path)
        delivered = []
        recovered.deliver_one("local", delivered.append)
        self.assertEqual(delivered, [PAYLOAD])

    def test_raw_archive_export_preserves_payload(self):
        self.spool.accept(PAYLOAD)
        exported = self.base / "raw.lp"
        receipt = self.spool.raw_export(exported)
        self.assertEqual(receipt["batches"], 1)
        self.assertEqual(exported.read_bytes(), PAYLOAD)


class ReadbackTests(unittest.TestCase):
    def receipt(self, value=1, timestamp=1791792000000000001):
        return {"results": [{"series": [{"name": "cpu", "tags": {"host": "ngfw"}, "columns": ["time", "value"], "values": [[timestamp, value]]}]}]}

    def test_exact_nanoseconds_fields_and_tags(self):
        self.assertTrue(compare(self.receipt(), self.receipt())["equal"])
        self.assertFalse(compare(self.receipt(), self.receipt(value=2))["equal"])
        self.assertFalse(compare(self.receipt(), self.receipt(timestamp=1791792000000000002))["equal"])
        other = self.receipt()
        other["results"][0]["series"][0]["tags"]["host"] = "other"
        self.assertFalse(compare(self.receipt(), other)["equal"])

    def test_empty_error_and_lossy_timestamp_are_not_proof(self):
        for bad in ({"results": [{}]}, {"results": [{"error": "permission denied"}]}, self.receipt(timestamp=1.791e18)):
            with self.assertRaises(ValueError):
                compare(self.receipt(), bad)

    def test_partial_flags_never_pass_equal_truncated_readback(self):
        for level in ("response", "result", "series"):
            data = self.receipt()
            target = data if level == "response" else data["results"][0] if level == "result" else data["results"][0]["series"][0]
            target["partial"] = True
            with self.assertRaises(ValueError):
                compare(data, data)


class LocalHttpReplayTests(unittest.TestCase):
    def test_actual_http_offline_restart_reconnect_readback(self):
        """Real loopback sockets with temporary destination, not external acceptance."""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "spool.sqlite"
            spool = Spool(path)
            captured = []
            state = {"offline": True}
            class TemporaryDestination(http.server.BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass
                def do_POST(self):
                    body = self.rfile.read(int(self.headers["Content-Length"]))
                    if state["offline"]:
                        self.send_error(503)
                    else:
                        captured.append(body)
                        self.send_response(204)
                        self.end_headers()
            remote = http.server.ThreadingHTTPServer(("127.0.0.1", 0), TemporaryDestination)
            receiver = BoundedHTTPServer(("127.0.0.1", 0), make_handler(spool, 1048576))
            threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (remote, receiver)]
            for thread in threads:
                thread.start()
            try:
                url = "http://127.0.0.1:%s/write?precision=ns" % remote.server_port
                spool.register("external", url)
                send = http_sender({"write_url": url})
                request = urllib.request.Request("http://127.0.0.1:%s/write?precision=ns" % receiver.server_port, data=PAYLOAD, method="POST")
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.status, 204)
                with urllib.request.urlopen("http://127.0.0.1:%s/ping" % receiver.server_port) as response:
                    self.assertEqual(response.status, 204)
                self.assertEqual(spool.deliver_one("external", send), -1)
                self.assertEqual(spool.status()["sinks"][0]["pending_batches"], 1)
                spool = Spool(path)
                state["offline"] = False
                self.assertEqual(spool.deliver_one("external", send), len(PAYLOAD))
                self.assertEqual(spool.deliver_one("external", send), 0)
                self.assertEqual(captured, [PAYLOAD])
                self.assertEqual(spool.status()["sinks"][0]["pending_bytes"], 0)
            finally:
                for server in (remote, receiver):
                    server.shutdown()
                    server.server_close()
                for thread in threads:
                    thread.join(2)

    def test_source_acl_denies_unapproved_client(self):
        with tempfile.TemporaryDirectory() as temp:
            spool = Spool(Path(temp) / "spool.sqlite")
            receiver = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(spool, 1048576, allowed_sources=["192.0.2.1/32"]))
            thread = threading.Thread(target=receiver.serve_forever, daemon=True)
            thread.start()
            try:
                request = urllib.request.Request("http://127.0.0.1:%s/write?precision=n" % receiver.server_port, data=PAYLOAD, method="POST")
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request)
                self.assertEqual(caught.exception.code, 403)
                self.assertEqual(spool.status()["accepted_points"], 0)
            finally:
                receiver.shutdown()
                receiver.server_close()
                thread.join(2)


class GzipIngestTests(unittest.TestCase):
    """Synthetic LP over real loopback HTTP; no appliance/live acceptance claim."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.spool = Spool(Path(self.temp.name) / "spool.sqlite")
        self.limit = len(PAYLOAD) * 8
        self.receiver = BoundedHTTPServer(("127.0.0.1", 0), make_handler(self.spool, self.limit))
        self.thread = threading.Thread(target=self.receiver.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%s/write?db=telegraf&precision=ns" % self.receiver.server_port

    def tearDown(self):
        self.receiver.shutdown()
        self.receiver.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def post(self, body, encoding="gzip"):
        request = urllib.request.Request(self.url, data=body, method="POST",
                                         headers={"Content-Encoding": encoding})
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status

    def assert_rejected_without_growth(self, body, encoding="gzip"):
        before = self.spool.status()
        with self.spool.connect() as db:
            batches_before = db.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post(body, encoding)
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()
        after = self.spool.status()
        for name in ("accepted_bytes", "accepted_points", "retained_bytes"):
            self.assertEqual(after[name], before[name], name)
        with self.spool.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM batches").fetchone()[0], batches_before)

    def test_gzip_preserves_decoded_bytes_hash_and_replay_at_limit(self):
        payload = PAYLOAD * 8
        self.assertEqual(self.post(gzip.compress(payload)), 204)
        with self.spool.connect() as db:
            row = db.execute("SELECT payload,sha256,points FROM batches").fetchone()
        import hashlib
        self.assertEqual(bytes(row["payload"]), payload)
        self.assertEqual(row["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(row["points"], 8)
        self.spool.register("local", "http://synthetic/write?precision=ns")
        delivered = []
        self.assertEqual(self.spool.deliver_one("local", delivered.append), len(payload))
        self.assertEqual(delivered, [payload])

    def test_identity_still_preserves_exact_payload(self):
        self.assertEqual(self.post(PAYLOAD, "identity"), 204)
        with self.spool.connect() as db:
            self.assertEqual(bytes(db.execute("SELECT payload FROM batches").fetchone()[0]), PAYLOAD)

    def test_invalid_truncated_crc_and_trailing_junk_are_not_acknowledged(self):
        # Existing valid data must survive each rejection without queue growth.
        self.assertEqual(self.post(PAYLOAD, "identity"), 204)
        valid = gzip.compress(PAYLOAD)
        corrupted = valid[:-8] + bytes([valid[-8] ^ 1]) + valid[-7:]
        for body in (b"not gzip", valid[:-1], valid[:-8], corrupted, valid + b"junk"):
            with self.subTest(body=body):
                self.assert_rejected_without_growth(body)

    def test_expansion_bomb_and_wire_oversize_are_rejected(self):
        bomb = gzip.compress(PAYLOAD * 1000)
        self.assertLess(len(bomb), self.limit)
        self.assert_rejected_without_growth(bomb)
        self.assert_rejected_without_growth(gzip.compress(PAYLOAD * 8 + b"\n"))
        self.assert_rejected_without_growth(b"x" * (self.limit + 1), "identity")

    def test_concatenated_members_share_one_decoded_limit(self):
        self.assertEqual(self.post(gzip.compress(PAYLOAD) + gzip.compress(PAYLOAD)), 204)
        with self.spool.connect() as db:
            self.assertEqual(bytes(db.execute("SELECT payload FROM batches").fetchone()[0]), PAYLOAD * 2)
        self.assert_rejected_without_growth(gzip.compress(PAYLOAD * 8) + gzip.compress(PAYLOAD))
        self.assert_rejected_without_growth(gzip.compress(PAYLOAD) + gzip.compress(PAYLOAD)[:-2])

    def test_unsupported_encoding_rejected_without_growth(self):
        self.assert_rejected_without_growth(PAYLOAD, "deflate")

    def raw_post(self, headers, body, path="/write?db=telegraf"):
        request = ("POST " + path + " HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
                   + headers + "\r\n").encode("ascii") + body
        with socket.create_connection(("127.0.0.1", self.receiver.server_port), timeout=5) as connection:
            connection.sendall(request)
            connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            status = response.status
            response.read()
            return status

    def assert_raw_rejected(self, headers, body, path="/write?db=telegraf", status=400):
        before = self.spool.status()
        with self.spool.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
        self.assertEqual(self.raw_post(headers, body, path), status)
        after = self.spool.status()
        for name in ("accepted_bytes", "accepted_points", "retained_bytes"):
            self.assertEqual(before[name], after[name], name)
        with self.spool.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM batches").fetchone()[0], count)

    def test_native_chunked_gzip_without_precision_preserves_lp(self):
        payload = PAYLOAD * 8
        encoded = gzip.compress(payload)
        connection = http.client.HTTPConnection("127.0.0.1", self.receiver.server_port, timeout=5)
        try:
            # Iterable body causes standard HTTP/1.1 chunked framing, no CL.
            connection.request("POST", "/write?db=telegraf", body=iter([encoded[:5], encoded[5:]]),
                               headers={"Content-Encoding": "gzip"}, encode_chunked=True)
            response = connection.getresponse()
            self.assertEqual(response.status, 204)
            response.read()
        finally:
            connection.close()
        with self.spool.connect() as db:
            self.assertEqual(bytes(db.execute("SELECT payload FROM batches").fetchone()[0]), payload)

    def test_ambiguous_and_unsupported_framing_rejected(self):
        headers = ["Content-Length: 10\r\nTransfer-Encoding: chunked\r\n",
                   "Content-Length: 10\r\nContent-Length: 10\r\n",
                   "Content-Length: 10, 10\r\n", "Content-Length: +10\r\n",
                   "Transfer-Encoding: chunked\r\nTransfer-Encoding: chunked\r\n",
                   "Transfer-Encoding: gzip, chunked\r\n", "Transfer-Encoding: identity\r\n",
                   "Content-Length: 10\r\nContent-Encoding: gzip\r\nContent-Encoding: identity\r\n", ""]
        for header in headers:
            with self.subTest(header=header):
                self.assert_raw_rejected(header, b"0\r\n\r\n")

    def test_malformed_truncated_chunks_and_trailers_never_grow_spool(self):
        good_chunk = ("%x\r\n" % len(PAYLOAD)).encode() + PAYLOAD + b"\r\n"
        bodies = [b"z\r\n", b"-1\r\n", b"1\na\r\n0\r\n\r\n", b"4\r\nx", b"1\r\na!!",
                  b"ffffffffffffffff\r\n", b"1;unsupported=extension\r\na\r\n0\r\n\r\n",
                  good_chunk, good_chunk + b"0\r\n", good_chunk + b"0\r\nContent-Length: 0\r\n\r\n",
                  good_chunk + b"0\r\n invalid-fold\r\n\r\n", good_chunk + b"0\r\ninvalid\r\n\r\n",
                  good_chunk + b"0\r\n" + b"X: y\r\n" * 66 + b"\r\n"]
        self.assertEqual(self.post(PAYLOAD, "identity"), 204)
        for body in bodies:
            with self.subTest(body=body[:40]):
                self.assert_raw_rejected("Transfer-Encoding: chunked\r\n", body)

    def test_chunk_wire_budget_includes_framing_and_gzip_limit_still_applies(self):
        encoded = gzip.compress(PAYLOAD * 2, compresslevel=0)
        tiny_chunks = b"".join(b"1\r\n" + bytes([byte]) + b"\r\n" for byte in encoded) + b"0\r\n\r\n"
        # Valid gzip and valid decoded LP both fit; framing alone exceeds budget.
        self.assertLess(len(encoded), self.limit)
        self.assertLess(len(PAYLOAD) * 2, self.limit)
        self.assertGreater(len(tiny_chunks), self.limit)
        self.assert_raw_rejected("Transfer-Encoding: chunked\r\nContent-Encoding: gzip\r\n", tiny_chunks)
        bomb = gzip.compress(PAYLOAD * 1000)
        body = ("%x\r\n" % len(bomb)).encode() + bomb + b"\r\n0\r\n\r\n"
        self.assertLess(len(body), self.limit)
        self.assert_raw_rejected("Transfer-Encoding: chunked\r\nContent-Encoding: gzip\r\n", body)

    def test_bounded_valid_trailer_and_explicit_precision_validation(self):
        body = ("%x\r\n" % len(PAYLOAD)).encode() + PAYLOAD + b"\r\n0\r\nX-Receipt: synthetic\r\n\r\n"
        self.assertEqual(self.raw_post("Transfer-Encoding: chunked\r\n", body), 204)
        for query in ("precision=ms", "precision=", "precision=ns&precision=ns", "precision=n&precision=ms"):
            self.assert_raw_rejected("Transfer-Encoding: chunked\r\n", body, "/write?" + query)

    def test_request_header_line_and_aggregate_budgets(self):
        for extra in ("X-Large: " + "x" * 4096 + "\r\n", ("X-Many: " + "x" * 3000 + "\r\n") * 6):
            self.assert_raw_rejected(extra + "Content-Length: %d\r\n" % len(PAYLOAD), PAYLOAD, status=431)


if __name__ == "__main__":
    unittest.main()
