"""Bounded, exact UI selectors and immutable-proposal validation; no transport."""
from hashlib import sha256
import json
import re
import xml.etree.ElementTree as ET

from .android_read import AndroidReadError, TEXT_LIMIT, validate_package


def _fail(reason):
    raise AndroidReadError(reason)


def _selector(value, package):
    if (type(value) is not dict or not {'resource_id'} <= value.keys()
            or not value.keys() <= {'resource_id', 'text'}):
        _fail('invalid_selector')
    resource = value['resource_id']
    if (not isinstance(resource, str) or not re.fullmatch(
            re.escape(package) + r':id/[A-Za-z_][A-Za-z0-9_]{0,127}', resource)):
        _fail('selector_package_mismatch')
    if 'text' in value and (not isinstance(value['text'], str) or len(value['text']) > 4000):
        _fail('invalid_selector_text')


def select_node(nodes, selector, package, *, optional=False):
    _selector(selector, package)
    matches = [n for n in nodes if n['resource_id'] == selector['resource_id']
               and n['package'] == package
               and ('text' not in selector or n['text'] == selector['text'])]
    if not matches and optional:
        return None
    if len(matches) != 1:
        _fail('target_missing_or_ambiguous')
    return matches[0]


def parse_hierarchy(raw, width, height):
    if (not isinstance(raw, bytes) or len(raw) > TEXT_LIMIT or b'<!' in raw
            or type(width) is not int or type(height) is not int
            or not 1 <= width <= 4096 or not 1 <= height <= 4096):
        _fail('invalid_ui_xml')
    start, end = raw.find(b'<hierarchy'), raw.rfind(b'</hierarchy>')
    if start < 0 or end < start:
        _fail('invalid_ui_xml')
    body = raw[start:end + len(b'</hierarchy>')]
    # Only bounded status text may surround one document; never select a second XML.
    prefix = re.sub(br'<\?xml\s+[^<>?]{1,200}\?>', b'', raw[:start], count=1)
    if b'<' in prefix:
        _fail('invalid_ui_xml')
    if b'<' in raw[end + len(b'</hierarchy>'):]:
        _fail('invalid_ui_xml')
    try:
        root = ET.fromstring(body.decode('utf-8'))
    except (ET.ParseError, UnicodeError, ValueError):
        _fail('invalid_ui_xml')
    if root.tag != 'hierarchy' or root.get('rotation') not in ('0', '1', '2', '3'):
        _fail('invalid_ui_hierarchy')
    nodes, canonical = [], []
    stack = [(child, 1, True, False) for child in reversed(list(root))]
    while stack:
        element, depth, parent_enabled, parent_password = stack.pop()
        attrs = element.attrib
        if (element.tag != 'node' or depth > 30 or len(nodes) >= 5000 or len(attrs) > 32
                or any(len(k) > 100 or len(v) > 4000 for k, v in attrs.items())):
            _fail('invalid_ui_node')
        flags = {}
        for key in ('enabled', 'clickable', 'focused', 'password'):
            if attrs.get(key) not in ('true', 'false'):
                _fail('invalid_ui_flag')
            flags[key] = attrs[key] == 'true'
        flags['enabled'] &= parent_enabled
        flags['password'] |= parent_password
        match = re.fullmatch(r'\[(\d{1,4}),(\d{1,4})\]\[(\d{1,4}),(\d{1,4})\]', attrs.get('bounds', ''))
        if not match:
            _fail('invalid_ui_bounds')
        x1, y1, x2, y2 = map(int, match.groups())
        if not (0 <= x1 <= x2 <= width and 0 <= y1 <= y2 <= height):
            _fail('invalid_ui_bounds')
        canonical.append([depth, sorted(attrs.items())])
        nodes.append({'resource_id': attrs.get('resource-id', ''), 'package': attrs.get('package', ''),
                      'class': attrs.get('class', ''), 'text': '' if flags['password'] else attrs.get('text', ''),
                      'bounds': [x1, y1, x2, y2], **flags})
        stack.extend((child, depth + 1, flags['enabled'], flags['password'])
                     for child in reversed(list(element)))
    if not nodes:
        _fail('empty_ui_hierarchy')
    encoded = json.dumps([root.attrib, canonical], sort_keys=True, ensure_ascii=True).encode()
    return nodes, sha256(encoded).hexdigest()


def validate_proposal(value, package):
    validate_package(package)
    common = {'operation', 'screen_sha256', 'ui_sha256', 'selector'}
    if type(value) is not dict or value.get('operation') not in ('tap', 'type'):
        _fail('invalid_control_proposal')
    required = common | ({'post_selector'} if value['operation'] == 'tap'
                         else {'text', 'expected_text', 'text_mode'})
    if value.keys() != required:
        _fail('invalid_control_fields')
    for name in ('screen_sha256', 'ui_sha256'):
        if not isinstance(value[name], str) or not re.fullmatch('[0-9a-f]{64}', value[name]):
            _fail('invalid_frame_hash')
    _selector(value['selector'], package)
    if value['operation'] == 'tap':
        _selector(value['post_selector'], package)
    else:
        for name, limit in (('text', 1000), ('expected_text', 4000)):
            text = value[name]
            if (not isinstance(text, str) or not 1 <= len(text) <= limit
                    or any(ord(c) < 32 or 127 <= ord(c) < 160 or 0xD800 <= ord(c) <= 0xDFFF
                           or c in '\u2028\u2029' for c in text)):
                _fail('invalid_input_text')
        if value['text_mode'] not in ('native', 'adbkeyboard'):
            _fail('invalid_text_mode')
        if value['text_mode'] == 'native' and (not value['text'].isascii() or '%' in value['text']):
            _fail('native_text_not_supported')
    # Return a detached JSON-compatible object, never a mutable caller reference.
    return json.loads(json.dumps(value, ensure_ascii=True))
