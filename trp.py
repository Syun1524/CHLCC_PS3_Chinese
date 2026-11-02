"""TROPHY.TRP packer/unpacker (PS3 / PSV trophy package).

Format (all big-endian), verified against CHLCC NPWR03263_00 TROPHY.TRP:

  0x00  8    magic / signature (random; e.g. dc a2 4d 00 00 00 00 01)
  0x08  4    zero
  0x0c  u32  total file size
  0x10  u32  entry count
  0x14  u32  entry size (0x40)
  0x18  ...  zero padding up to 0x40

  entry table from 0x40, each 0x40 bytes:
    +0x00  0x20  ASCII filename, NUL padded
    +0x20  4     zero
    +0x24  u32   absolute file offset (16-byte aligned)
    +0x28  4     zero
    +0x2c  u32   file size (NOT aligned)
    +0x30  0x10  unknown (00 00 00 01 00... for SFM entries, zero for PNG)

Commands:
    python trp.py list    <trp>
    python trp.py extract <trp> <outdir>
    python trp.py pack    <indir> <out> [--order-from <trp>]
    python trp.py set     <trp> <name> <newfile> <out>

`pack` recomputes every offset/size from scratch (keeps the original order if
`--order-from` is given).  `set` is the minimal-diff mode: every other entry
keeps its original offset/size, and only the replaced file's payload is
appended at the end (this is the approach used in the root-none.pages.dev
article, and it keeps the diff to a handful of bytes).
"""

import os
import struct
import sys

ENTRY_SIZE = 0x40
TABLE_OFF = 0x40
ALIGN = 16


def align_up(v, a=ALIGN):
    return (v + a - 1) // a * a


# ---------------------------------------------------------------- read

def parse(data):
    """Return (header_prefix_bytes, [entry dicts]) from a TRP blob."""
    size = struct.unpack_from('>I', data, 0x0C)[0]
    count = struct.unpack_from('>I', data, 0x10)[0]
    esize = struct.unpack_from('>I', data, 0x14)[0]
    if esize != ENTRY_SIZE:
        raise ValueError(f'unexpected entry size {esize:#x}')
    if size != len(data):
        raise ValueError(f'header size {size} != actual {len(data)}')

    entries = []
    for i in range(count):
        b = TABLE_OFF + i * esize
        name = data[b:b + 0x20].split(b'\x00')[0].decode('ascii')
        off = struct.unpack_from('>I', data, b + 0x24)[0]
        fsz = struct.unpack_from('>I', data, b + 0x2C)[0]
        entries.append({
            'name': name,
            'offset': off,
            'size': fsz,
            'unknown': bytes(data[b + 0x30:b + 0x40]),
        })
    # everything before the table (magic, counters, padding)
    return bytearray(data[0:TABLE_OFF]), entries


def read(path):
    with open(path, 'rb') as f:
        return parse(f.read())


def _write_table(buf, entries):
    """Write count/entry-size + the entry table into `buf` (in place).

    `buf` must already be long enough to hold the whole table.
    """
    struct.pack_into('>I', buf, 0x10, len(entries))
    struct.pack_into('>I', buf, 0x14, ENTRY_SIZE)
    for i, e in enumerate(entries):
        b = TABLE_OFF + i * ENTRY_SIZE
        nm = e['name'].encode('ascii')
        if len(nm) > 0x20:
            raise ValueError(f'name too long: {e["name"]}')
        buf[b:b + 0x20] = nm.ljust(0x20, b'\x00')
        buf[b + 0x20:b + 0x24] = b'\x00' * 4
        struct.pack_into('>I', buf, b + 0x24, e['offset'])
        buf[b + 0x28:b + 0x2C] = b'\x00' * 4
        struct.pack_into('>I', buf, b + 0x2C, e['size'])
        unk = e.get('unknown') or b'\x00' * 0x10
        buf[b + 0x30:b + 0x40] = unk.ljust(0x10, b'\x00')[:0x10]


# ---------------------------------------------------------------- write

def build(entries, header_prefix):
    """Full repack: recompute every offset (16-aligned blocks).

    entries: [{name, data, unknown}]
    header_prefix: original bytes 0x00..0x40 (magic + counters + padding)
    """
    header = bytearray(header_prefix)
    header.extend(b'\x00' * max(0, TABLE_OFF - len(header)))
    header = header[:TABLE_OFF]
    header.extend(b'\x00' * (len(entries) * ENTRY_SIZE))

    cursor = align_up(len(header))
    header.extend(b'\x00' * (cursor - len(header)))

    body = bytearray()
    for e in entries:
        e['offset'] = cursor + len(body)
        e['size'] = len(e['data'])
        body += e['data']
        body += b'\x00' * (align_up(len(body)) - len(body))

    _write_table(header, entries)
    total = len(header) + len(body)
    struct.pack_into('>I', header, 0x0C, total)
    return bytes(header) + bytes(body)


def build_set(original, name, new_data):
    """Minimal-diff replace: keep every other entry exactly where it is.

    The new payload is appended 16-aligned at the very end of the file and
    only that one entry's offset/size (plus the header total size) change.
    """
    header_prefix, entries = parse(original)
    idx = next((i for i, e in enumerate(entries) if e['name'] == name), None)
    if idx is None:
        raise SystemExit(f'{name} not found in archive')

    blob = bytearray(original)
    # where the data area currently ends
    end = align_up(max(e['offset'] + e['size'] for e in entries))
    blob.extend(b'\x00' * max(0, end - len(blob)))

    entries[idx]['offset'] = end
    entries[idx]['size'] = len(new_data)

    blob += new_data
    blob += b'\x00' * (align_up(len(blob)) - len(blob))

    # table lives inside `blob`; only the magic/counters prefix is preserved
    blob[0:len(header_prefix)] = header_prefix
    _write_table(blob, entries)
    struct.pack_into('>I', blob, 0x0C, len(blob))
    return bytes(blob)


# ---------------------------------------------------------------- cli

def _order_from(path):
    _, entries = read(path)
    return [e['name'] for e in entries]


def cmd_list(trp):
    _, entries = read(trp)
    print(f'{len(entries)} entries')
    print(f'{"#":>3} {"name":<24} {"offset":>9} {"size":>9}  unknown')
    for i, e in enumerate(entries):
        print(f'{i:3d} {e["name"]:<24} {e["offset"]:#9x} {e["size"]:#9x}  '
              f'{e["unknown"].hex(" ")}')


def cmd_extract(trp, outdir):
    raw = open(trp, 'rb').read()
    _, entries = read(trp)
    os.makedirs(outdir, exist_ok=True)
    for e in entries:
        o, s = e['offset'], e['size']
        open(os.path.join(outdir, e['name']), 'wb').write(raw[o:o + s])
        print(f'extracted {e["name"]} ({s} bytes)')


def cmd_pack(indir, out, order_from=None):
    names = _order_from(order_from) if order_from else sorted(
        os.listdir(indir))
    prefix = read(order_from)[0] if order_from else bytearray(TABLE_OFF)
    entries = []
    for n in names:
        p = os.path.join(indir, n)
        if not os.path.isfile(p):
            raise SystemExit(f'missing {p}')
        entries.append({
            'name': n,
            'data': open(p, 'rb').read(),
            # keep the "00 00 00 01 ..." marker the original uses for SFM
            'unknown': (b'\x00\x00\x00\x01' + b'\x00' * 12
                        if n.upper().endswith('.SFM') else b'\x00' * 16),
        })
    blob = build(entries, prefix)
    open(out, 'wb').write(blob)
    print(f'wrote {out} ({len(blob)} bytes, {len(entries)} entries)')


def cmd_set(trp, name, newfile, out):
    blob = build_set(open(trp, 'rb').read(), name,
                     open(newfile, 'rb').read())
    open(out, 'wb').write(blob)
    _, entries = read(out)
    e = next(x for x in entries if x['name'] == name)
    print(f'wrote {out} ({len(blob)} bytes)')
    print(f'{name}: offset {e["offset"]:#x} size {e["size"]:#x}')


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 1
    cmd = argv[1]
    if cmd == 'list':
        cmd_list(argv[2])
    elif cmd == 'extract':
        cmd_extract(argv[2], argv[3])
    elif cmd == 'pack':
        of = None
        if '--order-from' in argv:
            of = argv[argv.index('--order-from') + 1]
        cmd_pack(argv[2], argv[3], of)
    elif cmd == 'set':
        cmd_set(argv[2], argv[3], argv[4], argv[5])
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
