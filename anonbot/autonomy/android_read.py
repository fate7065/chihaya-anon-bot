"""ADB read operations with fixed commands and bounded child-process output."""
import os
from pathlib import Path
import re
import subprocess
import threading
import time


TEXT_LIMIT = 1024 * 1024
PNG_LIMIT = 12 * 1024 * 1024


class AndroidReadError(Exception):
    """Only static reason codes cross the adapter boundary."""


def validate_serial(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        raise AndroidReadError('invalid_device_serial')
    return value


def validate_package(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*', value) or len(value) > 200:
        raise AndroidReadError('invalid_package')
    return value


def run_bounded(argv, *, max_bytes=TEXT_LIMIT, timeout=12):
    """Run a trusted local executable, without a shell. Kill only this child on failure."""
    if type(max_bytes) is not int or not 1 <= max_bytes <= PNG_LIMIT:
        raise AndroidReadError('invalid_output_budget')
    if type(timeout) not in (int, float) or not 0 < timeout <= 30:
        raise AndroidReadError('invalid_timeout')
    buffers = [bytearray(), bytearray()]
    overflow = threading.Event()
    read_error = threading.Event()
    def read_pipe(stream, index, limit):
        try:
            with stream:
                while chunk := stream.read1(65536):
                    if len(buffers[index]) + len(chunk) > limit:
                        overflow.set()
                        return
                    buffers[index].extend(chunk)
        except OSError:
            read_error.set()
    process = None
    readers = []
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, shell=False,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for index, stream, limit in ((0, process.stdout, max_bytes), (1, process.stderr, 65536)):
            thread = threading.Thread(target=read_pipe, args=(stream, index, limit), daemon=True)
            readers.append(thread)
            thread.start()
        deadline = time.monotonic() + timeout
        while True:
            if overflow.is_set():
                raise AndroidReadError('output_limit')
            if read_error.is_set():
                raise AndroidReadError('output_read_failed')
            if process.poll() is not None and not any(thread.is_alive() for thread in readers):
                break
            if time.monotonic() >= deadline:
                raise AndroidReadError('command_timeout')
            overflow.wait(0.01)
        # Readers may finish between the loop's flag check and exit condition.
        if overflow.is_set():
            raise AndroidReadError('output_limit')
        if read_error.is_set():
            raise AndroidReadError('output_read_failed')
        if process.returncode != 0:
            raise AndroidReadError('adb_command_failed')
        return bytes(buffers[0])
    except OSError:
        raise AndroidReadError('adb_unavailable') from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            for thread in readers:
                thread.join(timeout=1)


def _decode(raw):
    try:
        return raw.decode('utf-8')
    except (AttributeError, UnicodeError):
        raise AndroidReadError('invalid_adb_text') from None


def parse_devices(raw):
    lines = _decode(raw).replace('\r', '').splitlines()
    if not lines or lines[0].strip() != 'List of devices attached':
        raise AndroidReadError('invalid_device_list')
    devices = []
    seen = set()
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 2 or len(devices) >= 100:
            raise AndroidReadError('invalid_device_list')
        serial = validate_serial(parts[0])
        if serial in seen:
            raise AndroidReadError('invalid_device_list')
        seen.add(serial)
        state = parts[1] if parts[1] in ('device', 'offline', 'unauthorized', 'recovery', 'sideload', 'bootloader') else 'unknown'
        item = {'serial': serial, 'state': state}
        for field in parts[2:]:
            key, separator, value = field.partition(':')
            if separator and key in ('product', 'model', 'device', 'transport_id') and re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', value):
                item[key] = value
        devices.append(item)
    return devices


def parse_foreground(text):
    pattern = (r'^\s*(?:mResumedActivity|topResumedActivity)\s*[:=]\s*ActivityRecord\{'
               r'[^}\r\n]*?\bu(\d+)\s+([A-Za-z_][A-Za-z0-9_.]*)/([A-Za-z0-9_.$]+)\s')
    matches = set(re.findall(pattern, text, re.MULTILINE))
    if len(matches) != 1:
        return {'status': 'unknown', 'reason': 'foreground_missing_or_ambiguous'}
    user, package, activity = matches.pop()
    validate_package(package)
    if len(activity) > 300 or len(user) > 8:
        raise AndroidReadError('invalid_foreground_response')
    return {'status': 'known', 'package': package, 'activity': activity, 'user_id': int(user)}


class AdbClient:
    def __init__(self, adb_path, *, runner=run_bounded):
        path = Path(adb_path)
        if (not path.is_absolute() or not path.is_file() or
                (os.name == 'nt' and path.suffix.lower() != '.exe')):
            raise AndroidReadError('adb_path_must_be_existing_absolute_executable')
        self.adb_path = str(path.resolve())
        self.runner = runner

    def _command(self, *args, max_bytes=TEXT_LIMIT):
        # A local socket ignores remote server environment variables and supports
        # ADB's normal local daemon startup. Explicit -H would treat it as remote.
        return self.runner([self.adb_path, '-L', 'tcp:5037', *args],
                           max_bytes=max_bytes, timeout=12)

    def devices(self):
        return {'operation': 'devices', 'devices': parse_devices(self._command('devices', '-l'))}

    def inspect(self, serial):
        serial = validate_serial(serial)
        state = _decode(self._command('-s', serial, 'get-state')).strip()
        if state != 'device':
            raise AndroidReadError('device_not_ready')
        foreground = parse_foreground(_decode(self._command(
            '-s', serial, 'shell', 'dumpsys', 'activity', 'activities')))
        return {'operation': 'inspect', 'serial': serial, 'state': state, 'foreground': foreground}

    def capture(self, serial, expected_package=None):
        serial = validate_serial(serial)
        if expected_package is not None:
            validate_package(expected_package)
        before = self.inspect(serial)
        foreground = before['foreground']
        if foreground['status'] != 'known':
            raise AndroidReadError('foreground_not_verified')
        if expected_package is not None and foreground['package'] != expected_package:
            raise AndroidReadError('expected_package_mismatch')
        raw = self._command('-s', serial, 'exec-out', 'screencap', '-p', max_bytes=PNG_LIMIT)
        after = self.inspect(serial)
        if before != after:
            raise AndroidReadError('foreground_changed')
        return raw, {'operation': 'snapshot', 'serial': serial, 'before': before, 'after': after}
