"""Stdlib-only tests; also runnable in Linux without the MCP dependencies.

The real OpenSSL checks use owned loopback sockets, never an engagement target.
Mocked Docker tests do not qualify production network isolation.
"""
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from security_lab.diagnostic_host import DIAGNOSTIC_HOST_PROGRAM


def host_class(**overrides):
    namespace = dict(json=json,os=os,pathlib=__import__('pathlib'),re=re,
                     subprocess=subprocess,sys=sys,time=time,hashlib=__import__('hashlib'))
    namespace.update(overrides)
    exec(compile(DIAGNOSTIC_HOST_PROGRAM,'diagnostic-host','exec'),namespace)
    return namespace['DiagnosticHost']


def request(root):
    return {'container_name':'security-lab-pentest-'+'a'*24,'owner':'owned-nonce','run_id':'tls-owned',
            'runs_directory':str(root),'compose_directory':str(root),'kind':'tls','address':'127.0.0.1',
            'port':443,'deadline':time.time()+60,'argv':['/usr/bin/openssl','version'],
            'attempts':2,'attempt_seconds':1}


@unittest.skipUnless(sys.platform=='linux','Linux namespace launcher and pipe selectors')
class DiagnosticHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_network_is_drop_default_before_attachment_and_expires_without_conntrack_escape(self):
        host = host_class()(request(self.root))
        host.directory(create=True)
        rules, calls = [], []
        def firewall(value=None):
            if value is not None:
                rules.append(value)
            return json.dumps({'nftables':[{'table':{'family':'inet','name':'security_lab_diagnostic'}}]})
        host.firewall = firewall
        host.docker = lambda args,**kwargs: calls.append(args)
        host.owned = lambda role: {'NetworkSettings':{'Networks':{'owned-egress':{}}}}
        host.install_network('8.8.8.8',443,'owned-egress',time.time()+30)
        text = rules[0]
        self.assertEqual(text.count('policy drop'),3)
        self.assertIn('8.8.8.8 . 443 timeout ',text)
        self.assertIn('ct state established ip saddr . tcp sport @endpoint',text)
        self.assertIn('ct state related meta l4proto { icmp, ipv6-icmp }',text)
        self.assertNotIn('ct state established accept',text)
        self.assertEqual(calls[0][:3],['network','disconnect','none'])
        policy = json.loads((host.directory()/'network-policy.json').read_text())
        self.assertFalse(policy['independent_receiving_side_qualified'])

    def test_network_readback_change_prevents_capture_and_execution(self):
        host = host_class()(request(self.root))
        host.directory(create=True)
        count = 0
        def firewall(value=None):
            nonlocal count
            if value is not None:
                return ''
            count += 1
            return json.dumps({'nftables':[{'table':{'name':str(count)}}]})
        host.firewall = firewall
        host.docker = lambda *a,**k: ''
        host.owned = lambda role: {'NetworkSettings':{'Networks':{'owned-egress':{}}}}
        with self.assertRaisesRegex(RuntimeError,'readback changed'):
            host.install_network('8.8.8.8',443,'owned-egress',time.time()+30)
        self.assertFalse((host.directory()/'network-policy.json').exists())

    def test_foreign_container_is_never_stopped_or_removed(self):
        host = host_class()(request(self.root))
        calls = []
        def docker(args,**kwargs):
            calls.append(args)
            if args[0] == 'ps':
                return 'foreign-id'
            return json.dumps([{'Name':'/'+host.name,'Config':{'Labels':{}}}])
        host.docker = docker
        with self.assertRaisesRegex(RuntimeError,'ownership mismatch'):
            host.close()
        self.assertFalse(any(args[0] in {'stop','rm'} for args in calls))

    def test_capture_log_failure_still_removes_owned_containers(self):
        host = host_class()(request(self.root))
        host.directory(create=True)
        containers = {'capture':{'Id':'capture-id','State':{'Running':False}}}
        host.owned = lambda role: containers.get(role)
        def docker(args,**kwargs):
            if args[0] == 'logs':
                raise RuntimeError('simulated log failure')
            if args[0] == 'rm':
                containers.clear()
            return ''
        host.docker = docker
        result = host.close()
        self.assertTrue(result['access_closed'])
        self.assertEqual(result['capture_error'],'capture_artifact_collection_failed')
        self.assertFalse(containers)

    def test_observer_mounts_only_fresh_pcap_and_failed_start_restores_private_mode(self):
        host = host_class()(request(self.root))
        host.directory(create=True)
        host.image = 'sha256:'+'a'*64
        calls=[]
        def docker(argv,**kwargs):
            calls.append(argv)
            raise RuntimeError('simulated observer launch failure')
        host.docker=docker
        with self.assertRaisesRegex(RuntimeError,'observer launch failure'):
            host.capture('8.8.8.8',443,time.time()+30)
        argv=calls[0]
        self.assertEqual(argv[argv.index('--mount')+1],
                         'type=bind,src='+str(host.directory()/'traffic.pcap')+',dst=/capture.pcap')
        self.assertIn('KILL',argv)
        self.assertNotIn('CHOWN',argv)
        self.assertEqual(argv[argv.index('-w')+1],'-')
        self.assertIn('fsize=1048576:1048576',argv)
        host.owned=lambda role:None
        result=host.close()
        self.assertEqual((host.directory()/'traffic.pcap').stat().st_mode & 0o777,0o600)
        self.assertEqual(result['capture_error'],'capture_artifact_incomplete')

    def test_host_artifacts_cannot_be_replaced_with_links(self):
        host = host_class()(request(self.root))
        host.directory(create=True)
        foreign = self.root/'foreign'
        foreign.write_text('not an artifact')
        (host.directory()/'traffic.pcap').symlink_to(foreign)
        with self.assertRaisesRegex(RuntimeError,'link refused'):
            host.artifacts()

    def pipeline(self,*,fail_capture=False,timeouts=False):
        base = host_class()
        class Harness(base):
            def __init__(self,p):
                super().__init__(p)
                self.events, self.launched = [], False
            def docker(self,args,**kwargs):
                if 'config' in args:
                    return json.dumps({'services':{'pentest-diagnostics':{}},
                                       'networks':{'tool-egress':{'name':'owned-egress'}}})
                self.launched = True
                return ''
            def owned(self,role):
                if not self.launched:
                    return None
                if role == 'capture':
                    return {'State':{'Running':True}}
                return {'State':{'Running':True},'Config':{'User':'1001:1001'},
                        'HostConfig':{'NetworkMode':'none','ReadonlyRootfs':True,'CapDrop':['ALL'],
                                      'SecurityOpt':['no-new-privileges:true']},'Image':'sha256:'+'a'*64}
            def install_network(self,*args):
                self.events.append('firewall_then_attach')
            def capture(self,*args):
                self.events.append('capture_ready')
                if fail_capture:
                    raise RuntimeError('capture readiness timeout')
            def attempt(self,argv,seconds,index):
                self.events.append('attempt-'+str(index))
                for suffix in ('stdout','stderr'):
                    (self.directory()/f'attempt-{index}.{suffix}').write_bytes(b'partial CLI bytes')
                return {'exit_code':124 if timeouts else 137,'tls_handshake_complete':not timeouts,
                        'preview_truncated':False,'stop_reason':'deadline' if timeouts else 'handshake_complete',
                        'retryable_timeout':timeouts}
            def close(self):
                self.events.append('close')
                return {'access_closed':True,'artifacts':self.artifacts(),'target_receiving_side_capture':False}
        return Harness(request(self.root))

    def test_full_host_path_requires_capture_before_probe_and_records_successful_handshake(self):
        host = self.pipeline()
        result = host.run()
        self.assertEqual(host.events,['firewall_then_attach','capture_ready','attempt-1','close'])
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['receipts'][0]['exit_code'],137)
        self.assertEqual(result['verified_vulnerabilities'],[])
        self.assertIn('result.json',{r['name'] for r in result['artifacts']})

    def test_capture_readiness_failure_never_runs_probe(self):
        host = self.pipeline(fail_capture=True)
        result = host.run()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['receipts'],[])
        self.assertEqual(host.events,['firewall_then_attach','capture_ready','close'])
        self.assertTrue(result['access_closed'])

    def test_timeout_retries_keep_separate_evidence_and_same_reservation(self):
        host = self.pipeline(timeouts=True)
        deadline = host.p['deadline']
        result = host.run()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['attempts_completed'],2)
        self.assertEqual(host.p['deadline'],deadline)
        names = {r['name'] for r in result['artifacts']}
        self.assertTrue({'attempt-1.stderr','attempt-2.stderr'} <= names)
        self.assertEqual(host.events.count('firewall_then_attach'),1)

    def test_command_output_limit_preserves_bounded_partial_files(self):
        real_popen = subprocess.Popen
        processes = []
        def local_popen(command,**kwargs):
            process = real_popen([sys.executable,'-c','import sys; sys.stdout.write("x"*1000000)'],**kwargs)
            processes.append(process)
            return process
        transport = SimpleNamespace(Popen=local_popen,PIPE=subprocess.PIPE,DEVNULL=subprocess.DEVNULL,
                                    TimeoutExpired=subprocess.TimeoutExpired)
        host = host_class(subprocess=transport)(request(self.root))
        host.directory(create=True)
        host.owned = lambda role: {'Id':'owned-process'}
        host.docker = lambda *a,**k: processes[-1].terminate()
        result = host.attempt(['/fixed/stock/tool'],5,1)
        self.assertEqual(result['stop_reason'],'output_limit')
        self.assertFalse(result['retryable_timeout'])
        self.assertEqual(result['output_bytes'],65536)
        self.assertEqual((host.directory()/'attempt-1.stdout').stat().st_size,65536)

    @unittest.skipUnless(os.environ.get('SECURITY_LAB_NFT_NAMESPACE_TEST')=='1',
                         'Opt-in root test in a new empty network namespace only')
    def test_actual_kernel_accepts_rules_and_endpoint_membership_expires(self):
        self.assertEqual(os.geteuid(),0)
        self.assertNotEqual(os.readlink('/proc/self/ns/net'),os.readlink('/proc/1/ns/net'))
        initial = json.loads(subprocess.check_output(['nft','-j','list','ruleset']))
        self.assertFalse([row for row in initial['nftables'] if 'metainfo' not in row])
        subprocess.run(['ip','link','set','lo','up'],check=True,timeout=2)
        host = host_class()(request(self.root))
        host.directory(create=True)
        def firewall(rules=None):
            command = ['nft','-f','-'] if rules is not None else ['nft','-j','list','table','inet','security_lab_diagnostic']
            return subprocess.run(command,input=rules,text=True,capture_output=True,check=True,timeout=2).stdout
        host.firewall = firewall
        host.docker = lambda *a,**k: ''
        host.owned = lambda role: {'NetworkSettings':{'Networks':{'owned-egress':{}}}}
        try:
            listener=socket.socket()
            listener.bind(('127.0.0.1',0))
            listener.listen(1)
            self.addCleanup(listener.close)
            with socket.create_connection(listener.getsockname(),timeout=1):
                listener.accept()[0].close()
            host.install_network('8.8.8.8',443,'owned-egress',time.time()+7)
            with socket.socket() as denied:
                denied.settimeout(.3)
                with self.assertRaises(ConnectionRefusedError):
                    denied.connect(listener.getsockname())
            policy = json.loads((host.directory()/'network-policy.json').read_text())
            self.assertTrue(policy['installed_readback']['nftables'])
            time.sleep(2.1)
            table = json.loads(firewall())
            sets = [row['set'] for row in table['nftables'] if 'set' in row]
            self.assertEqual(len(sets),1)
            self.assertFalse(sets[0].get('elem'))
        finally:
            subprocess.run(['nft','delete','table','inet','security_lab_diagnostic'],check=True,timeout=2)

    def exercise_stock_openssl(self,mode):
        if not shutil.which('openssl') or not shutil.which('timeout'):
            self.skipTest('Stock OpenSSL and coreutils required')
        key, cert = self.root/'key.pem', self.root/'cert.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                        '-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost',
                        '-keyout',str(key),'-out',str(cert)],check=True,capture_output=True,timeout=10)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert,key)
        listener = socket.socket()
        listener.bind(('127.0.0.1',0))
        listener.listen(1)
        listener.settimeout(4)
        port = listener.getsockname()[1]
        done, received = threading.Event(), []
        def serve():
            try:
                with listener.accept()[0] as client:
                    client.settimeout(4)
                    if mode == 'silent':
                        received.append(client.recv(65536))
                        done.wait(3)
                    else:
                        with context.wrap_socket(client,server_side=True):
                            done.wait(3)
            except (OSError,ssl.SSLError):
                pass
            finally:
                listener.close()
        server = threading.Thread(target=serve,daemon=True)
        server.start()
        real_popen = subprocess.Popen
        processes = []
        def local_popen(command,**kwargs):
            # Replace only Docker transport with local execution. The stock
            # timeout and exact OpenSSL argv still execute real CLI processes.
            self.assertEqual(command[:3],['docker','exec','security-lab-pentest-'+'a'*24])
            process = real_popen(command[3:],**kwargs)
            processes.append(process)
            return process
        transport = SimpleNamespace(Popen=local_popen,PIPE=subprocess.PIPE,DEVNULL=subprocess.DEVNULL,
                                    TimeoutExpired=subprocess.TimeoutExpired)
        host = host_class(subprocess=transport)(request(self.root))
        host.directory(create=True)
        host.owned = lambda role: {'Id':'local-fixture'}
        host.docker = lambda *a,**k: processes[-1].terminate()
        name = 'wrong.example.test' if mode=='bad_name' else 'localhost'
        argv = ['/usr/bin/openssl','s_client','-connect',f'127.0.0.1:{port}','-servername','localhost',
                '-verify_hostname',name,'-verify_return_error','-CAfile',str(cert),
                '-brief','-state','-msg','-debug','-ign_eof','-min_protocol','TLSv1.2','-max_protocol','TLSv1.3']
        try:
            row = host.attempt(argv,2,1)
        finally:
            done.set()
            server.join(timeout=5)
        self.assertTrue((host.directory()/'attempt-1.stderr').is_file())
        if mode == 'silent':
            self.assertEqual(row['stop_reason'],'deadline')
            self.assertFalse(row['tls_handshake_complete'])
            self.assertTrue(received and received[0].startswith(b'\x16\x03'))
            self.assertGreater(row['output_bytes'],0)
        elif mode == 'bad_name':
            self.assertNotEqual(row['exit_code'],0)
            self.assertFalse(row['tls_handshake_complete'])
            self.assertNotEqual(row['stop_reason'],'deadline')
        else:
            self.assertTrue(row['tls_handshake_complete'],row)
            self.assertEqual(row['stop_reason'],'handshake_complete')
            self.assertLess(row['finished_at']-row['started_at'],2)

    def test_real_openssl_handshake_completes_without_waiting_for_deadline(self):
        self.exercise_stock_openssl('success')

    def test_real_openssl_silent_server_keeps_partial_clienthello_evidence(self):
        self.exercise_stock_openssl('silent')

    def test_real_openssl_hostname_error_is_not_success_or_timeout(self):
        self.exercise_stock_openssl('bad_name')


if __name__ == '__main__':
    unittest.main(verbosity=2)
