"""Validate the bounded 8-bit RGB/RGBA, non-interlaced subset emitted by screencap."""
import struct
import zlib

from .android_read import AndroidReadError, PNG_LIMIT


SIGNATURE = b'\x89PNG\r\n\x1a\n'


def validate_png(raw):
    if type(raw) is not bytes or not 45 <= len(raw) <= PNG_LIMIT or not raw.startswith(SIGNATURE):
        raise AndroidReadError('invalid_png')
    offset, header, ended, idat_closed, seen_idat = 8, None, False, False, False
    compressed = bytearray()
    while offset + 12 <= len(raw):
        size = struct.unpack_from('!I', raw, offset)[0]
        kind = raw[offset + 4:offset + 8]
        end = offset + 12 + size
        if end > len(raw) or not all(65 <= c <= 90 or 97 <= c <= 122 for c in kind):
            raise AndroidReadError('invalid_png')
        data = raw[offset + 8:offset + 8 + size]
        crc = struct.unpack_from('!I', raw, offset + 8 + size)[0]
        if zlib.crc32(kind + data) != crc:
            raise AndroidReadError('invalid_png_crc')
        if header is None and kind != b'IHDR':
            raise AndroidReadError('invalid_png')
        if kind == b'IHDR':
            if header is not None or size != 13:
                raise AndroidReadError('invalid_png')
            header = struct.unpack('!IIBBBBB', data)
            width, height, depth, color, compression, filtering, interlace = header
            if (not 1 <= width <= 4096 or not 1 <= height <= 4096 or width * height > 16000000
                    or depth != 8 or color not in (2, 6) or (compression, filtering, interlace) != (0, 0, 0)):
                raise AndroidReadError('unsupported_png_format_or_dimensions')
        elif kind == b'IDAT':
            if idat_closed:
                raise AndroidReadError('invalid_png')
            seen_idat = True
            compressed.extend(data)
        elif kind == b'IEND':
            if size or end != len(raw) or not compressed:
                raise AndroidReadError('invalid_png')
            ended = True
            break
        else:
            if not kind[0] & 32:  # Unknown critical chunk: do not guess a decoder.
                raise AndroidReadError('unsupported_png_chunk')
            idat_closed = seen_idat
        offset = end
    if not ended:
        raise AndroidReadError('invalid_png')
    stride = width * (3 if color == 2 else 4) + 1
    expected = stride * height
    try:
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(compressed, expected + 1)
        if (len(decoded) != expected or not decoder.eof or decoder.unused_data
                or decoder.unconsumed_tail or any(decoded[row * stride] > 4 for row in range(height))):
            raise AndroidReadError('invalid_png_pixels')
    except zlib.error:
        raise AndroidReadError('invalid_png_pixels') from None
    return {'width': width, 'height': height}


def fixture_png():
    """Synthetic checkerboard for offline transport/file tests, never a device screenshot."""
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    rows = []
    for y in range(96):
        pixels = bytes(channel for x in range(64)
                       for channel in ((190, 195, 205) if (x // 8 + y // 8) % 2 else (90, 100, 120)))
        rows.append(b'\0' + pixels)
    header = struct.pack('!IIBBBBB', 64, 96, 8, 2, 0, 0, 0)
    return SIGNATURE + chunk(b'IHDR', header) + chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b'')
