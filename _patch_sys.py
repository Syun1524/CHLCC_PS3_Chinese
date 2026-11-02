"""Patch _system.scr: raise the font-atlas surface height.

Surface definitions found in _system.scr (CreateAlphaSurface, opcode 01 00 00):
  0x348: 01 00 00 | A0 4E 00 00 | A8 00 00 00 | A5 00 00 00   id=78  W=2048 H=1280
  0x357: 01 00 00 | A0 4F 00 00 | A8 00 00 00 | A5 00 00 00   id=79  W=2048 H=1280

H = 1280 (0x500) -> A5 00
H = 1920 (0x780) -> A7 80     (3840 chars = 64 cols x 60 rows x 32px)
"""
import sys

def enc3(v):
    assert 0 <= v < 0x2000, v
    return bytes([0xA0 | ((v >> 8) & 0x1F), v & 0xFF, 0x00, 0x00])

src = sys.argv[1]
dst = sys.argv[2]
new_h = int(sys.argv[3], 0) if len(sys.argv) > 3 else 1920

data = bytearray(open(src, 'rb').read())
old = enc3(1280)
new = enc3(new_h)
print(f'source : {src} ({len(data)} bytes)')
print(f'patch  : H 1280 {old.hex(" ")}  ->  {new_h} {new.hex(" ")}')

# locate every "A8 00 00 00 <H>" pattern (2048-wide surface)
hits = []
i = 0
while i < len(data) - 8:
    if data[i:i + 4] == enc3(2048) and data[i + 4:i + 8] == old:
        hits.append(i + 4)
    i += 1

print(f'found {len(hits)} x "2048 x 1280" surface(s) at: {[hex(h) for h in hits]}')
if len(hits) != 2:
    print('!! expected exactly 2 (id 78 and 79) -- aborting')
    sys.exit(1)

for h in hits:
    data[h:h + 4] = new

open(dst, 'wb').write(bytes(data))
print(f'wrote  : {dst}')

# verify
chk = open(dst, 'rb').read()
for h in hits:
    print(f'  {h:#06x}: {" ".join(f"{c:02x}" for c in chk[h:h+4])}')
