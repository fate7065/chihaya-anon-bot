"""Read-only device actions and local screenshot artifacts, independent of any model."""
from hashlib import sha256
import os
from pathlib import Path

from .actions import ExecutionResult
from .android_png import fixture_png, validate_png
from .android_read import AndroidReadError, validate_package, validate_serial


class FixtureAdbClient:
    def devices(self):
        return {'operation': 'devices', 'devices': [
            {'serial': 'demo-emulator', 'state': 'device', 'model': 'OFFLINE_FIXTURE'}]}

    def inspect(self, serial):
        if validate_serial(serial) != 'demo-emulator':
            raise AndroidReadError('fixture_device_not_found')
        return {'operation': 'inspect', 'serial': serial, 'state': 'device',
                'foreground': {'status': 'known', 'package': 'com.example.demo',
                               'activity': '.MainActivity', 'user_id': 0}}

    def capture(self, serial, expected_package=None):
        before = self.inspect(serial)
        if expected_package is not None and expected_package != before['foreground']['package']:
            raise AndroidReadError('expected_package_mismatch')
        return fixture_png(), {'operation': 'snapshot', 'serial': serial,
                               'before': before, 'after': before}


class AndroidReadExecutor:
    def __init__(self, client=None, *, live=False, artifact_dir='data/android-artifacts'):
        if type(live) is not bool or (live and client is None):
            raise AndroidReadError('live_client_required')
        self.live = live
        self.client = client if client is not None else FixtureAdbClient()
        # The local caller configures this directory; action payloads cannot choose paths.
        self.artifact_dir = Path(artifact_dir).resolve()

    @staticmethod
    def _payload(intent):
        payload = intent.payload
        if intent.account_id != 'adb-local':
            raise AndroidReadError('unsupported_device_account')
        if intent.capability == 'android.devices':
            if payload or intent.target != 'connected-devices':
                raise AndroidReadError('invalid_devices_target_or_payload')
        elif intent.capability in ('android.inspect', 'android.snapshot'):
            allowed = {'serial', 'expected_package'} if intent.capability == 'android.snapshot' else {'serial'}
            if 'serial' not in payload or set(payload) - allowed:
                raise AndroidReadError('invalid_device_payload')
            if intent.target != validate_serial(payload['serial']):
                raise AndroidReadError('device_target_mismatch')
            if payload.get('expected_package') is not None:
                validate_package(payload['expected_package'])
        else:
            raise AndroidReadError('unsupported_device_capability')
        return payload

    def _save(self, raw, intent):
        dimensions = validate_png(raw)
        digest = sha256(raw).hexdigest()
        name = ('live-' if self.live else 'fixture-') + intent.fingerprint + '.png'
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifact_dir / name
        created = False
        try:
            with path.open('xb') as target:
                created = True
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
        except OSError:
            if created:
                path.unlink(missing_ok=True)
            raise AndroidReadError('artifact_write_failed_or_exists') from None
        return {'path': str(path), 'sha256': digest, 'bytes': len(raw), **dimensions}

    def execute(self, intent):
        try:
            payload = self._payload(intent)
            if intent.capability == 'android.devices':
                data = self.client.devices()
            elif intent.capability == 'android.inspect':
                data = self.client.inspect(**payload)
            else:
                raw, data = self.client.capture(**payload)
                data = {**data, 'artifact': self._save(raw, intent)}
        except AndroidReadError as exc:
            return ExecutionResult('failed', '安卓读取未完成', {'reason': str(exc), 'simulation': not self.live})
        except OSError:
            return ExecutionResult('failed', '本地截图存储不可用', {'reason': 'artifact_storage_unavailable', 'simulation': not self.live})
        data = {**data, 'simulation': not self.live, 'content_trust': 'untrusted_external',
                'source': 'adb_local_read' if self.live else 'm26a_offline_fixture'}
        return ExecutionResult('succeeded' if self.live else 'simulated', '安卓读取任务完成', data)
