"""Serialize HostGator releases and recover retained, verified application images.

Database rollback is intentionally unsupported. A compatibility override is an
operator assertion after reviewing migrations/configuration, never a DB rewind.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
from typing import Iterator, TypedDict

import release_artifact as artifacts

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path('/etc/proace/config.json')
SERVICES = ('api', 'notification-worker', 'media-worker', 'notification-stream', 'celery-beat', 'nginx')


class ReleaseRecord(TypedDict):
    manifest: artifacts.Manifest
    schema: str
    configuration: str


COMPOSE = ('docker', 'compose', '--env-file', '/dev/null', '-f', 'deploy/hostgator/runtime.compose.yaml')


def state_path(name: str) -> Path:
    directory = ROOT / '.release'
    if directory.is_symlink():
        raise ValueError('Release state must not use symbolic links.')
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / name
    if path.is_symlink():
        raise ValueError('Release records must not use symbolic links.')
    return path


def read_record(name: str) -> ReleaseRecord:
    value = json.loads(state_path(name).read_text())
    if not isinstance(value, dict):
        raise ValueError('Invalid retained release record.')
    manifest = value.get('manifest')
    if not isinstance(manifest, dict):
        raise ValueError('Invalid retained release manifest.')
    artifacts.checked_manifest(manifest, str(manifest.get('commit', '')))
    for field in ('schema', 'configuration'):
        if not isinstance(value.get(field), str) or not re.fullmatch(r'[a-f0-9]{64}', value[field]):
            raise ValueError('Missing retained release compatibility evidence.')
    return value


def write_json(name: str, value: dict[str, object] | artifacts.Manifest | ReleaseRecord) -> None:
    destination = state_path(name)
    descriptor, temporary = tempfile.mkstemp(prefix='release-', dir=destination.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def release_lock() -> Iterator[None]:
    descriptor = os.open(state_path('operation.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another deployment or rollback is running.') from None
        yield


def environment(manifest: artifacts.Manifest) -> dict[str, str]:
    public = json.loads(CONFIG.read_text())
    env = dict(os.environ)
    env.update({
        'API_IMAGE': manifest['images']['api']['identity'],
        'STREAM_IMAGE': manifest['images']['stream']['identity'],
        'POSTGRES_USER': str(public['POSTGRES_USER']),
        'POSTGRES_DB': str(public['POSTGRES_DB']),
    })
    return env


def command(arguments: list[str], env: dict[str, str], capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, cwd=ROOT, env=env, check=True, text=True,
                          capture_output=capture, timeout=900)


def compatibility(manifest: artifacts.Manifest) -> tuple[str, str]:
    env = environment(manifest)
    result = command([*COMPOSE, 'exec', '-T', 'postgres', 'psql', '-U', env['POSTGRES_USER'],
                      '-d', env['POSTGRES_DB'], '-At', '-c',
                      "SELECT app || ':' || name FROM django_migrations ORDER BY app, name"], env, True)
    schema = hashlib.sha256(result.stdout.encode()).hexdigest()
    digest = hashlib.sha256()
    # Exact current public runtime config and orchestration must match by default.
    for path in (ROOT / 'deploy/hostgator/runtime.compose.yaml', ROOT / 'deploy/hostgator/compose.yaml',
                 ROOT / 'deploy/hostgator/Caddyfile', CONFIG, CONFIG.parent / 'allowed-ips.conf',
                 *sorted((ROOT / 'server/nginx').glob('*.conf'))):
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return schema, digest.hexdigest()


def verify_images(manifest: artifacts.Manifest) -> None:
    host = command(['docker', 'info', '--format', '{{.OSType}}/{{.Architecture}}'], environment(manifest), True)
    platform = host.stdout.strip().replace('/x86_64', '/amd64').replace('/aarch64', '/arm64')
    if platform != manifest['platform']:
        raise ValueError('Retained release platform does not match this host.')
    for record in manifest['images'].values():
        # Inspect the immutable ID, not a tag that may have been moved.
        if artifacts.image_identity(record['identity']) != (record['identity'], manifest['platform']):
            raise ValueError('Retained release image is unavailable or changed.')


def healthy(manifest: artifacts.Manifest) -> None:
    env = environment(manifest)
    command([*COMPOSE, 'ps'], env)
    for service in SERVICES[:-1]:
        container = command([*COMPOSE, 'ps', '-q', service], env, True).stdout.strip()
        if not container or '\n' in container:
            raise ValueError('Expected one running container for each application service.')
        actual = command(['docker', 'inspect', '--format', '{{.Image}} {{.State.Running}}', container], env, True).stdout.strip()
        role = 'stream' if service == 'notification-stream' else 'api'
        if actual != manifest['images'][role]['identity'] + ' true':
            raise ValueError('Running application image does not match the release.')
    from urllib.parse import urlsplit
    origin = str(json.loads(CONFIG.read_text())['FRONTEND_API_ORIGIN'])
    address = urlsplit(origin)
    if address.scheme != 'https' or not address.hostname or address.username or address.password or address.query or address.fragment:
        raise ValueError('Configure a valid public HTTPS API origin.')
    command(['curl', '--fail', '--silent', '--show-error', '--max-time', '10',
             '--retry', '12', '--retry-delay', '5', origin.rstrip('/') + '/health/ready/'], env, True)


def mark_healthy() -> None:
    value = json.loads(state_path('active.json').read_text())
    manifest = artifacts.checked_manifest(value, value['commit'])
    verify_images(manifest)
    healthy(manifest)
    schema, configuration = compatibility(manifest)
    if state_path('healthy.json').exists():
        prior = read_record('healthy.json')
        if prior['manifest']['commit'] != manifest['commit']:
            write_json('rollback.json', prior)
    write_json('healthy.json', {'manifest': manifest, 'schema': schema, 'configuration': configuration})


def rollback_target() -> ReleaseRecord:
    current = json.loads(state_path('active.json').read_text())
    last_healthy = read_record('healthy.json')
    return last_healthy if current['commit'] != last_healthy['manifest']['commit'] else read_record('rollback.json')


def rollback(apply: bool, reviewed_compatibility: bool = False) -> None:
    target = rollback_target()
    manifest = artifacts.checked_manifest(target['manifest'], target['manifest']['commit'])
    verify_images(manifest)
    schema, configuration = compatibility(manifest)
    matches = schema == target['schema'] and configuration == target['configuration']
    if not matches and not reviewed_compatibility:
        raise ValueError('Schema or runtime configuration changed. Review compatibility with the retained release; '
                         'only then use --reviewed-compatibility. No database changes were made.')
    print('Rollback target:', manifest['commit'], '(application images only)')
    if not apply:
        print('Preflight passed; no services changed. Use --apply to restore this release.')
        return
    env = environment(manifest)
    # No migrate, build, pull, volume deletion, credential restore or DB restore.
    command([*COMPOSE, 'run', '--rm', '--no-deps', '--pull', 'never', 'api',
             'python', 'manage.py', 'collectstatic', '--noinput'], env)
    command([*COMPOSE, 'up', '-d', '--wait', '--no-deps', '--no-build', '--pull', 'never', *SERVICES], env)
    healthy(manifest)
    write_json('active.json', manifest)
    write_json('healthy.json', {**target, 'schema': schema, 'configuration': configuration})
    print('Rollback healthy. Database, orders, uploads and Vault secrets were preserved.')


def deploy(archive: Path, expected: str, commit: str, source: Path | None = None) -> None:
    # Source is uploaded into staging. Copy only while holding the host lock.
    if source is not None:
        artifacts.validate_commit(commit)
        staged = (ROOT / '.release/incoming' / (commit + '-source')).resolve()
        if source.resolve() != staged or source.is_symlink() or not source.is_dir():
            raise ValueError('Unexpected staged source directory.')
        artifacts.verify(archive, expected, commit)
        command(['rsync', '--archive', '--exclude=.release/', '--exclude=.git/',
                 '--exclude=.env*', str(staged) + '/', str(ROOT) + '/'], dict(os.environ))
    artifacts.ROOT = ROOT
    artifacts.activate(archive, expected, commit)
    try:
        command(['sh', 'deploy/hostgator/deploy.sh'], dict(os.environ))
        mark_healthy()
    except (OSError, ValueError, subprocess.SubprocessError):
        print('Deployment failed. Checking whether automatic application recovery is compatible.', flush=True)
        try:
            rollback(True)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            print('Automatic recovery unavailable or failed. Inspect service health and retained release records; '
                  'review schema/configuration compatibility before manual rollback.', flush=True)
        raise


def main() -> None:
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, help='Deployment checkout root for a staged release.')
    commands = parser.add_subparsers(dest='operation', required=True)
    release = commands.add_parser('deploy')
    release.add_argument('archive', type=Path)
    release.add_argument('digest')
    release.add_argument('commit')
    release.add_argument('--source', type=Path)
    undo = commands.add_parser('rollback')
    undo.add_argument('--apply', action='store_true', help='Restore services; default is read-only preflight.')
    undo.add_argument('--reviewed-compatibility', action='store_true', help='Operator has reviewed schema/config differences.')
    commands.add_parser('mark-healthy', help='Bootstrap a currently verified healthy release for future recovery.')
    args = parser.parse_args()
    if args.root is not None:
        ROOT = args.root.resolve(strict=True)
    try:
        with release_lock():
            if args.operation == 'deploy':
                deploy(args.archive, args.digest, args.commit, args.source)
            elif args.operation == 'rollback':
                rollback(args.apply, args.reviewed_compatibility)
            else:
                mark_healthy()
    except ValueError as exc:
        raise SystemExit('Release operation refused: ' + str(exc)) from None
    except (OSError, KeyError, subprocess.SubprocessError):
        raise SystemExit('Release operation failed. Check retained images, migration/config compatibility and service health. '
                         'Database recovery is never automatic.') from None


if __name__ == '__main__':
    main()
