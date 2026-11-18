"""Run the actual host deployment shell script with isolated external commands."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
APPLICATION_SERVICES = {
    'nginx', 'api', 'notification-worker', 'media-worker',
    'notification-stream', 'celery-beat',
}


class DeploymentSequenceTests(unittest.TestCase):
    def run_deployment(self, failure: str = '') -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
        with tempfile.TemporaryDirectory(prefix='proace-deploy-sequence-') as directory:
            root = Path(directory)
            script = root / 'deploy/hostgator/deploy.sh'
            script.parent.mkdir(parents=True)
            configuration = root / 'configuration'
            configuration.mkdir()
            (configuration / 'config.json').write_text(json.dumps({
                'FRONTEND_API_ORIGIN': 'https://api.fixture.test',
                'POSTGRES_USER': 'fixture', 'POSTGRES_DB': 'fixture',
            }))
            secrets = root / 'runtime/api'
            secrets.mkdir(parents=True)
            for name in ('runtime.json', 'postgres-password', 'redis.conf', 'minio-user', 'minio-password'):
                (secrets / name).write_text('synthetic fixture')
            # Only filesystem locations change; execute the production shell logic.
            script.write_text((ROOT / 'deploy/hostgator/deploy.sh').read_text()
                              .replace('/etc/proace', str(configuration))
                              .replace('/run/proace-vault', str(root / 'runtime')))
            (root / 'scripts').mkdir()
            shutil.copyfile(ROOT / 'scripts/release_artifact.py', root / 'scripts/release_artifact.py')
            (root / '.release').mkdir()
            identity = 'sha256:' + '1' * 64
            (root / '.release/active.json').write_text(json.dumps({
                'commit': 'a' * 40, 'platform': 'linux/amd64',
                'images': {role: {'tag': repository + ':' + 'a' * 40, 'identity': identity}
                           for role, repository in {'api': 'proace-api', 'stream': 'proace-api-stream'}.items()},
            }))
            binaries = root / 'bin'
            binaries.mkdir()
            (binaries / 'python3').symlink_to(sys.executable)
            boundary = '''#!{python}
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
with open(os.environ['DEPLOY_TEST_LOG'], 'a') as stream:
    stream.write(json.dumps([name, *sys.argv[1:]]) + '\\n')
if name == 'findmnt':
    print('tmpfs')
elif name == 'docker' and sys.argv[1:3] == ['image', 'inspect']:
    print('sha256:' + '1' * 64 + ' linux/amd64')
if os.environ.get('DEPLOY_TEST_FAILURE') == 'migration' and 'migrate' in sys.argv:
    sys.exit(1)
if os.environ.get('DEPLOY_TEST_FAILURE') == 'stop' and 'stop' in sys.argv:
    sys.exit(1)
'''.format(python=sys.executable)
            for name in ('docker', 'findmnt', 'curl'):
                executable = binaries / name
                executable.write_text(boundary)
                executable.chmod(0o700)
            log = root / 'commands.jsonl'
            environment = {
                'PATH': str(binaries) + os.pathsep + '/usr/bin:/bin',
                'DEPLOY_TEST_LOG': str(log), 'DEPLOY_TEST_FAILURE': failure,
            }
            result = subprocess.run(['sh', str(script)], cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=30)
            commands = [json.loads(line) for line in log.read_text().splitlines()]
            return result, commands

    def test_old_application_is_stopped_before_migration_and_restart(self) -> None:
        result, commands = self.run_deployment()
        self.assertEqual(result.returncode, 0, result.stderr)
        stop = next(index for index, row in enumerate(commands) if 'stop' in row)
        migrate = next(index for index, row in enumerate(commands) if 'migrate' in row)
        restart = next(index for index, row in enumerate(commands)
                       if 'up' in row and 'notification-worker' in row)
        self.assertLess(stop, migrate)
        self.assertLess(migrate, restart)
        self.assertEqual(set(commands[stop][commands[stop].index('60') + 1:]), APPLICATION_SERVICES)
        self.assertFalse(any('down' in row or 'prune' in row for row in commands))
        self.assertTrue(any('curl' == row[0] for row in commands))

    def test_failed_migration_never_restarts_application_or_reports_success(self) -> None:
        result, commands = self.run_deployment('migration')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any('stop' in row for row in commands))
        self.assertTrue(any('migrate' in row for row in commands))
        self.assertFalse(any('up' in row and 'notification-worker' in row for row in commands))
        self.assertFalse(any('collectstatic' in row for row in commands))
        self.assertNotIn('deployment completed', result.stdout)

    def test_failed_quiescence_never_changes_schema(self) -> None:
        result, commands = self.run_deployment('stop')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any('migrate' in row for row in commands))
        self.assertNotIn('deployment completed', result.stdout)


if __name__ == '__main__':
    unittest.main()
