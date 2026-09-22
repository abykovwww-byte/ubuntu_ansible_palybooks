#!/usr/bin/env python3
"""Guest-only, bounded AuditD ablation with ordered B restoration. Never edits files.

apply runs one explicit command under C, then restores B in finally. SIGINT/TERM
terminate the command process group before restoring. SIGKILL/power loss require
manual restore from the private backup; persistent AuditD rules remain untouched.
"""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess

NETWORK = frozenset({'pt_siem_api_socket', 'pt_siem_api_connect', 'pt_siem_api_accept',
                     'pt_siem_api_listen', 'pt_siem_api_bind'})
PROFILES = {'C-EDR-NET': NETWORK, 'C-EDR-PROC': frozenset({'pt_siem_proc'})}
CONTROLS = ('enabled', 'failure', 'pid', 'rate_limit', 'backlog_limit', 'backlog_wait_time')
FILE_TYPES = {4096: 'fifo', 8192: 'character', 16384: 'dir', 24576: 'block',
              32768: 'file', 40960: 'link', 49152: 'socket'}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def text_rules(rules):
    return '\n'.join(rules) + ('\n' if rules else '')


def rule_argv(rule):
    """Render auditctl input without changing the recorded kernel rule text.

    auditctl -l may print numeric filetype, but libaudit accepts symbolic names.
    Both deletion and replay must address the same underlying file type.
    """
    argv = shlex.split(rule)
    for i in range(1, len(argv)):
        if argv[i-1] != '-F':
            continue
        match = re.fullmatch(r'(filetype(?:!=|=))(.+)', argv[i])
        if not match:
            continue
        value = match[2]
        if value in FILE_TYPES.values():
            continue
        try:
            number = int(value, 16 if value.lower().startswith('0x') else 10)
            name = FILE_TYPES[number]
        except (ValueError, KeyError):
            raise ValueError('unsupported filetype: exact replay cannot be planned') from None
        argv[i] = match[1] + name
    return argv


def parse_rules(text):
    if text.strip() == 'No rules':
        return []
    rules = [line for line in text.splitlines() if line.strip()]
    if len(rules) != len(set(rules)):
        raise ValueError('duplicate rules: exact reconstruction is not supported')
    for rule in rules:
        argv = rule_argv(rule)
        # Runtime auditctl output only, never directives/control files or shell input.
        if not argv or argv[0] not in ('-a', '-w'):
            raise ValueError('unsupported runtime rule: exact ordered restore cannot be planned')
        if any(token in argv for token in ('-D', '-e', '-f', '-b', '-R', '-A')):
            raise ValueError('control directive in runtime rules')
        scoped = any(token.startswith(('dir=/', 'path=/')) and token not in ('dir=/', 'path=/') for token in argv)
        all_calls = any(argv[i:i+2] == ['-S', 'all'] for i in range(len(argv)-1))
        excluded_packet = ('exclude' in argv[1].split(',')) if len(argv) > 1 and argv[0] == '-a' else False
        if ('NETFILTER_PKT' in rule and not excluded_packet) or (all_calls and not scoped):
            raise ValueError('unsafe broad/packet rule cannot be replayed')
        if argv[0] == '-a' and (len(argv) < 3 or not any(set(argv[1].split(',')) == {action, group}
                for action in ('always', 'never') for group in ('exit', 'exclude'))):
            raise ValueError('unsupported filter list; exact ordered restore unavailable')
    return rules


def keys(rule):
    argv, result = shlex.split(rule), set()
    for i, token in enumerate(argv):
        if token == '-k' and i + 1 < len(argv):
            result.update(argv[i+1].split('\x01'))
        if token.startswith('key='):
            result.update(token[4:].split('\x01'))
    return result


def plan(baseline, profile, expected_hash, candidate=None):
    rules = parse_rules(baseline['rules_text'])
    if digest(baseline['rules_text']) != expected_hash:
        raise ValueError('unexpected B rules hash; no changes')
    status = baseline['status']
    if any(type(status.get(k)) is not int for k in CONTROLS) or not status.get('pid'):
        raise ValueError('AuditD status incomplete; no changes')
    if status.get('enabled') != 1 or status.get('failure') not in (0, 1) or status.get('lost') != 0:
        raise ValueError('unsafe AuditD controls/loss; no changes')
    if not baseline.get('edr_active') or not baseline.get('auditd_active'):
        raise ValueError('EDR and AuditD must be active')
    if profile == 'B-EDR-CURRENT':
        return {'profile': profile, 'baseline_sha256': expected_hash, 'target_sha256': expected_hash,
                'removed': [], 'remaining': rules, 'diff': '', 'restore_from_index': len(rules)}
    if profile == 'D-EDR-CANDIDATE':
        if (not isinstance(candidate, dict) or candidate.get('baseline_sha256') != expected_hash
                or not candidate.get('ablation_run_ids') or not candidate.get('telemetry_acceptance_evidence')
                or not candidate.get('rationale') or not candidate.get('removed_keys')):
            raise ValueError('D requires evidence-backed candidate manifest for this B')
        selected = frozenset(candidate['removed_keys'])
        if not selected <= NETWORK | {'pt_siem_proc'}:
            raise ValueError('D may exclude only investigated NET/PROC keys')
    elif profile not in PROFILES:
        raise ValueError('only agreed NET/PROC ablations; A requires a clean control and D requires evidence')
    else:
        selected = PROFILES[profile]
    removed = [rule for rule in rules if keys(rule) & selected]
    if not removed or any(keys(rule) - selected for rule in removed):
        raise ValueError('target absent or mixed-key rule; exact approved removal unavailable')
    remaining = [r for r in rules if r not in removed]
    # Both shapes must be round-trippable before any mutation.
    parse_rules(text_rules(remaining))
    return {'profile': profile, 'baseline_sha256': expected_hash,
            'target_sha256': digest(text_rules(remaining)), 'removed': removed,
            'remaining': remaining, 'restore_from_index': rules.index(removed[0]),
            'restore_strategy': 'delete exact surviving suffix in reverse; append original suffix in order',
            'diff': ''.join(difflib.unified_diff(baseline['rules_text'].splitlines(True),
                        text_rules(remaining).splitlines(True), fromfile='B-EDR-CURRENT', tofile=profile))}


class Kernel:
    def call(self, args):
        completed = subprocess.run(args, text=True, capture_output=True, timeout=10, check=False,
                                   env=os.environ | {'LC_ALL': 'C'})
        if completed.returncode:
            raise RuntimeError(args[0] + ' failed; exit ' + str(completed.returncode))
        return completed.stdout

    def active(self, service):
        value = subprocess.run(['systemctl', 'is-active', service], text=True, capture_output=True, timeout=10)
        return value.returncode == 0 and value.stdout.strip() == 'active'

    def inventory(self):
        rules = self.call(['auditctl', '-l'])
        status_text = self.call(['auditctl', '-s'])
        status = {}
        for line in status_text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit():
                status[parts[0]] = int(parts[1])
        files = [Path('/etc/audit/auditd.conf'), Path('/etc/audit/audit.rules'),
                 Path('/etc/ngfw-audit-forwarder/rsyslog.conf')]
        files.extend(sorted(Path('/etc/audit/rules.d').glob('*')))
        hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}
        return {'rules_text': rules, 'rules_sha256': digest(rules), 'status_text': status_text,
                'status': status, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'file_hashes': hashes,
                'edr_active': self.active('vxagent'), 'auditd_active': self.active('auditd')}

    def delete(self, rule):
        argv = rule_argv(rule)
        argv[0] = {'-a': '-d', '-w': '-W'}[argv[0]]
        self.call(['auditctl'] + argv)

    def add(self, rule):
        self.call(['auditctl'] + rule_argv(rule))


def unchanged(before, after):
    if before['boot_id'] != after['boot_id'] or before['file_hashes'] != after['file_hashes']:
        raise ValueError('boot or persistent config changed; manual recovery required')
    if any(before['status'].get(k) != after['status'].get(k) for k in CONTROLS):
        raise ValueError('fixed AuditD controls changed; manual recovery required')


def restore(kernel, baseline):
    current = kernel.inventory()
    unchanged(baseline, current)
    original = parse_rules(baseline['rules_text'])
    active = parse_rules(current['rules_text'])
    # Permit only an ordered subsequence of the saved B. Never remove foreign rules.
    if any(r not in original for r in active) or active != [r for r in original if r in active]:
        raise ValueError('unexpected active rule/order drift; refusing uncontrolled restore')
    prefix = 0
    while prefix < min(len(original), len(active)) and original[prefix] == active[prefix]:
        prefix += 1
    for rule in reversed(active[prefix:]):
        kernel.delete(rule)
    for rule in original[prefix:]:
        kernel.add(rule)
    final = kernel.inventory()
    unchanged(baseline, final)
    if final['rules_text'] != baseline['rules_text']:
        raise RuntimeError('restore verification FAILED: active B order/content differs')
    return {'restored': True, 'rules_sha256': final['rules_sha256'], 'boot_id': final['boot_id']}


def private_backup(path, baseline):
    path = Path(path)
    if not path.is_absolute() or any((p / '.git').exists() for p in [path.parent, *path.parents]):
        raise ValueError('backup must be an absolute private path outside Git')
    if not path.parent.is_dir() or path.parent.is_symlink() or path.parent.stat().st_mode & 0o077:
        raise ValueError('backup parent must exist with private 0700 permissions')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        json.dump(baseline, out, indent=2)
        out.flush()
        os.fsync(out.fileno())


def apply(kernel, baseline, change, callback):
    fresh = kernel.inventory()
    unchanged(baseline, fresh)
    if fresh['rules_text'] != baseline['rules_text']:
        raise ValueError('B changed since plan; no changes')
    try:
        for rule in change['removed']:
            kernel.delete(rule)
        active = kernel.inventory()
        unchanged(baseline, active)
        if active['rules_text'] != text_rules(change['remaining']):
            raise RuntimeError('active C differs from planned ordered rule set')
        return callback()
    finally:
        # A repeated SIGINT/TERM must not interrupt the restoration itself.
        previous = {}
        try:
            if __import__('threading').current_thread() is __import__('threading').main_thread():
                for sig in (signal.SIGINT, signal.SIGTERM):
                    previous[sig] = signal.signal(sig, signal.SIG_IGN)
            restore(kernel, baseline)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def run_bounded(command, timeout):
    child = subprocess.Popen(command, start_new_session=True)
    try:
        return child.wait(timeout=timeout)
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['inventory', 'plan', 'apply', 'verify', 'restore'])
    parser.add_argument('--profile', choices=['B-EDR-CURRENT', *PROFILES, 'D-EDR-CANDIDATE', 'A-NO-EDR'])
    parser.add_argument('--candidate-file', type=Path)
    parser.add_argument('--expected-hash')
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--timeout', type=int, default=1800)
    parser.add_argument('--command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if os.name != 'posix' or os.geteuid() != 0:
        parser.error('run only as root inside the isolated NGFW guest')
    import fcntl
    os.umask(0o077)
    with open('/run/ngfw-audit-profile.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        kernel = Kernel()
        baseline = kernel.inventory()
        if args.action == 'inventory':
            print(json.dumps(baseline, indent=2))
            return 0
        if args.action in ('verify', 'restore'):
            if not args.backup or args.backup.is_symlink() or args.backup.stat().st_mode & 0o077:
                parser.error('private 0600 backup required')
            baseline = json.loads(args.backup.read_text())
            if args.action == 'restore':
                print(json.dumps(restore(kernel, baseline)))
            else:
                current = kernel.inventory()
                unchanged(baseline, current)
                if current['rules_text'] != baseline['rules_text']:
                    raise ValueError('B content/order not restored')
                print(json.dumps({'restored': True, 'rules_sha256': current['rules_sha256']}))
            return 0
        candidate = json.loads(args.candidate_file.read_text()) if args.candidate_file else None
        change = plan(baseline, args.profile, args.expected_hash, candidate)
        print(json.dumps(change, indent=2), flush=True)
        if args.action == 'plan':
            return 0
        if not args.backup or not args.command or not 1 <= args.timeout <= 7200:
            parser.error('apply needs new private backup, bounded timeout and explicit --command')
        private_backup(args.backup, baseline)
        def interrupted(*_):
            # Ignore repeated operator signals while the child stops and B is restored.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            raise KeyboardInterrupt('operator interruption; restoring B')
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, interrupted)
        return apply(kernel, baseline, change, lambda: run_bounded(args.command, args.timeout))


if __name__ == '__main__':
    raise SystemExit(main())
