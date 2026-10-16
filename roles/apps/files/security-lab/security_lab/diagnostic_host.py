"""Trusted host-side glue for stock OpenSSL, testssl.sh, nft and tcpdump.

Embedded into the existing SSH/local host program, like HandoffStore. No custom
TCP/TLS implementation, caller-selected executable, host interface or mount.
"""

DIAGNOSTIC_HOST_PROGRAM = r'''
import ipaddress, math, selectors

class DiagnosticHost:
    def __init__(self, request):
        if sys.platform != 'linux':
            raise RuntimeError('Diagnostic launcher requires a Linux Docker host')
        self.p = request
        self.name = request['container_name']
        self.owner = request['owner']
        self.run_id = request['run_id']
        self.roles = {'probe': self.name, 'capture': self.name+'-capture', 'firewall': self.name+'-firewall'}
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', self.run_id)
                or not re.fullmatch(r'security-lab-pentest-[a-f0-9]{24}', self.name)):
            raise RuntimeError('Invalid diagnostic ownership identity')

    def docker(self, args, *, data=None, timeout=10):
        result = subprocess.run(['docker', *args], cwd=self.p['compose_directory'],
            input=data, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError('Owned diagnostic Docker operation failed')
        return result.stdout+result.stderr if args[0] == 'logs' else result.stdout

    def owned(self, role):
        name = self.roles[role]
        query = ['ps', '-a', '--filter', 'name=^/'+name+'$', '--format', '{{.ID}}']
        ids = self.docker(query).split()
        if not ids:
            return None
        if len(ids) != 1:
            raise RuntimeError('Ambiguous diagnostic container')
        value = json.loads(self.docker(['inspect', ids[0]]))[0]
        labels = value.get('Config', {}).get('Labels', {})
        if (value.get('Name') != '/'+name or labels.get('security-lab.compose-owner') != self.owner
                or labels.get('security-lab.run-id') != self.run_id
                or labels.get('security-lab.diagnostic-role') != role):
            raise RuntimeError('Diagnostic container ownership mismatch')
        return value

    def labels(self, role):
        return ['--name', self.roles[role], '--label', 'security-lab.compose-owner='+self.owner,
                '--label', 'security-lab.run-id='+self.run_id,
                '--label', 'security-lab.diagnostic-role='+role]

    def directory(self, *, create=False):
        root = pathlib.Path(self.p['runs_directory'])
        if not root.is_dir() or root.is_symlink() or root.resolve() != root:
            raise RuntimeError('Diagnostic artifact root is not a real host directory')
        directory = root / self.run_id
        if create:
            directory.mkdir(mode=0o700)
            with (directory/'owner.json').open('x', encoding='utf-8') as stream:
                json.dump({'owner':self.owner,'run_id':self.run_id}, stream)
        if not directory.exists():
            return None
        marker = directory/'owner.json'
        if (directory.is_symlink() or not directory.is_dir() or marker.is_symlink()
                or not marker.is_file() or marker.stat().st_size > 1000
                or json.loads(marker.read_text('utf-8')) != {'owner':self.owner,'run_id':self.run_id}):
            raise RuntimeError('Diagnostic artifact ownership mismatch')
        return directory

    def artifacts(self):
        import fcntl
        directory = self.directory()
        rows = []
        if directory is None:
            return rows
        names = ['traffic.pcap', 'capture.log', 'network-policy.json', 'result.json']
        names += [f'attempt-{i}.{suffix}' for i in range(1,4) for suffix in ('stdout','stderr')]
        for name in names:
            path = directory/name
            if path.is_symlink():
                raise RuntimeError('Diagnostic artifact link refused')
            if not path.exists():
                continue
            if not path.is_file() or path.stat().st_size > 1048576:
                raise RuntimeError('Diagnostic artifact bound exceeded')
            with path.open('rb') as stream:
                until = time.monotonic()+3
                while True:
                    try:
                        fcntl.flock(stream,fcntl.LOCK_SH|fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= until:
                            raise RuntimeError('Diagnostic output writer has not stopped')
                        time.sleep(.02)
                data = stream.read(1048577)
            if len(data)>1048576:
                raise RuntimeError('Diagnostic artifact grew beyond its bound')
            rows.append({'name':name,'host_path':str(path),'size_bytes':len(data),
                         'sha256':hashlib.sha256(data).hexdigest()})
        return rows

    def close(self):
        import fcntl
        directory = self.directory()
        if directory is None:
            return self._close()
        fd = os.open(directory/'.cleanup.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'r+') as lock:
            until = time.monotonic()+10
            while True:
                try:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= until:
                        raise RuntimeError('Diagnostic cleanup already active')
                    time.sleep(.02)
            return self._close()

    def _close(self):
        # Never rm a container until exact ownership and stopped state are read
        # back. Capture runs after probe termination to observe local teardown.
        directory = self.directory()
        capture_error = None
        for role in ('probe', 'firewall', 'capture'):
            value = self.owned(role)
            if value is None:
                continue
            if value['State'].get('Running'):
                self.docker(['stop','--time','2',value['Id']], timeout=7)
            value = self.owned(role)
            if value is not None and value['State'].get('Running'):
                raise RuntimeError('Diagnostic stop unconfirmed')
            if role == 'capture' and directory is not None and value is not None:
                try:
                    log = directory/'capture.log'
                    if not log.exists():
                        with log.open('x', encoding='utf-8') as stream:
                            stream.write(self.docker(['logs',value['Id']])[:16000])
                except Exception:
                    # Evidence collection failure must not prevent revocation.
                    capture_error = 'capture_artifact_collection_failed'
            if value is not None:
                self.docker(['rm',value['Id']])
            if self.owned(role) is not None:
                raise RuntimeError('Diagnostic removal unconfirmed')
        if directory is not None:
            path = directory/'traffic.pcap'
            if path.is_symlink():
                raise RuntimeError('Diagnostic capture link refused')
            if path.exists():
                path.chmod(0o600)
                if path.stat().st_size < 24:
                    capture_error = capture_error or 'capture_artifact_incomplete'
        return {'status':'stopped','access_closed':True,'artifacts':self.artifacts(),
                'capture_error':capture_error,
                'capture_vantage':'probe_network_namespace_on_worker_host',
                'target_receiving_side_capture':False}

    def firewall(self, rules=None):
        # The installer has no host mount, host network or Docker socket. Its
        # NET_ADMIN applies only to the exact disposable probe namespace.
        args = ['run','--rm','-i',*self.labels('firewall'),'--network','container:'+self.roles['probe'],
                '--workdir','/tmp',
                '--user','0:0','--cap-drop','ALL','--cap-add','NET_ADMIN','--read-only',
                '--security-opt','no-new-privileges:true','--pids-limit','16','--memory','64m',
                '--entrypoint','/usr/bin/timeout',self.image,'--kill-after=1s','4',
                '/usr/sbin/nft']
        return self.docker(args+(['-f','-'] if rules is not None else ['-j','list','table','inet','security_lab_diagnostic']),
                           data=rules, timeout=5)

    def install_network(self, address, port, network, deadline):
        version = ipaddress.ip_address(address).version
        family = 'ip' if version == 4 else 'ip6'
        addr_type = 'ipv4_addr' if version == 4 else 'ipv6_addr'
        # Kernel membership expires even if SSH/Controller/launcher disappears.
        # Subtract the bounded installer latency, never round expiry upward.
        ttl = math.floor((deadline-time.time()-5)*1000)
        if ttl < 1000:
            raise RuntimeError('Insufficient diagnostic installation time')
        rules = ('add table inet security_lab_diagnostic\n'
            'table inet security_lab_diagnostic {\n'
            ' set endpoint { type '+addr_type+' . inet_service; flags timeout; '
            'elements = { '+address+' . '+str(port)+' timeout '+str(ttl)+'ms }; }\n'
            ' chain input { type filter hook input priority 0; policy drop;\n'
            '  ct state related meta l4proto { icmp, ipv6-icmp } accept\n'
            '  meta l4proto ipv6-icmp ip6 hoplimit 255 icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert } accept\n'
            '  ct state established '+family+' saddr . tcp sport @endpoint accept\n }\n'
            ' chain output { type filter hook output priority 0; policy drop;\n'
            '  meta l4proto ipv6-icmp ip6 hoplimit 255 icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert } accept\n'
            '  ct state { new, established } '+family+' daddr . tcp dport @endpoint accept\n }\n'
            ' chain forward { type filter hook forward priority 0; policy drop; }\n}\n')
        self.firewall(rules)
        before = json.loads(self.firewall())
        self.docker(['network','disconnect','none',self.name])
        self.docker(['network','connect',network,self.name])
        actual = self.owned('probe')
        if actual is None or set(actual['NetworkSettings']['Networks']) != {network}:
            raise RuntimeError('Diagnostic network attachment mismatch')
        after = json.loads(self.firewall())
        # nft dynamically reports expires/handles; compare the rule structure,
        # not those time-dependent values, across Docker network attachment.
        def stable(value):
            if isinstance(value,dict):
                return {k:stable(v) for k,v in value.items() if k not in {'expires','handle','metainfo'}}
            if isinstance(value,list):
                return [stable(v) for v in value]
            return value
        if not before.get('nftables') or stable(before) != stable(after):
            raise RuntimeError('Diagnostic nft readback changed during network attachment')
        policy = {'address':address,'port':port,'deadline':deadline,'rules_sha256':hashlib.sha256(rules.encode()).hexdigest(),
                  'installed_readback':after,'independent_receiving_side_qualified':False}
        with (self.directory()/'network-policy.json').open('x',encoding='utf-8') as stream:
            json.dump(policy,stream)

    def capture(self, address, port, deadline):
        seconds = max(1, math.floor(deadline-time.time()))
        # Docker archive/cp cannot reliably read a container's tmpfs. Give only
        # the trusted observer one fresh file, never the run directory. Its
        # parent stays host-owned 0700; restore 0600 once the observer is closed.
        path = self.directory()/'traffic.pcap'
        with path.open('xb'):
            pass
        path.chmod(0o666)
        self.docker(['run','-d',*self.labels('capture'),'--network','container:'+self.name,
            '--workdir','/tmp',
            '--user','0:0','--cap-drop','ALL','--cap-add','NET_RAW','--cap-add','SETUID','--cap-add','SETGID',
            '--cap-add','KILL',
            '--security-opt','no-new-privileges:true','--read-only','--pids-limit','16','--memory','64m',
            '--mount','type=bind,src='+str(path)+',dst=/capture.pcap',
            '--ulimit','fsize=1048576:1048576',
            '--entrypoint','/usr/bin/timeout',self.image,'--signal=TERM','--kill-after=1s',str(seconds),
            '/usr/bin/tcpdump','-Z','probe','-p','-i','eth0','-nn','-U','-s','2048','-c','256',
            '-w','/capture.pcap','host',address,'and','tcp','port',str(port)])
        ready_until = min(deadline-1,time.time()+5)
        while time.time() < ready_until:
            value = self.owned('capture')
            if value is None or not value['State'].get('Running'):
                raise RuntimeError('Diagnostic capture did not start')
            # tcpdump writes readiness to stderr, not stdout.
            result = subprocess.run(['docker','logs',value['Id']],capture_output=True,text=True,timeout=2)
            if result.returncode:
                raise RuntimeError('Diagnostic capture readiness unavailable')
            if 'listening on eth0' in result.stderr:
                return
            time.sleep(.05)
        raise RuntimeError('Diagnostic capture readiness timeout')

    def attempt(self, argv, seconds, index):
        import fcntl
        directory = self.directory()
        # Ordinary pipes, no PTY/stdin commands. -ign_eof prevents s_client from
        # aborting early merely because a noninteractive stdin has closed.
        command = ['docker','exec',self.name,'/usr/bin/timeout','--signal=TERM','--kill-after=1s',
                   str(seconds),*argv]
        process = subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        selector = selectors.DefaultSelector()
        buffers, tails, outputs, total, reason = {}, {}, {}, 0, None
        started, cutoff = time.time(), time.monotonic()+seconds+1
        try:
            for suffix,pipe in (('stdout',process.stdout),('stderr',process.stderr)):
                buffers[suffix] = bytearray()
                tails[suffix] = b''
                outputs[suffix] = (directory/f'attempt-{index}.{suffix}').open('xb')
                fcntl.flock(outputs[suffix],fcntl.LOCK_EX)
                selector.register(pipe,selectors.EVENT_READ,suffix)
            while selector.get_map():
                if time.monotonic() >= cutoff:
                    reason = 'deadline'
                    break
                for key,_ in selector.select(timeout=min(.1,max(0,cutoff-time.monotonic()))):
                    chunk = os.read(key.fileobj.fileno(),8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = max(0,65536-total)
                    saved = chunk[:remaining]
                    outputs[key.data].write(saved)
                    outputs[key.data].flush()
                    buffers[key.data].extend(saved[:max(0,2000-len(buffers[key.data]))])
                    tails[key.data] = (tails[key.data]+saved)[-8192:]
                    total += len(saved)
                    if self.p['kind'] == 'tls' and re.search(rb'(?m)^CONNECTION ESTABLISHED\r?$',tails['stderr']):
                        reason = 'handshake_complete'
                        break
                    if len(chunk) > remaining:
                        reason = 'output_limit'
                        break
                if reason:
                    break
            if reason:
                # Killing docker exec alone does not kill its container process.
                # Stop the exact worker as well, then let close collect PCAP.
                value = self.owned('probe')
                if value is not None:
                    self.docker(['stop','--time','1',value['Id']],timeout=5)
            try:
                code = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                code = process.wait(timeout=2)
                reason = reason or 'deadline'
        finally:
            selector.close()
            for stream in outputs.values():
                stream.close()
            for pipe in (process.stdout,process.stderr):
                pipe.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
        return {'type':'command_run','command':argv,'exit_code':code,'started_at':started,'finished_at':time.time(),
                'stop_reason':reason or ('deadline' if code in {124,137} else 'exited'),
                'stdout':buffers['stdout'].decode('utf-8','replace'),
                'stderr':buffers['stderr'].decode('utf-8','replace'),
                'tls_handshake_complete':reason == 'handshake_complete',
                'retryable_timeout':reason is None and code == 124,
                'output_bytes':total,'preview_truncated':total>sum(len(v) for v in buffers.values())}

    def run(self):
        p = self.p
        deadline = p['deadline']
        if not isinstance(deadline,(int,float)) or not math.isfinite(deadline) or not 0 < deadline-time.time() <= 180:
            raise RuntimeError('Invalid diagnostic deadline')
        address = str(ipaddress.ip_address(p['address']))
        port = p['port']
        if type(port) is not int or not 1 <= port <= 65535:
            raise RuntimeError('Invalid diagnostic port')
        config = json.loads(self.docker(['compose','--file','compose.yml','--profile','tools','--profile','diagnostics',
                                         'config','--format','json']))
        if 'pentest-diagnostics' not in config.get('services',{}):
            raise RuntimeError('diagnostic_runtime_update_required')
        network = config.get('networks',{}).get('tool-egress',{}).get('name')
        if not network or not re.fullmatch(r'[A-Za-z0-9_.-]+',network):
            raise RuntimeError('Diagnostic egress network unavailable')
        self.directory(create=True)
        rows, error = [], None
        try:
            for role in self.roles:
                if self.owned(role) is not None:
                    raise RuntimeError('Diagnostic identity already exists')
            self.docker(['compose','--file','compose.yml','--profile','diagnostics','run','-d','-T',
                *self.labels('probe'),'--entrypoint','/usr/local/bin/python','pentest-diagnostics','-c',
                'import sys,time; time.sleep(max(0,float(sys.argv[1])-time.time()))',str(deadline)])
            value = self.owned('probe')
            if (value is None or not value['State'].get('Running') or value['Config'].get('User') != '1001:1001'
                    or value['HostConfig'].get('NetworkMode') != 'none'
                    or not value['HostConfig'].get('ReadonlyRootfs')
                    or any(m.get('Type')!='tmpfs' or m.get('Destination')!='/tmp' for m in value.get('Mounts',[]))
                    or value['HostConfig'].get('Privileged')
                    or set(value['HostConfig'].get('CapDrop') or []) != {'ALL'}
                    or value['HostConfig'].get('CapAdd')
                    or 'no-new-privileges:true' not in (value['HostConfig'].get('SecurityOpt') or [])):
                raise RuntimeError('Diagnostic probe isolation readback failed')
            self.image = value['Image']
            if not re.fullmatch(r'sha256:[a-f0-9]{64}',self.image):
                raise RuntimeError('Diagnostic immutable image identity unavailable')
            self.install_network(address,port,network,deadline)
            self.capture(address,port,deadline)
            for index in range(1,p['attempts']+1):
                available = math.floor(deadline-time.time()-5)
                if available < 1:
                    break
                capture = self.owned('capture')
                if capture is None or not capture['State'].get('Running'):
                    error = 'capture_ended_before_probe'
                    break
                row = self.attempt(p['argv'],min(p['attempt_seconds'],available),index)
                rows.append(row)
                # Retry only an unanswered/timed-out attempt, never replay a
                # successful handshake or a definite TLS/verification error.
                if not row['retryable_timeout'] or index == p['attempts']:
                    break
                time.sleep(.25)
        except Exception as exc:
            error = type(exc).__name__+': '+str(exc)[:200]
        finally:
            closure = self.close()
        success = rows and (rows[-1]['exit_code']==0 or rows[-1].get('tls_handshake_complete'))
        result = {**closure,'status':'completed' if success and not error and not closure.get('capture_error') else 'failed',
            'receipts':rows,'receipts_truncated':any(r['preview_truncated'] for r in rows),
            'receipt_kind':'real_isolated_'+p['kind'],'attempts_requested':p['attempts'],
            'tool_image_id':self.image if hasattr(self,'image') else None,'reserved_deadline':deadline,
            'attempts_completed':len(rows),'error':error,'verified_vulnerabilities':[],
            'scope_enforcement':'per_run_nft_pinned_tcp_endpoint',
            'network_enforcement_qualification':'candidate_requires_receiving_side_tests',
            'capture_limits':{'max_packets':256,'snaplen':2048,'complete_remote_visibility':False}}
        with (self.directory()/'result.json').open('x',encoding='utf-8') as stream:
            json.dump(result,stream)
        result['artifacts'] = self.artifacts()
        return result
'''
