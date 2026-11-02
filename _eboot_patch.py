#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Patch CHLCC PS3 EBOOT.ELF so Private-Use-Area characters map to a font cell.

Target function: 0x5E200 = UTF-16(BE) string -> game font-codepoint string.
This is the PS3 port of the PSV FUN_81032898 described in the article.

Signature (note the prologue swaps the registers!):
    conv(u16 *src /*r3*/, u16 *dst /*r4*/)
    prologue:  r5 = r4 ; r4 = r3 ; r3 = r5     =>  r4 = src, r3 = dst

Original per-character logic:
    halfwidth table @0x1CFCC8 (86 entries)   : codepoint = index + 0x8000
    fullwidth table @0x1CFD76 (2212 entries) : codepoint = index + 0x807F
    index 0 / not found in either table      : codepoint = 0x8000
    output = 2 big-endian bytes ; 0xFF terminates the string.

The renderer derives the atlas cell as   font_cell = codepoint - 0x8000, so
cell   0.. 85 = halfwidth table
cell  86..126 = unreachable gap
cell 127..2338 = fullwidth table

------------------------------------------------------------------------------
This patch rewrites ONLY the fullwidth-search + result block,
0x5E2A4 .. 0x5E304 (25 words, exactly the original size).  That block is
entered from exactly one place (0x5E290 `bne 0x5E2A4`) and falls through to the
untouched loop tail at 0x5E308.  Nothing else in the function is modified.

Added case:  if the character is in NEITHER table AND ch >= THRESHOLD (PUA),
             then codepoint = ch - 0x6000   =>   font_cell = ch - 0xE000.
U+E000+N therefore renders font cell N, so Chinese glyphs can be placed into
free cells of the atlas and referenced from TROP.SFM via PUA codepoints.

THRESHOLD IS DERIVED FROM THE CHARSET, NOT HARD-CODED.  The charset always
places a run of PUA "compound character" slots at low indices (and possibly a
few at the very end).  The original code maps every one of those to codepoint
0x8000 (cell 0), so we must keep them BELOW the threshold to stay 100 %
behaviour-compatible.  We therefore use

        THRESHOLD = 0xE000 + (highest charset index that is PUA) + 1

i.e. the first PUA codepoint that is NOT already present in the charset.  New
Chinese glyphs must be placed at cells >= (highest PUA index + 1) and referenced
with U+E000+cell.  Changing the charset length automatically moves the
threshold - nothing needs editing here.
------------------------------------------------------------------------------
"""
import os
import struct
import sys

sys.path.insert(0, r'e:\Committee of Zero\mgsfontgen\tools')
from capstone import Cs, CS_ARCH_PPC, CS_MODE_BIG_ENDIAN, CS_MODE_64

ELF_IN = r'e:\Committee of Zero\mgsfontgen\chlcc\EBOOT.ELF'
ELF_OUT = r'e:\Committee of Zero\mgsfontgen\chlcc\EBOOT_patched.ELF'

va2off = lambda v: v - 0x10000

# ------------------------------------------------------------------ encoders
def _dform(op, rD, rA, imm):
    return ((op << 26) | (rD << 21) | (rA << 16) | (imm & 0xFFFF)) & 0xFFFFFFFF

def li(rD, imm):        return _dform(14, rD, 0, imm)      # addi rD,0,imm
def lis(rD, imm):       return _dform(15, rD, 0, imm)      # addis rD,0,imm
def addic(rD, rA, imm): return _dform(12, rD, rA, imm)
def ori(rA, rS, u):     return _dform(24, rS, rA, u)
def cmpli(bf, rA, u):   return ((10 << 26) | ((bf & 7) << 23) | (rA << 16)
                                | (u & 0xFFFF)) & 0xFFFFFFFF
def cmpi(bf, rA, imm):  return ((11 << 26) | ((bf & 7) << 23) | (rA << 16)
                                | (imm & 0xFFFF)) & 0xFFFFFFFF

def bc(bo, bi, addr, target):
    return ((16 << 26) | ((bo & 31) << 21) | ((bi & 31) << 16)
            | ((target - addr) & 0xFFFC)) & 0xFFFFFFFF

def b(addr, target):
    return ((18 << 26) | ((target - addr) & 0x3FFFFFC)) & 0xFFFFFFFF

def rlwinm(rA, rS, sh, mb, me, rc=0):
    return ((21 << 26) | (rS << 21) | (rA << 16) | (sh << 11)
            | (mb << 6) | (me << 1) | rc) & 0xFFFFFFFF

def slwi(rA, rS, n):    return rlwinm(rA, rS, n, 0, 31 - n)

def lhzx(rD, rA, rB):
    return ((31 << 26) | (rD << 21) | (rA << 16) | (rB << 11) | (279 << 1)) & 0xFFFFFFFF

def lhz(rD, d, rA):     return _dform(40, rD, rA, d)
def sth(rS, d, rA):     return _dform(44, rS, rA, d)

def cmpw(rA, rB):
    return ((31 << 26) | (rA << 16) | (rB << 11)) & 0xFFFFFFFF

def addc(rD, rA, rB):
    return ((31 << 26) | (rD << 21) | (rA << 16) | (rB << 11) | (10 << 1)) & 0xFFFFFFFF

NOP = 0x60000000

# ------------------------------------------------------------- patch region
BASE   = 0x5E2A4          # first word we rewrite
STORE  = 0x5E308          # shared store + loop tail (NOT touched)
FW_HIT = 0x5E2F0
DEFAULT= 0x5E2E4

DEFAULT_CHARSET = r'e:\Committee of Zero\mgsfontgen\chlcc\Charset.utf8'
CHARSET = DEFAULT_CHARSET          # overridden by --charset / set_charset()


def charset_pua_threshold(path=None):
    if path is None:
        path = CHARSET
    """THRESHOLD = 0xE000 + (highest charset index holding a PUA char) + 1.

    Every PUA char already present in the charset stays strictly below the
    threshold, so it keeps mapping to cell 0 exactly like the original code.
    New glyphs go at cells >= threshold - 0xE000.
    Returns (threshold, highest_index, count_of_pua).
    """
    try:
        txt = open(path, 'rb').read().decode('utf-8')
    except OSError:
        # no charset available -> fall back to the plain 0xE000 boundary
        return 0xE000, -1, 0
    cs = [ord(c) for c in txt if c not in '\r\n']
    idx = [i for i, cp in enumerate(cs) if 0xE000 <= cp <= 0xF8FF]
    if not idx:
        return 0xE000, -1, 0
    return 0xE000 + max(idx) + 1, max(idx), len(idx)


THRESHOLD, PUA_LAST, PUA_COUNT = charset_pua_threshold()

NEW = {
    # ---- fullwidth table search (idx in r10, byte offset in r31) -----------
    0x5E2A4: li(10, 0),                        # li     r10, 0
    0x5E2A8: slwi(31, 10, 1),                  # slwi   r31, r10, 1
    0x5E2AC: lhzx(31, 6, 31),                  # lhzx   r31, r6, r31
    0x5E2B0: cmpw(12, 31),                     # cmpw   r12, r31
    0x5E2B4: bc(12, 2, 0x5E2B4, FW_HIT),       # beq    FW_HIT
    0x5E2B8: addic(10, 10, 1),                 # addic  r10, r10, 1
    0x5E2BC: slwi(31, 10, 1),                  # slwi   r31, r10, 1
    0x5E2C0: lhzx(31, 6, 31),                  # lhzx   r31, r6, r31
    0x5E2C4: cmpw(31, 5),                      # cmpw   r31, r5   (terminator?)
    0x5E2C8: bc(4, 2, 0x5E2C8, 0x5E2A8),       # bne    loop

    # ---- not in fullwidth table: PUA range test, else default cell 0 -------
    # Only U+E000..U+F8FF are redirected; U+F900..U+FFFF (CJK compat.) and
    # any other codepoint above THRESHOLD keep the ORIGINAL cell-0 result.
    0x5E2CC: cmpli(6, 12, THRESHOLD),          # cmplwi cr6, r12, THRESHOLD
    0x5E2D0: bc(12, 24, 0x5E2D0, DEFAULT),     # blt    cr6, DEFAULT
    0x5E2D4: cmpli(7, 12, 0xF8FF),             # cmplwi cr7, r12, 0xf8ff
    0x5E2D8: bc(12, 29, 0x5E2D8, DEFAULT),     # bgt    cr7, DEFAULT
    0x5E2DC: addic(11, 12, -0x6000),           # addic  r11, r12, -0x6000
    0x5E2E0: b(0x5E2E0, STORE),                # b      STORE
    DEFAULT: lis(11, 0),                       # lis    r11, 0
    0x5E2E8: ori(11, 11, 0x8000),              # ori    r11, r11, 0x8000
    0x5E2EC: b(0x5E2EC, STORE),                # b      STORE

    # ---- fullwidth hit: codepoint = index + 0x807F -------------------------
    FW_HIT: cmpi(1, 10, 0),                    # cmpwi  cr1, r10, 0
    0x5E2F4: bc(12, 6, 0x5E2F4, DEFAULT),      # beq    cr1, DEFAULT
    0x5E2F8: lis(11, 0),                       # lis    r11, 0
    0x5E2FC: ori(11, 11, 0x807F),              # ori    r11, r11, 0x807f
    0x5E300: addc(11, 10, 11),                 # addc   r11, r10, r11
    0x5E304: b(0x5E304, STORE),                # b      STORE
}


def set_charset(path):
    """Recompute THRESHOLD for a different charset and rebuild the patch words.

    Called automatically from main() when --charset differs from the default.
    """
    global CHARSET, THRESHOLD, PUA_LAST, PUA_COUNT
    CHARSET = path
    THRESHOLD, PUA_LAST, PUA_COUNT = charset_pua_threshold(path)
    NEW[0x5E2CC] = cmpli(6, 12, THRESHOLD)
    return THRESHOLD


# ------------------------------------------------- the 25 original words
EXPECT = {
    0x5E2A4: 0xA3E7FD76,   # lhz    r31, -0x28a(r7)
    0x5E2A8: 0x616A0000,   # ori    r10, r11, 0
    0x5E2AC: 0x7C1F2800,   # cmpw   r31, r5
    0x5E2B0: 0x4182002C,   # beq    0x5e2dc
    0x5E2B4: 0x797F0020,   # clrldi r31, r11, 0x20
    0x5E2B8: 0x7FE6FA2E,   # lhzx   r31, r6, r31
    0x5E2BC: 0x7C0CF800,   # cmpw   r12, r31
    0x5E2C0: 0x4182001C,   # beq    0x5e2dc
    0x5E2C4: 0x316B0002,   # addic  r11, r11, 2
    0x5E2C8: 0x314A0001,   # addic  r10, r10, 1
    0x5E2CC: 0x797F0020,   # clrldi r31, r11, 0x20
    0x5E2D0: 0x7FE6FA2E,   # lhzx   r31, r6, r31
    0x5E2D4: 0x7C1F2800,   # cmpw   r31, r5
    0x5E2D8: 0x4082FFDC,   # bne    0x5e2b4
    0x5E2DC: 0x555F083C,   # slwi   r31, r10, 1
    0x5E2E0: 0x3D600000,   # lis    r11, 0
    0x5E2E4: 0x2C8A0000,   # cmpwi  cr1, r10, 0
    0x5E2E8: 0x616B8000,   # ori    r11, r11, 0x8000
    0x5E2EC: 0x7FE6FA2E,   # lhzx   r31, r6, r31
    0x5E2F0: 0x7C0CF800,   # cmpw   r12, r31
    0x5E2F4: 0x40820014,   # bne    0x5e308
    0x5E2F8: 0x41860010,   # beq    cr1, 0x5e308
    0x5E2FC: 0x3D600000,   # lis    r11, 0
    0x5E300: 0x616B807F,   # ori    r11, r11, 0x807f
    0x5E304: 0x7D6A5814,   # addc   r11, r10, r11
}

# context words that must be untouched (guards against patching a wrong file)
CONTEXT = {
    0x5E200: 0xF821FFC1,   # stdu   r1, -0x40(r1)
    0x5E204: 0x60850000,   # ori    r5, r4, 0
    0x5E20C: 0x60640000,   # ori    r4, r3, 0
    0x5E210: 0x60A30000,   # ori    r3, r5, 0
    0x5E228: 0x41820108,   # beq    0x5e330
    0x5E234: 0x3109FCC8,   # addic  r8, r9, -0x338   (halfwidth  table 0x1CFCC8)
    0x5E238: 0x30C7FD76,   # addic  r6, r7, -0x28a   (fullwidth  table 0x1CFD76)
    0x5E27C: 0x4082FFD8,   # bne    0x5e254
    0x5E290: 0x40820014,   # bne    0x5e2a4   <-- the single entry into our block
    0x5E294: 0x3D600000,   # lis    r11, 0
    0x5E298: 0x616B8000,   # ori    r11, r11, 0x8000
    0x5E29C: 0x7D6A5814,   # addc   r11, r10, r11
    0x5E2A0: 0x48000068,   # b      0x5e308
    0x5E308: 0x30840002,   # addic  r4, r4, 2
    0x5E30C: 0x556AC63E,   # rlwinm r10, r11, 0x18, 0x18, 0x1f
    0x5E310: 0x78840020,   # clrldi r4, r4, 0x20
    0x5E314: 0x31830002,   # addic  r12, r3, 2
    0x5E318: 0x99430000,   # stb    r10, 0(r3)
    0x5E31C: 0x99630001,   # stb    r11, 1(r3)
    0x5E320: 0xA1440000,   # lhz    r10, 0(r4)
    0x5E324: 0x79830020,   # clrldi r3, r12, 0x20
    0x5E328: 0x7C0A2800,   # cmpw   r10, r5
    0x5E32C: 0x4082FF10,   # bne    0x5e23c
    0x5E330: 0x388000FF,   # li     r4, 0xff
    0x5E334: 0x98830000,   # stb    r4, 0(r3)
}

MD = Cs(CS_ARCH_PPC, CS_MODE_BIG_ENDIAN | CS_MODE_64)


def get(d, va):
    return struct.unpack_from('>I', d, va2off(va))[0]


def dis(d, va, n):
    buf = bytes(d[va2off(va):va2off(va) + 4 * n])
    for insn in MD.disasm(buf, va):
        print(f'  {insn.address:#010x}  {insn.bytes.hex()}  '
              f'{insn.mnemonic:9s} {insn.op_str}')


# ------------------------------------------------------------- table readers
def read_u16_table(d, va, limit=8192):
    o = va2off(va)
    out = []
    for i in range(limit):
        v = struct.unpack_from('>H', d, o + i * 2)[0]
        if v == 0:
            break
        out.append(v)
    return out


def _scan(tbl, ch):
    """Return the FIRST index whose entry == ch, else len(tbl)."""
    for i, v in enumerate(tbl):
        if v == ch:
            return i
    return len(tbl)


def make_sim(hw, fw, threshold):
    """Faithful model of 0x5E200.  threshold < 0 -> original (no PUA case)."""
    n_hw, n_fw = len(hw), len(fw)

    def sim(ch):
        i = _scan(hw, ch)
        if i < n_hw:
            return i
        j = _scan(fw, ch)
        if j < n_fw:
            if j == 0:
                return 0
            return 0x7F + j
        if threshold >= 0 and threshold <= ch <= 0xF8FF:
            return ch - 0xE000
        return 0
    return sim


def verify_no_regression(d):
    """Assert patched(ch) == original(ch) for EVERY char in the charset."""
    hw = read_u16_table(d, 0x1CFCC8)
    fw = read_u16_table(d, 0x1CFD76)
    orig = make_sim(hw, fw, -1)
    new = make_sim(hw, fw, THRESHOLD)
    txt = open(CHARSET, 'rb').read().decode('utf-8')
    cs = [ord(c) for c in txt if c not in '\r\n']
    bad = [(i, cp, orig(cp), new(cp)) for i, cp in enumerate(cs)
           if orig(cp) != new(cp)]
    print(f'\nzero-regression check over charset ({len(cs)} chars):')
    print(f'  halfwidth table {len(hw)} entries, fullwidth {len(fw)} entries')
    if bad:
        print(f'  !! {len(bad)} REGRESSIONS')
        for i, cp, o, n in bad[:20]:
            print(f'     idx {i} U+{cp:04X}: orig cell {o} -> new cell {n}')
        return False
    print('  OK - every charset char maps to the SAME cell as before')
    # also check a few new cells actually work
    for cell in (THRESHOLD - 0xE000, 3408, 3839):
        ch = 0xE000 + cell
        assert new(ch) == cell, (hex(ch), cell, new(ch))
    print(f'  OK - U+{THRESHOLD:04X}+N -> cell N '
          f'(e.g. U+{0xE000 + 3408:04X} -> 3408, U+{0xE000 + 3839:04X} -> 3839)')
    return True


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description='Patch EBOOT.ELF so PUA codepoints map to font cells.')
    ap.add_argument('--charset', default=DEFAULT_CHARSET,
                    help='charset the atlas is built from (drives THRESHOLD)')
    ap.add_argument('--in', dest='elf_in', default=ELF_IN,
                    help='input EBOOT.ELF (must be the pristine one)')
    ap.add_argument('--out', dest='elf_out', default=None,
                    help='output path (default: alongside the input, _patched)')
    args = ap.parse_args()

    if args.charset != CHARSET:
        set_charset(args.charset)

    elf_in = args.elf_in
    elf_out = args.elf_out
    if elf_out is None:
        stem, ext = os.path.splitext(elf_in)
        elf_out = stem + '_patched' + ext

    d = bytearray(open(elf_in, 'rb').read())
    print(f'input : {elf_in}  ({len(d)} bytes)')

    bad = [(va, w, get(d, va)) for va, w in EXPECT.items() if get(d, va) != w]
    if bad:
        print('!! original bytes do NOT match expectations:')
        for va, w, g in bad:
            print(f'   {va:#x}: expected {w:08X}  got {g:08X}')
        return 1

    bad = [(va, w, get(d, va)) for va, w in CONTEXT.items() if get(d, va) != w]
    if bad:
        print('!! surrounding context does NOT match (wrong binary?):')
        for va, w, g in bad:
            print(f'   {va:#x}: expected {w:08X}  got {g:08X}')
        return 1

    print(f'original bytes verified OK '
          f'({len(EXPECT)} patch words + {len(CONTEXT)} context words)')

    print(f'\ncharset        : {CHARSET}')
    print(f'PUA chars      : {PUA_COUNT} (highest index {PUA_LAST})')
    print(f'THRESHOLD      : 0x{THRESHOLD:04X}  '
          f'(U+E000+{THRESHOLD - 0xE000})')
    print(f'  -> cells 0..{THRESHOLD - 0xE000 - 1} keep the ORIGINAL behaviour')
    print(f'  -> cells {THRESHOLD - 0xE000}.. are addressable via U+{THRESHOLD:04X}+N')
    assert THRESHOLD - 0xE000 == PUA_LAST + 1
    assert THRESHOLD - 0xE000 <= 0x7FFF, 'PUA cell index must fit the 0x7F80 mask'

    print('\n=== BEFORE  0x5e2a4 .. 0x5e308 ===')
    dis(d, BASE, 25)

    for va, w in NEW.items():
        struct.pack_into('>I', d, va2off(va), w)

    print('\n=== AFTER   0x5e2a4 .. 0x5e308 ===')
    dis(d, BASE, 25)

    print('\n=== untouched entry + tail ===')
    dis(d, 0x5E290, 5)
    dis(d, STORE, 11)
    dis(d, 0x5E330, 6)

    if not verify_no_regression(d):
        return 1

    assert len(d) == 1903144, len(d)
    open(elf_out, 'wb').write(bytes(d))
    print(f'\nwritten: {elf_out} ({len(d)} bytes)')
    print(f'THRESHOLD used: 0x{THRESHOLD:04X}  (from {CHARSET})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
