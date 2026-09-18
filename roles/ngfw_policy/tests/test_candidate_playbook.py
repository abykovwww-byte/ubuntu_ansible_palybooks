"""Run the real Ansible tasks on Linux with the API simulator, never the lab."""
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

from test_prepare import PrepareAPI, inventory_for

REPO = Path(__file__).resolve().parents[3]


@unittest.skipUnless(sys.platform == "linux" and shutil.which("ansible-playbook"),
                     "Actual playbook integration runs in Linux CI with Ansible")
class CandidatePlaybookTests(unittest.TestCase):
    def test_check_then_candidate_then_repeat_and_evidence(self):
        import grp
        with tempfile.TemporaryDirectory(prefix="ngfw-candidate-test-") as temp:
            root = Path(temp)
            role = root / "roles/ngfw_policy"
            shutil.copytree(REPO / "roles/ngfw_policy", role)
            state = root / "simulated-api-state.json"
            evidence = root / "evidence"
            evidence.mkdir()
            (evidence / "discovery.json").write_text(json.dumps(inventory_for(PrepareAPI())), encoding="utf-8")
            # Only the temp role is replaced. It invokes the real module entry
            # point and AnsibleModule but routes API calls to the test simulator.
            (role / "library/pt_ngfw_policy.py").write_text(textwrap.dedent('''\
                #!/usr/bin/python
                import json
                import os
                from pathlib import Path
                from ansible.module_utils.basic import AnsibleModule
                from test_prepare import PrepareAPI, p
                state = Path(os.environ["NGFW_TEST_STATE"])
                fake = PrepareAPI()
                if state.exists():
                    fake.__dict__.update(json.loads(state.read_text()))
                p.API = lambda config: fake
                try:
                    p.main()
                finally:
                    state.write_text(json.dumps({k: v for k, v in vars(fake).items() if k != "calls"}))
                '''), encoding="utf-8")
            variables = root / "vars.json"
            variables.write_text(json.dumps({
                "ansible_connection": "local", "ansible_become": False,
                "ansible_python_interpreter": sys.executable,
                "ngfw_policy_owner": getpass.getuser(),
                "ngfw_policy_group": grp.getgrgid(os.getgid()).gr_name,
                "ngfw_policy_output_dir": str(evidence),
                "ngfw_policy_candidate_config": {"acl_count": 18, "nat_count": 4},
                "ngfw_policy_login": "synthetic-ci-login",
                "ngfw_policy_password": "synthetic-ci-password",
                # The fixed workflow must not consume this as its operation.
                "ngfw_policy_mode": "publish",
            }), encoding="utf-8")
            inventory = root / "hosts.ini"
            inventory.write_text("[server]\nlocalhost\n", encoding="utf-8")
            env = {**os.environ, "ANSIBLE_ROLES_PATH": str(root / "roles"),
                   "PYTHONPATH": str(REPO / "roles/ngfw_policy/tests"), "NGFW_TEST_STATE": str(state)}
            cmd = ["ansible-playbook", "-i", str(inventory),
                   str(REPO / "playbooks/ngfw-policy-candidate.yml"), "-e", "@" + str(variables)]

            def run(*extra):
                result = subprocess.run([*cmd, *extra], cwd=REPO, env=env, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertNotIn("synthetic-ci-password", result.stdout)

            run("--check")
            self.assertFalse((evidence / "prepared-vars.json").exists())
            self.assertEqual(json.loads(state.read_text())["groups"][0]["subgroups"], [])
            for _ in range(2):
                run()
                self.assertEqual(json.loads((evidence / "check-result.json").read_text())["change_count"], 0)
                self.assertEqual(json.loads((evidence / "prepare-result.json").read_text())["state"], "prepared-candidate")
                self.assertFalse(json.loads((evidence / "prepared-vars.json").read_text())["ngfw_policy_config"]["dedicated_mngt_confirmed"])
            self.assertEqual(json.loads((evidence / "apply-result.json").read_text())["change_count"], 0)
            self.assertFalse((evidence / "publish-result.json").exists())


if __name__ == "__main__":
    unittest.main()
