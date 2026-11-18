"""Execute release orchestration against isolated records and a fake host boundary."""
import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import host_release as host
from test_release_artifact import manifest


class HostReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(host, 'ROOT', self.root))
        self.target = {'manifest': manifest(), 'schema': '1'*64, 'configuration': '2'*64}
        self.current = manifest()
        self.current['commit'] = 'b'*40
        for role, repository in host.artifacts.ROLES.items():
            self.current['images'][role]['tag'] = repository + ':' + 'b'*40
        self.write('active.json', self.current)
        self.write('healthy.json', self.target)
        self.commands: list[list[str]] = []
        self.stack.enter_context(patch.object(host, 'verify_images'))
        self.stack.enter_context(patch.object(host, 'compatibility', return_value=('1'*64, '2'*64)))
        self.stack.enter_context(patch.object(host, 'environment', return_value={}))
        self.stack.enter_context(patch.object(host, 'healthy'))
        self.stack.enter_context(patch.object(host, 'command', side_effect=self.run_command))

    def write(self, name, value):
        host.write_json(name, value)

    def run_command(self, arguments, env, capture=False):
        self.commands.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, stdout='')

    def test_preflight_does_not_change_services_or_active_release(self):
        host.rollback(False)
        self.assertEqual(self.commands, [])
        self.assertEqual(json.loads(host.state_path('active.json').read_text()), self.current)

    def test_apply_uses_retained_images_without_touching_database(self):
        host.rollback(True)
        self.assertEqual(json.loads(host.state_path('active.json').read_text()), self.target['manifest'])
        self.assertEqual(host.read_record('healthy.json'), self.target)
        joined = ' '.join(' '.join(row) for row in self.commands)
        self.assertIn('collectstatic', joined)
        self.assertIn('--wait --no-deps --no-build --pull never', joined)
        for forbidden in (' migrate ', ' down ', ' prune ', ' rebuild_product_index '):
            self.assertNotIn(forbidden, joined)
        for service in host.SERVICES:
            self.assertIn(service, self.commands[-1])

    def test_schema_changes_refuse_automatic_and_manual_default_recovery(self):
        with patch.object(host, 'compatibility', return_value=('3'*64, '2'*64)):
            with self.assertRaises(ValueError):
                host.rollback(True)
        self.assertEqual(self.commands, [])
        self.assertEqual(json.loads(host.state_path('active.json').read_text()), self.current)

    def test_config_changes_also_require_review(self):
        with patch.object(host, 'compatibility', return_value=('1'*64, '3'*64)):
            with self.assertRaises(ValueError):
                host.rollback(True)
        self.assertEqual(self.commands, [])

    def test_review_override_never_rewinds_schema(self):
        with patch.object(host, 'compatibility', return_value=('3'*64, '4'*64)):
            host.rollback(True, reviewed_compatibility=True)
        self.assertEqual(host.read_record('healthy.json')['schema'], '3'*64)
        self.assertFalse(any('migrate' in row for row in self.commands))

    def test_failed_health_never_marks_rollback_successful(self):
        with patch.object(host, 'healthy', side_effect=ValueError('not ready')):
            with self.assertRaises(ValueError):
                host.rollback(True)
        self.assertEqual(json.loads(host.state_path('active.json').read_text()), self.current)

    def test_missing_or_modified_image_blocks_service_changes(self):
        with patch.object(host, 'verify_images', side_effect=ValueError('missing image')):
            with self.assertRaises(ValueError):
                host.rollback(True)
        self.assertEqual(self.commands, [])

    def test_rollback_after_success_uses_previous_successful_release(self):
        self.write('active.json', self.target['manifest'])
        older = {**self.target, 'manifest': self.current}
        self.write('rollback.json', older)
        self.assertEqual(host.rollback_target(), older)

    def test_no_healthy_baseline_refuses_rollback(self):
        host.state_path('healthy.json').unlink()
        with self.assertRaises(FileNotFoundError):
            host.rollback(True)
        self.assertEqual(self.commands, [])

    def test_mark_healthy_retains_prior_success_and_retry_does_not_overwrite_it(self):
        host.mark_healthy()
        self.assertEqual(host.read_record('rollback.json'), self.target)
        self.assertEqual(host.read_record('healthy.json')['manifest'], self.current)
        host.mark_healthy()
        self.assertEqual(host.read_record('rollback.json'), self.target)

    def test_failed_deploy_recovers_but_still_fails_ci(self):
        calls = []
        def run(arguments, env, capture=False):
            calls.append(arguments)
            if arguments[0] == 'sh':
                raise subprocess.CalledProcessError(1, arguments)
            return subprocess.CompletedProcess(arguments, 0, stdout='')
        with patch.object(host.artifacts, 'activate'), patch.object(host, 'command', side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError):
                host.deploy(self.root/'fixture.tgz', '0'*64, 'b'*40)
        self.assertEqual(json.loads(host.state_path('active.json').read_text()), self.target['manifest'])
        self.assertGreater(len(calls), 1)

    def test_lock_and_symlink_guards(self):
        with host.release_lock():
            with self.assertRaises(ValueError):
                with host.release_lock():
                    self.fail('concurrent lock accepted')
        host.state_path('rollback.json').symlink_to(self.root/'external')
        with self.assertRaises(ValueError):
            host.read_record('rollback.json')

class HostBoundaryTests(unittest.TestCase):
    def test_image_preflight_checks_immutable_ids_and_host_architecture(self):
        value = manifest()
        with (patch.object(host, 'environment', return_value={}),
              patch.object(host, 'command', return_value=subprocess.CompletedProcess([], 0, stdout='linux/x86_64\n')),
              patch.object(host.artifacts, 'image_identity', return_value=(value['images']['api']['identity'], 'linux/amd64')) as inspect):
            host.verify_images(value)
            self.assertEqual([call.args[0] for call in inspect.call_args_list],
                             [row['identity'] for row in value['images'].values()])
        with (patch.object(host, 'environment', return_value={}),
              patch.object(host, 'command', return_value=subprocess.CompletedProcess([], 0, stdout='linux/arm64\n'))):
            with self.assertRaises(ValueError):
                host.verify_images(value)

    def test_different_image_identity_is_rejected(self):
        with (patch.object(host, 'environment', return_value={}),
              patch.object(host, 'command', return_value=subprocess.CompletedProcess([], 0, stdout='linux/amd64\n')),
              patch.object(host.artifacts, 'image_identity', return_value=('sha256:'+'9'*64, 'linux/amd64'))):
            with self.assertRaises(ValueError):
                host.verify_images(manifest())

    def test_fingerprints_detect_migrations_and_runtime_config_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root/'config.json'
            config.write_text(json.dumps({'POSTGRES_USER': 'fixture', 'POSTGRES_DB': 'fixture'}))
            (root/'allowed-ips.conf').write_text('allow 127.0.0.1;')
            for relative in ('deploy/hostgator/runtime.compose.yaml', 'deploy/hostgator/compose.yaml',
                             'deploy/hostgator/Caddyfile', 'server/nginx/nginx.conf'):
                path = root/relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture')
            with (patch.object(host, 'ROOT', root), patch.object(host, 'CONFIG', config),
                  patch.object(host, 'command', return_value=subprocess.CompletedProcess([], 0, stdout='core:0001\n')) as command):
                first = host.compatibility(manifest())
                self.assertIn('SELECT app', command.call_args.args[0][-1])
                command.return_value = subprocess.CompletedProcess([], 0, stdout='core:0001\ncore:0002\n')
                second = host.compatibility(manifest())
                self.assertNotEqual(first[0], second[0])
                self.assertEqual(first[1], second[1])
                (root/'server/nginx/nginx.conf').write_text('changed routing')
                self.assertNotEqual(second[1], host.compatibility(manifest())[1])

    def test_health_requires_running_target_images_not_only_a_public_200(self):
        value = manifest()
        with (patch.object(host, 'environment', return_value={}),
              patch.object(host, 'command') as command):
            command.side_effect = [subprocess.CompletedProcess([], 0, stdout=''),
                                   subprocess.CompletedProcess([], 0, stdout='fixture-container'),
                                   subprocess.CompletedProcess([], 0, stdout='sha256:'+'9'*64+' true')]
            with self.assertRaises(ValueError):
                host.healthy(value)
            self.assertFalse(any(call.args[0][0] == 'curl' for call in command.call_args_list))

    def test_failed_deployment_with_changed_schema_does_not_replace_services(self):
        case = HostReleaseTests()
        case.setUp()
        try:
            with (patch.object(host.artifacts, 'activate'),
                  patch.object(host, 'compatibility', return_value=('9'*64, '2'*64)),
                  patch.object(host, 'command', side_effect=subprocess.CalledProcessError(1, ['sh'])) as command):
                with self.assertRaises(subprocess.CalledProcessError):
                    host.deploy(case.root/'fixture.tgz', '0'*64, 'b'*40)
                self.assertEqual(command.call_count, 1)
            self.assertEqual(json.loads(host.state_path('active.json').read_text()), case.current)
        finally:
            case.doCleanups()


if __name__ == '__main__':
    unittest.main()
