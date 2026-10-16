"""Trusted runtime JSON artifact operations, embedded in the existing SSH RPC.

Only a hash-pinned host-selected verifier is staged beside per-run JSON; no
application installation or credentials. POSIX dir-fd operations anchor every
component; the native FULL_ACCESS actor can still alter its writable parent
after launch, so verification receipts never claim immutable worker storage.
"""

from .native_preflight import PREFLIGHT_FILENAME

HANDOFF_FILE_PROGRAM = 'PREFLIGHT_FILENAME = ' + repr(PREFLIGHT_FILENAME) + '\n' + r'''
import hashlib, json, os, re, secrets, stat
MAX_HANDOFF_BYTES = 1048576
def canonical_file(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
def file_digest(value):
    return hashlib.sha256(value).hexdigest()
class HandoffStore:
    NAMES = ("osint-context.json", "descriptor.json", ".owner.json")
    def __init__(self, workspace, run_id, owner, descriptor, preflight_program=None):
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise RuntimeError("Handoff files require POSIX no-follow directory operations")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", run_id) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", owner):
            raise RuntimeError("Invalid owned artifact identity")
        self.workspace, self.run_id = workspace, run_id
        self.owner_hash = file_digest(owner.encode("utf-8"))
        self.descriptor = descriptor
        self.preflight_bytes = preflight_program.encode('utf-8') if isinstance(preflight_program,str) else None
        self.parents = self.directory = self.lock = None
        self.created = False
        self.identity = None
        self.temporary = set()
    def names(self):
        return self.NAMES + ((PREFLIGHT_FILENAME,) if self.descriptor.get('packet_schema_version')==2 else ())
    def _open_root(self):
        if not isinstance(self.workspace, str) or not self.workspace.startswith("/") or ".." in self.workspace.split("/"):
            raise RuntimeError("Fixed absolute workspace required")
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in self.workspace.split("/"):
                if not part: continue
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = next_fd
            try: os.mkdir("handoffs", mode=0o755, dir_fd=fd)
            except FileExistsError: pass
            self.parents = os.open("handoffs", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        finally:
            os.close(fd)
    def _open_directory(self):
        if self.parents is None: self._open_root()
        self.directory = os.open(self.run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.parents)
        info = os.fstat(self.directory)
        self.identity = {"device":info.st_dev, "inode":info.st_ino}
    def _read(self, name, maximum):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
                raise RuntimeError("Unexpected artifact type/size/link count")
            with os.fdopen(os.dup(fd), "rb") as stream:
                value = stream.read(maximum + 1)
            if len(value) > maximum: raise RuntimeError("Artifact limit")
            return value
        finally: os.close(fd)
    def _write_atomic(self, name, data, mode):
        temporary = "." + name + "." + secrets.token_hex(12) + ".tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.directory)
        self.temporary.add(temporary)
        try:
            with os.fdopen(os.dup(fd), "wb") as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.fchmod(fd, mode)
            # link publishes complete bytes atomically and cannot replace an
            # existing destination. The temporary name is then removed.
            os.link(temporary, name, src_dir_fd=self.directory, dst_dir_fd=self.directory, follow_symlinks=False)
            os.unlink(temporary, dir_fd=self.directory)
            self.temporary.remove(temporary)
            os.fsync(self.directory)
        finally: os.close(fd)
    def _marker(self):
        return {"schema_version":1, "owner_sha256":self.owner_hash, "run_id":self.run_id,
                "identity":self.identity, "descriptor_sha256":file_digest(canonical_file(self.descriptor).encode("utf-8"))}
    def _lock_owner(self):
        import fcntl
        self.lock = os.open(".owner.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.directory)
        fcntl.flock(self.lock, fcntl.LOCK_EX)
    def write(self, text):
        data = text.encode("utf-8")
        self._validate(data)
        if self.descriptor['packet_schema_version']==2:
            if (self.preflight_bytes is None or len(self.preflight_bytes)>65536
                    or file_digest(self.preflight_bytes)!=self.descriptor['preflight_sha256']):
                raise RuntimeError('Native preflight program hash mismatch')
        self._open_root()
        os.mkdir(self.run_id, mode=0o755, dir_fd=self.parents)  # atomic collision denial
        self.created = True
        self._open_directory()
        self._write_atomic(".owner.json", canonical_file(self._marker()).encode("utf-8"), 0o600)
        self._lock_owner()
        self._write_atomic("osint-context.json", data, 0o444)
        self._write_atomic("descriptor.json", canonical_file(self.descriptor).encode("utf-8"), 0o444)
        if self.descriptor['packet_schema_version']==2:
            self._write_atomic(PREFLIGHT_FILENAME,self.preflight_bytes,0o444)
        receipt = self.verify()
        self.created = False  # completed artifacts require full validation for deletion
        return receipt
    def _validate(self, data):
        d = self.descriptor
        if (type(d) is not dict or d.get("schema_version") != 1 or d.get("run_id") != self.run_id
                or type(d.get("packet_schema_version")) is not int or d["packet_schema_version"] not in {1, 2}
                or d.get("worker_path") != "/workspace/handoffs/"+self.run_id+"/osint-context.json"
                or type(d.get("size_bytes")) is not int or not 0 < d["size_bytes"] <= MAX_HANDOFF_BYTES
                or len(data) != d["size_bytes"] or file_digest(data) != d.get("content_sha256")):
            raise RuntimeError("Context descriptor/content hash mismatch")
        if d['packet_schema_version']==2 and (not isinstance(d.get('preflight_sha256'),str)
                or re.fullmatch('[a-f0-9]{64}',d['preflight_sha256']) is None):
            raise RuntimeError('Native preflight descriptor hash missing')
        packet = json.loads(data.decode("utf-8"))
        # This store validates transport/file identity, not execution admission.
        # The caller has already validated its typed preview or job-bound native
        # contract. Descriptor version and packet version are different formats.
        if (type(packet) is not dict or packet.get("schema_version") != d["packet_schema_version"] or packet.get("stage") != "checks"
                or packet.get("case_id") != d.get("case_id")
                or packet.get("generation") != d.get("generation") or packet.get("context_hash") != d.get("context_hash")
                or packet.get("binding", {}).get("id") != d.get("scope_id")
                or packet.get("binding", {}).get("hash") != d.get("scope_hash")
                or file_digest(canonical_file(packet["scope_manifest"]).encode("utf-8")) != d["scope_hash"]
                or canonical_file(packet) != data.decode("utf-8")):
            raise RuntimeError("Context schema or original scope binding mismatch")
    def verify(self, expected_identity=None):
        if self.directory is None: self._open_directory()
        if self.lock is None: self._lock_owner()
        marker = json.loads(self._read(".owner.json", 4096).decode("utf-8"))
        if marker != self._marker() or (expected_identity is not None and self.identity != expected_identity):
            raise RuntimeError("Artifact ownership or directory identity mismatch")
        if self._read("descriptor.json", 16384) != canonical_file(self.descriptor).encode("utf-8"):
            raise RuntimeError("Stored descriptor changed")
        for name in ("descriptor.json", "osint-context.json") + ((PREFLIGHT_FILENAME,) if self.descriptor['packet_schema_version']==2 else ()):
            if stat.S_IMODE(os.stat(name, dir_fd=self.directory, follow_symlinks=False).st_mode) != 0o444:
                raise RuntimeError("Read-only context mode changed")
        self._validate(self._read("osint-context.json", MAX_HANDOFF_BYTES))
        if self.descriptor['packet_schema_version']==2:
            if file_digest(self._read(PREFLIGHT_FILENAME,65536))!=self.descriptor['preflight_sha256']:
                raise RuntimeError('Stored native preflight program changed')
        return {"descriptor":self.descriptor, "identity":self.identity,
                "file_hash_verified":True, "file_read_only_mode":True,
                "preflight_file_hash_verified":self.descriptor['packet_schema_version']==2,
                "verification_stage":"before_native_launch_or_host_readback",
                "immutable_after_model_start":False}
    def cleanup(self, expected_identity=None):
        if self.directory is None:
            try: self._open_directory()
            except FileNotFoundError: return True
        if not self.created:
            self.verify(expected_identity)
        else:
            # Only the current host writer may clean a partial write. No
            # arbitrary entry, symlink or recursive path is ever followed.
            info = os.fstat(self.directory)
            if self.identity != {"device":info.st_dev,"inode":info.st_ino}: raise RuntimeError("Directory changed")
        for name in self.names() + tuple(self.temporary):
            try:
                info = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
            except FileNotFoundError: continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError("Cleanup refuses nonregular or linked artifact")
        if set(os.listdir(self.directory)) - set(self.names()) - self.temporary:
            raise RuntimeError("Cleanup refuses unexpected artifact entries")
        for name in self.names() + tuple(self.temporary):
            try: os.unlink(name, dir_fd=self.directory)
            except FileNotFoundError: pass
        os.fsync(self.directory)
        # Remove only the exact owned empty directory, never a tree.
        current = os.stat(self.run_id, dir_fd=self.parents, follow_symlinks=False)
        if self.identity != {"device":current.st_dev,"inode":current.st_ino}: raise RuntimeError("Parent entry changed")
        os.rmdir(self.run_id, dir_fd=self.parents)
        return True
    def release(self):
        for name in ("lock","directory","parents"):
            fd = getattr(self,name)
            if fd is not None:
                os.close(fd); setattr(self,name,None)
'''
