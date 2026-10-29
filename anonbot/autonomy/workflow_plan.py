"""A fixed local read plan; external results can only supply a validated BV ID."""
from dataclasses import dataclass
from hashlib import sha256
import json
import re

from ..domain import ValidationError
from .actions import ActionIntent
from .android_read import AndroidReadError, validate_package, validate_serial
from .bilibili import BiliReadError, parse_bvid
from .jsonio import fields


TOOLS = {
    'bilibili.search': ({'query'}, {'page', 'limit'}),
    'bilibili.info': ({'bvid'}, set()),
    'bilibili.subtitles': ({'bvid'}, {'page', 'language'}),
    'android.devices': (set(), set()),
    'android.inspect': ({'serial'}, set()),
    'android.snapshot': ({'serial'}, {'expected_package'}),
}


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value):
        raise ValidationError('invalid workflow or step identifier')
    return value


def _arguments(tool, args):
    required, optional = TOOLS[tool]
    fields(args, required, optional)
    try:
        if 'query' in args:
            text = args['query']
            if not isinstance(text, str) or not text.strip() or len(text) > 100 or any(ord(c) < 32 for c in text):
                raise ValidationError('invalid search query')
        for key, maximum in (('page', 100 if tool == 'bilibili.search' else 50), ('limit', 20)):
            if key in args and (type(args[key]) is not int or not 1 <= args[key] <= maximum):
                raise ValidationError('invalid bounded read argument')
        if 'bvid' in args and parse_bvid(args['bvid']) != args['bvid']:
            raise ValidationError('plan requires a BV identifier')
        if 'language' in args and args['language'] is not None:
            if not isinstance(args['language'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', args['language']):
                raise ValidationError('invalid subtitle language')
        if 'serial' in args:
            validate_serial(args['serial'])
        if args.get('expected_package') is not None:
            validate_package(args['expected_package'])
    except (BiliReadError, AndroidReadError):
        raise ValidationError('invalid workflow read argument') from None


@dataclass(frozen=True, init=False)
class WorkflowPlan:
    _json: str

    def __init__(self, document):
        fields(document, ('version', 'workflow_id', 'steps'))
        if type(document['version']) is not int or document['version'] != 1:
            raise ValidationError('unsupported workflow version')
        _name(document['workflow_id'])
        steps = document['steps']
        if type(steps) is not list or not 1 <= len(steps) <= 10:
            raise ValidationError('workflow requires 1 to 10 steps')
        prior = {}
        for step in steps:
            fields(step, ('id', 'tool', 'args'), ('bindings',))
            step_id = _name(step['id'])
            tool = step['tool']
            if step_id in prior or not isinstance(tool, str) or tool not in TOOLS:
                raise ValidationError('duplicate step or unsupported read tool')
            bindings = step.get('bindings', {})
            fields(bindings, (), ('bvid',))
            if bindings and tool not in ('bilibili.info', 'bilibili.subtitles'):
                raise ValidationError('only BV ID bindings are supported')
            if type(step['args']) is not dict or set(step['args']) & set(bindings):
                raise ValidationError('arguments and bindings overlap or are invalid')
            checked = dict(step['args'])
            for key, binding in bindings.items():
                fields(binding, ('step', 'path'))
                source = binding['step']
                if not isinstance(source, str) or source not in prior:
                    raise ValidationError('binding must reference a prior step')
                path = binding['path']
                if prior[source] == 'bilibili.search':
                    valid = (type(path) is list and len(path) == 3 and path[0] == 'items'
                             and type(path[1]) is int and 0 <= path[1] < 20 and path[2] == 'bvid')
                else:
                    valid = prior[source] == 'bilibili.info' and path == ['bvid']
                if not valid:
                    raise ValidationError('unsupported result binding path')
                checked[key] = 'BV1GJ411x7h7'  # Validate remaining literal arguments before any I/O.
            _arguments(tool, checked)
            prior[step_id] = tool
        try:
            encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
            if len(encoded.encode('utf-8')) > 65536:
                raise ValidationError('workflow plan exceeds 65536 bytes')
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ValidationError('invalid workflow JSON') from None
        object.__setattr__(self, '_json', encoded)

    @property
    def document(self):
        return json.loads(self._json)

    def fingerprint(self, live):
        if type(live) is not bool:
            raise ValidationError('invalid workflow mode')
        return sha256((str(live) + self._json).encode('utf-8')).hexdigest()

    def intent(self, position, results, *, live):
        if type(position) is not int or not 0 <= position < len(self.document['steps']):
            raise ValidationError('invalid workflow step position')
        step = self.document['steps'][position]
        args = step['args']
        try:
            for key, binding in step.get('bindings', {}).items():
                value = results[binding['step']]
                for part in binding['path']:
                    value = value[part]
                args[key] = value
        except (KeyError, IndexError, TypeError):
            raise ValidationError('binding result unavailable') from None
        _arguments(step['tool'], args)
        tool = step['tool']
        target = ('public-search' if tool == 'bilibili.search' else
                  'connected-devices' if tool == 'android.devices' else args.get('bvid', args.get('serial')))
        digest = self.fingerprint(live)
        return ActionIntent('wf-' + sha256(f'{digest}:{position}'.encode()).hexdigest(),
                            'workflow-cli', 'workflow:' + self.document['workflow_id'] + ':' + digest[:16],
                            tool, 'anonymous' if tool.startswith('bilibili.') else 'adb-local', target, args)
