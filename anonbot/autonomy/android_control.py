"""Fixed ADB write transport and UI-postcondition executor; no automatic approval."""
import base64
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import os
import shlex
import sqlite3
from xml.sax.saxutils import quoteattr

from .actions import ExecutionResult
from .android_png import fixture_png, validate_png
from .android_read import AdbClient, AndroidReadError, validate_package, validate_serial
from .android_ui import parse_hierarchy, select_node, validate_proposal
from .workflow_lock import RunnerLock, WorkflowBusy


@dataclass(frozen=True)
class UiFrame:
    png: bytes
    screen_sha256: str
    ui_sha256: str
    nodes: tuple
    width: int
    height: int
    serial: str
    package: str

    def metadata(self):
        return {key: getattr(self, key) for key in (
            'screen_sha256', 'ui_sha256', 'width', 'height', 'serial', 'package')}


class AdbControlClient(AdbClient):
    simulation = False

    def preview(self, serial, package):
        before, before_info = self.capture(serial, package)
        dimensions = validate_png(before)
        width, height = dimensions['width'], dimensions['height']
        raw = self._command('-s', validate_serial(serial), 'exec-out', 'uiautomator',
                            'dump', '/proc/self/fd/1')
        nodes, ui_hash = parse_hierarchy(raw, width, height)
        after, after_info = self.capture(serial, package)
        if sha256(before).digest() != sha256(after).digest() or before_info != after_info:
            raise AndroidReadError('screen_changed_during_preview')
        return UiFrame(before, sha256(before).hexdigest(), ui_hash, tuple(nodes), width, height, serial, package)

    def prepare_text(self, serial, mode):
        if mode == 'adbkeyboard':
            raw = self._command('-s', validate_serial(serial), 'shell', 'settings',
                                'get', 'secure', 'default_input_method')
            if raw.strip() not in (b'com.android.adbkeyboard/.AdbIME',
                                    b'com.android.adbkeyboard/com.android.adbkeyboard.AdbIME'):
                raise AndroidReadError('adbkeyboard_not_current_ime')

    def tap(self, serial, x, y):
        if type(x) is not int or type(y) is not int or not (0 <= x < 4096 and 0 <= y < 4096):
            raise AndroidReadError('invalid_tap_coordinate')
        self._command('-s', validate_serial(serial), 'shell', 'input', 'tap', str(x), str(y))

    def type_text(self, serial, text, mode):
        # Validate again at the transport boundary; never accept a raw shell command.
        validate_proposal({'operation': 'type', 'screen_sha256': '0' * 64, 'ui_sha256': '0' * 64,
                           'selector': {'resource_id': 'com.example.demo:id/edit'},
                           'text': text, 'expected_text': text, 'text_mode': mode}, 'com.example.demo')
        serial = validate_serial(serial)
        if mode == 'native':
            self._command('-s', serial, 'shell', 'input', 'text', shlex.quote(text.replace(' ', '%s')))
        else:
            encoded = base64.b64encode(text.encode('utf-8')).decode('ascii')
            self._command('-s', serial, 'shell', 'am', 'broadcast', '-a', 'ADB_INPUT_B64',
                          '-p', 'com.android.adbkeyboard', '--es', 'msg', encoded)


def save_frame(frame, directory, name):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + '.png')
    with path.open('xb') as handle:
        handle.write(frame.png)
        handle.flush()
        os.fsync(handle.fileno())
    return frame.metadata() | {'screenshot': str(path)}


def device_lock(serial, live):
    """Same local ADB serial is exclusive across all CLI task directories."""
    directory = Path(__file__).resolve().parents[2] / 'data' / 'android-device-locks'
    directory.mkdir(parents=True, exist_ok=True)
    key = ('live:' if live else 'fixture:') + validate_serial(serial)
    return RunnerLock(directory / (sha256(key.encode()).hexdigest() + '.lock'))


class AndroidControlExecutor:
    def __init__(self, client, *, live, serial, package, artifact_dir):
        if type(live) is not bool or client.simulation is not (not live):
            raise AndroidReadError('transport_mode_mismatch')
        self.client, self.live = client, live
        self.serial, self.package = validate_serial(serial), validate_package(package)
        self.artifact_dir = Path(artifact_dir)

    def execute(self, intent):
        try:
            with device_lock(self.serial, self.live):
                return self._execute(intent)
        except (WorkflowBusy, OSError):
            return ExecutionResult('failed', 'device_busy_no_write')

    def _execute(self, intent):
        attempted = False
        evidence = {'simulation': not self.live, 'verification_scope': 'local_ui',
                    'platform_delivery_verified': False}
        try:
            mode = 'live' if self.live else 'fixture'
            expected_scope = f'android-control:{mode}:{self.serial}:{self.package}'
            if (intent.actor_id != 'local-cli' or intent.scope_id != expected_scope
                    or intent.account_id != 'adb-local' or intent.target != self.serial):
                raise AndroidReadError('control_scope_mismatch')
            payload = validate_proposal(intent.payload, self.package)
            if intent.capability != 'android.' + payload['operation']:
                raise AndroidReadError('control_capability_mismatch')
            if payload['operation'] == 'type':
                self.client.prepare_text(self.serial, payload['text_mode'])
            before = self.client.preview(self.serial, self.package)
            if (before.screen_sha256 != payload['screen_sha256']
                    or before.ui_sha256 != payload['ui_sha256']):
                raise AndroidReadError('stale_control_frame')
            target = select_node(before.nodes, payload['selector'], self.package)
            x1, y1, x2, y2 = target['bounds']
            if target['password'] or not target['enabled'] or x1 >= x2 or y1 >= y2:
                raise AndroidReadError('target_not_interactable')
            if payload['operation'] == 'tap':
                if not target['clickable']:
                    raise AndroidReadError('target_not_clickable')
                if select_node(before.nodes, payload['post_selector'], self.package, optional=True):
                    raise AndroidReadError('postcondition_already_present')
            elif (not target['focused'] or target['class'] != 'android.widget.EditText'
                  or sum(n['focused'] and n['enabled'] and n['class'] == 'android.widget.EditText'
                         for n in before.nodes) != 1
                  or target['text'] == payload['expected_text']):
                raise AndroidReadError('input_target_not_ready')
            stem = mode + '-' + sha256(intent.intent_id.encode()).hexdigest()[:24]
            evidence['before'] = save_frame(before, self.artifact_dir, stem + '-before')
            attempted = True
            if payload['operation'] == 'tap':
                self.client.tap(self.serial, (x1 + x2) // 2, (y1 + y2) // 2)
            else:
                self.client.type_text(self.serial, payload['text'], payload['text_mode'])
            after = self.client.preview(self.serial, self.package)
            evidence['after'] = save_frame(after, self.artifact_dir, stem + '-after')
            selector = payload['post_selector'] if payload['operation'] == 'tap' else payload['selector']
            result = select_node(after.nodes, selector, self.package)
            if (not result['enabled'] or result['password'] or
                    (payload['operation'] == 'type' and result['text'] != payload['expected_text'])):
                raise AndroidReadError('postcondition_not_verified')
            return ExecutionResult('succeeded' if self.live else 'simulated', 'local_ui_postcondition_verified', evidence)
        except (AndroidReadError, OSError, ValueError, TypeError):
            # Once a write may have happened, never say it failed without effect.
            return ExecutionResult('unknown' if attempted else 'failed',
                                   'effect_requires_manual_check' if attempted else 'control_precondition_failed', evidence)


class FixtureControlClient:
    """Persistent synthetic UI; never launches ADB or contacts any account."""
    simulation = True

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('CREATE TABLE IF NOT EXISTS device (id INTEGER PRIMARY KEY CHECK(id=1), state INTEGER, text TEXT)')
            connection.execute("INSERT OR IGNORE INTO device VALUES (1,0,'')")

    def preview(self, serial, package):
        if serial != 'demo-emulator' or package != 'com.example.demo':
            raise AndroidReadError('fixture_target_mismatch')
        with closing(sqlite3.connect(self.path)) as connection, connection:
            state, text = connection.execute('SELECT state,text FROM device WHERE id=1').fetchone()
        elements = []
        for resource, kind, value in (('next', 'Button', 'Next'), ('edit', 'EditText', text),
                                      *([('done', 'TextView', 'Done')] if state else [])):
            attrs = {'resource-id': package + ':id/' + resource, 'package': package,
                     'class': 'android.widget.' + kind, 'text': value, 'bounds': '[1,2][30,40]',
                     'enabled': 'true', 'clickable': 'true', 'password': 'false',
                     'focused': 'true' if kind == 'EditText' else 'false'}
            elements.append('<node ' + ' '.join(k + '=' + quoteattr(v) for k, v in attrs.items()) + '/>')
        nodes, digest = parse_hierarchy(('<hierarchy rotation="0">' + ''.join(elements) + '</hierarchy>').encode(), 64, 96)
        png = fixture_png()
        return UiFrame(png, sha256(png).hexdigest(), digest, tuple(nodes), 64, 96, serial, package)

    def prepare_text(self, serial, mode):
        if serial != 'demo-emulator' or mode not in ('native', 'adbkeyboard'):
            raise AndroidReadError('fixture_input_mismatch')

    def tap(self, serial, x, y):
        if serial != 'demo-emulator' or (x, y) != (15, 21):
            raise AndroidReadError('fixture_tap_mismatch')
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('UPDATE device SET state=1 WHERE id=1')

    def type_text(self, serial, text, mode):
        self.prepare_text(serial, mode)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('UPDATE device SET text=text || ? WHERE id=1', (text,))
