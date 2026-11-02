#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
反向打包: 灰度PNG -> 8bpp索引位图(.bin)
格式: 头8字节 = 宽(u16) 高(u16) 位深=8(u16) 版本(u16), 小端; 之后 = 宽*高 字节索引
灰度值直接作为索引值 (0-255)

用法:
  python png2bin.py input.png                      # 版本号默认1
  python png2bin.py input.png -v 2                 # 指定版本号/标志
  python png2bin.py input.png -o out.bin
  python png2bin.py <目录> -o <输出目录>            # 批量模式
  python png2bin.py <目录> -o <输出目录> -v 2       # 批量+指定版本
"""
import struct, argparse
from pathlib import Path
from PIL import Image


def png_to_bin(img: Image.Image, version: int) -> bytes:
    """把一张图转成 bin 字节流"""
    # 统一转为灰度 (L 模式), 每像素1字节
    if img.mode != "L":
        img = img.convert("L")
    w, h = img.size
    if not (0 < w <= 0xFFFF and 0 < h <= 0xFFFF):
        raise ValueError(f"尺寸超出 u16 范围: {w}x{h}")

    pixels = img.tobytes()          # L模式: 宽*高 字节, 行优先
    assert len(pixels) == w * h

    header = struct.pack("<HHHH", w, h, 8, version)
    return header + pixels


def convert_one(in_path: Path, out_path: Path, version: int, overwrite: bool):
    if out_path.exists() and not overwrite:
        print(f"[-] 已存在, 跳过(加 --overwrite 覆盖): {out_path}")
        return False
    img = Image.open(in_path)
    data = png_to_bin(img, version)
    out_path.write_bytes(data)
    w, h = img.size
    print(f"[+] {in_path.name} ({w}x{h}) -> {out_path.name}  {len(data)} 字节 "
          f"(应为 {w*h+8})")
    return True


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    i = 1
    while True:
        c = p.with_name(f"{p.stem}_{i}{p.suffix}")
        if not c.exists():
            return c
        i += 1


def main():
    ap = argparse.ArgumentParser(description="灰度PNG -> bin(8bpp索引位图)")
    ap.add_argument("input", help="输入 PNG 文件或目录")
    ap.add_argument("-o", "--output", default=None,
                    help="输出 .bin 文件(单文件) 或 输出目录(批量)")
    ap.add_argument("-v", "--version", type=int, default=1,
                    help="头部版本号/标志字段, 默认 1")
    ap.add_argument("--overwrite", action="store_true", help="允许覆盖已存在文件")
    args = ap.parse_args()

    src = Path(args.input)

    # ---- 单文件模式 ----
    if src.is_file():
        out = Path(args.output) if args.output else src.with_suffix(".bin")
        convert_one(src, out, args.version, args.overwrite)
        return

    # ---- 批量模式 ----
    if not src.is_dir():
        print(f"[!] 路径不存在: {src}")
        return

    out_dir = Path(args.output) if args.output else src
    out_dir.mkdir(parents=True, exist_ok=True)

    pngs = sorted(src.glob("*.png"))
    if not pngs:
        print(f"[!] 目录中没有 PNG: {src}")
        return

    print(f"[*] 批量转换 {len(pngs)} 个 PNG -> {out_dir}  (版本={args.version})\n")
    n = 0
    for p in pngs:
        target = out_dir / (p.stem + ".bin")
        if not args.overwrite:
            target = unique_path(target)
        if convert_one(p, target, args.version, args.overwrite):
            n += 1
    print(f"\n[*] 完成, 共转换 {n} 个")


if __name__ == "__main__":
    main()