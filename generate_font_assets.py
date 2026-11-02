#!/usr/bin/env python3
"""Generate fixed-size MAGES font PNG and BIN assets.

The game assets in chlcc use 64 columns and 32x32 final cells. Half-width
characters are compressed horizontally while full-width characters retain
their 32-pixel cell geometry.

Requires Pillow and fontTools: python -m pip install Pillow fonttools
"""

import argparse
import math
import struct
import unicodedata
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont
from fontTools.ttLib import TTFont


COLUMNS = 64
NORMAL_CELL = 32
OUTLINE_CELL = 32
TARGET_CELL = 32
DEFAULT_FONT_SIZE = 30
DEFAULT_CHARSET_SIZE = 5440
DEFAULT_VERSION = 2

# The stock CHLCC atlases (chlcc/12.bin + chlcc/25.bin) are 2048x1184, i.e.
# 37 rows x 64 columns = 2368 cells. Those 2368 cells must stay bit-for-bit
# identical no matter which charset or font is requested, so they are copied
# straight out of the reference files instead of being drawn.
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REFERENCE_DIR = SCRIPT_DIR.parent / "chlcc"
DEFAULT_FREEZE_CELLS = 2368


def read_compound_table(path: Path) -> dict[int, str]:
    """Read the [E000-E029]=... syntax used by CompoundCharacters.tbl."""
    compounds: dict[int, str] = {}
    if path is None:
        return compounds

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("["):
            continue
        close = line.find("]")
        if close < 0 or close + 1 >= len(line) or line[close + 1] != "=":
            continue
        value = line[close + 2:]
        bounds = line[1:close].split("-")
        try:
            start = int(bounds[0].replace("0x", ""), 16)
            end = int(bounds[-1].replace("0x", ""), 16)
        except ValueError:
            continue
        for codepoint in range(start, end + 1):
            compounds[codepoint] = value
    return compounds


def load_charset(path: Path) -> str:
    # A BOM is harmless here and should not become a glyph in the first cell.
    return path.read_text(encoding="utf-8-sig")


def resolve_font(path_or_name: str) -> Path:
    path = Path(path_or_name)
    if path.is_file():
        return path

    fonts_dir = Path(__import__("os").environ.get("WINDIR", "C:/Windows")) / "Fonts"
    needle = path_or_name.casefold()
    matches = [candidate for candidate in fonts_dir.iterdir()
               if candidate.is_file() and needle in candidate.name.casefold()]
    if matches:
        return matches[0]
    raise FileNotFoundError(f"找不到字体文件或 Windows 字体: {path_or_name}")


def load_font(path: Path, size: int) -> tuple[ImageFont.FreeTypeFont, set[int]]:
    font = ImageFont.truetype(str(path), size)
    with TTFont(str(path), lazy=True) as typeface:
        cmap = set()
        for table in typeface["cmap"].tables:
            cmap.update(table.cmap)
    return font, cmap


def choose_font(character: str, font: ImageFont.FreeTypeFont, font_cmap: set[int],
                fallback_font: ImageFont.FreeTypeFont | None,
                fallback_cmap: set[int]) -> ImageFont.FreeTypeFont:
    if fallback_font is not None and ord(character) not in font_cmap and ord(character) in fallback_cmap:
        return fallback_font
    return font


def glyph_text(character: str, compounds: dict[int, str]) -> str:
    codepoint = ord(character)
    if unicodedata.category(character) == "Co":
        return compounds.get(codepoint, " ")
    return character


def is_narrow_character(character: str, text: str) -> bool:
    return len(text) == 1 and unicodedata.east_asian_width(character) in ("Na", "H", "A")


def narrow_cell(cell: Image.Image) -> Image.Image:
    scaled = cell.resize((round(TARGET_CELL * 2 / 3), TARGET_CELL), Image.Resampling.BILINEAR)
    result = Image.new("L", (TARGET_CELL, TARGET_CELL), 0)
    result.paste(scaled, (0, 0))
    return result


def draw_normal(characters: str, compounds: dict[int, str], font: ImageFont.FreeTypeFont,
                font_cmap: set[int], fallback_font: ImageFont.FreeTypeFont | None,
                fallback_cmap: set[int], baseline_x: int, baseline_y: int) -> tuple[Image.Image, bytearray]:
    rows = math.ceil(len(characters) / COLUMNS)
    image = Image.new("L", (NORMAL_CELL * COLUMNS, NORMAL_CELL * rows), 0)
    widths = bytearray()
    for index, character in enumerate(characters):
        row, column = divmod(index, COLUMNS)
        text = glyph_text(character, compounds)
        draw_font = choose_font(character, font, font_cmap, fallback_font, fallback_cmap)
        cell = Image.new("L", (NORMAL_CELL, NORMAL_CELL), 0)
        ImageDraw.Draw(cell).text((baseline_x, baseline_y), text, font=draw_font, fill=255)
        if is_narrow_character(character, text):
            cell = narrow_cell(cell)
        image.paste(cell, (column * NORMAL_CELL, row * NORMAL_CELL))
        probe = ImageDraw.Draw(Image.new("L", (1, 1)))
        box = probe.textbbox((0, 0), text + " ", font=draw_font)
        widths.append(min(255, max(0, math.ceil((box[2] - box[0]) / 1.5) + 1)))
    return image, widths


def outline_cell(cell: Image.Image) -> Image.Image:
    """Turn a filled glyph cell into its outline counterpart.

    Measured against the stock pair (chlcc/12.bin -> chlcc/25.bin) the outline
    atlas is not a hollow ring around the glyph: it is the glyph itself, grown
    outwards by one pixel, with the body stamped solid.

    1. Every pixel the glyph already covers becomes fully opaque. In the stock
       atlas the interior matches the normal atlas at 255 for 100% of its
       918,483 ink pixels, so antialiasing is only kept on the outer edge.
    2. The edge is then bled one pixel outwards by taking the maximum of the
       source pixel and its four orthogonal neighbours, so the antialiased
       values survive as the new outer contour.

    Diagonal neighbours are deliberately excluded - they explain just 2.6% of
    the stock ring, and adding them roughly doubles the error, because the
    stock renderer grows with a Manhattan disc rather than a square.
    """
    width, height = cell.size
    # Pad first so the shifts cannot wrap around the opposite edge.
    padded = Image.new("L", (width + 2, height + 2), 0)
    padded.paste(cell, (1, 1))
    grown = padded
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        grown = ImageChops.lighter(grown, ImageChops.offset(padded, dx, dy))
    grown = grown.crop((1, 1, width + 1, height + 1))
    grown.paste(255, mask=cell.point(lambda value: 255 if value else 0))
    return grown


def draw_outline(characters: str, compounds: dict[int, str], font: ImageFont.FreeTypeFont,
                 font_cmap: set[int], fallback_font: ImageFont.FreeTypeFont | None,
                 fallback_cmap: set[int], baseline_x: int, baseline_y: int) -> Image.Image:
    rows = math.ceil(len(characters) / COLUMNS)
    cell_size = (OUTLINE_CELL, OUTLINE_CELL)
    image = Image.new("L", (OUTLINE_CELL * COLUMNS, OUTLINE_CELL * rows), 0)
    for index, character in enumerate(characters):
        row, column = divmod(index, COLUMNS)
        text = glyph_text(character, compounds)
        draw_font = choose_font(character, font, font_cmap, fallback_font, fallback_cmap)
        # Build the outline from the same drawn glyph the normal atlas uses,
        # then bloat it in outline_cell(). The growth happens after narrowing so
        # that half-width cells also gain exactly one pixel on every side.
        cell = Image.new("L", cell_size, 0)
        ImageDraw.Draw(cell).text((baseline_x, baseline_y), text, font=draw_font, fill=255)
        if is_narrow_character(character, text):
            cell = narrow_cell(cell)
        image.paste(outline_cell(cell), (column * OUTLINE_CELL, row * OUTLINE_CELL))
    return image


def write_bin(image: Image.Image, path: Path, version: int) -> None:
    image = image.convert("L")
    path.write_bytes(struct.pack("<HHHH", image.width, image.height, 8, version)
                     + image.tobytes())


def load_frozen_atlas(directory: Path, stem: str, columns: int, cell: int) -> Image.Image:
    """Load a stock CHLCC atlas (e.g. chlcc/12.bin) as the frozen prefix.

    The BIN is authoritative (8bpp raw pixels behind a little-endian header);
    when a PNG of the same name exists it is cross-checked so that a stale or
    mismatched pair cannot slip through unnoticed.
    """
    bin_path = directory / f"{stem}.bin"
    if not bin_path.is_file():
        raise FileNotFoundError(f"找不到参考字库 {bin_path}")
    raw = bin_path.read_bytes()
    if len(raw) < 8:
        raise ValueError(f"{bin_path} 太小，不是字库")
    width, height, bpp, _version = struct.unpack("<HHHH", raw[:8])
    if bpp != 8 or len(raw) - 8 != width * height:
        raise ValueError(f"{bin_path} 不是 8bpp 裸像素字库")
    if width != columns * cell:
        raise ValueError(f"{bin_path} 宽度 {width} != {columns} * {cell}")
    if height % cell:
        raise ValueError(f"{bin_path} 高度 {height} 不是 {cell} 的整数倍")
    image = Image.frombytes("L", (width, height), raw[8:])

    png_path = directory / f"{stem}.png"
    if png_path.is_file():
        with Image.open(png_path) as png:
            if png.convert("L").tobytes() != raw[8:]:
                raise ValueError(f"{png_path} 与 {bin_path} 的像素不一致")
    return image


def paste_frozen(image: Image.Image, reference: Image.Image, start_index: int,
                 freeze_cells: int, columns: int, cell: int) -> int:
    """Overwrite the cells that belong to the frozen prefix with reference pixels.

    `start_index` is the index of this batch's first character inside the full
    charset, so the frozen region can be mapped onto the batch even when the
    batch boundaries do not line up with the atlas rows.
    """
    if reference is None or freeze_cells <= 0:
        return 0
    ref_rows = reference.height // cell
    copied = 0
    for row in range(image.height // cell):
        first = start_index + row * columns
        if first >= freeze_cells:
            break
        last = min(first + columns, freeze_cells)
        for index in range(first, last):
            ref_row, ref_col = divmod(index, columns)
            if ref_row >= ref_rows:
                return copied
            box = (ref_col * cell, ref_row * cell,
                   ref_col * cell + cell, ref_row * cell + cell)
            image.paste(reference.crop(box), ((index - first) * cell, row * cell))
            copied += 1
    return copied


def write_batch(characters: str, compounds: dict[int, str], font: ImageFont.FreeTypeFont,
                font_cmap: set[int], fallback_font: ImageFont.FreeTypeFont | None,
                fallback_cmap: set[int],
                output_dir: Path, suffix: str, is_single_batch: bool,
                args: argparse.Namespace, start_index: int = 0,
                frozen_normal: Image.Image | None = None,
                frozen_outline: Image.Image | None = None) -> None:
    normal, widths = draw_normal(characters, compounds, font, font_cmap,
                                 fallback_font, fallback_cmap,
                                 args.baseline_x, args.baseline_y)
    outline = draw_outline(characters, compounds, font, font_cmap,
                           fallback_font, fallback_cmap,
                           args.baseline_x, args.baseline_y)
    normal = normal.convert("L")
    outline = outline.convert("L")

    # Cells that belong to the stock prefix are taken from the reference files
    # verbatim, so the first --freeze-cells glyphs never depend on the font or
    # the charset that was passed in.
    frozen = paste_frozen(normal, frozen_normal, start_index, args.freeze_cells,
                          COLUMNS, NORMAL_CELL)
    paste_frozen(outline, frozen_outline, start_index, args.freeze_cells,
                 COLUMNS, OUTLINE_CELL)

    normal_png = output_dir / f"{args.normal_name}_{suffix}.png"
    outline_png = output_dir / f"{args.outline_name}_{suffix}.png"
    normal.save(normal_png, format="PNG", bits=8)
    outline.save(outline_png, format="PNG", bits=8)
    normal_bin = args.normal_bin if is_single_batch else (
        f"{Path(args.normal_bin).stem}_{suffix}.bin")
    outline_bin = args.outline_bin if is_single_batch else (
        f"{Path(args.outline_bin).stem}_{suffix}.bin")
    write_bin(normal, output_dir / normal_bin, args.version)
    write_bin(outline, output_dir / outline_bin, args.version)
    with (output_dir / "widths.bin").open("ab") as widths_file:
        widths_file.write(widths)
    if frozen:
        print(f"  冻结前缀: cell {start_index}..{start_index + frozen - 1} "
              f"直接取自参考字库")
    print(f"{normal_png.name}: {normal.width}x{normal.height}, mode={normal.mode}")
    print(f"{outline_png.name}: {outline.width}x{outline.height}, mode={outline.mode}")


def main() -> None:
    parser = argparse.ArgumentParser(description="生成固定 2048 宽度的 MAGES 字库 PNG 和 BIN")
    parser.add_argument("--charset", required=True, type=Path)
    parser.add_argument("--compound", required=True, type=Path)
    parser.add_argument("--font", required=True, help="字体文件路径或 Windows 字体文件名片段")
    parser.add_argument("--fallback-font", default="simhei",
                        help="主字体缺字时使用的字体，默认 simhei；传空字符串可禁用")
    parser.add_argument("--out-dir", type=Path, default=Path("."))
    parser.add_argument("--font-size", type=int, default=DEFAULT_FONT_SIZE)
    parser.add_argument("--charset-size", type=int, default=DEFAULT_CHARSET_SIZE)
    parser.add_argument("--baseline-x", type=int, default=1)
    parser.add_argument("--baseline-y", type=int, default=1)
    parser.add_argument("--normal-name", default="font")
    parser.add_argument("--outline-name", default="font-outline")
    parser.add_argument("--normal-bin", default="12.bin")
    parser.add_argument("--outline-bin", default="25.bin")
    parser.add_argument("--version", type=int, default=DEFAULT_VERSION)
    parser.add_argument("--freeze-cells", type=int, default=DEFAULT_FREEZE_CELLS,
                        help="前多少个字符必须与参考字库逐像素一致，默认 2368")
    parser.add_argument("--reference-dir", type=Path, default=DEFAULT_REFERENCE_DIR,
                        help="存放参考字库 12.bin/25.bin 的目录，默认 ../chlcc")
    parser.add_argument("--reference-normal", default="12",
                        help="参考正常字库的文件名（不含扩展名），默认 12")
    parser.add_argument("--reference-outline", default="25",
                        help="参考描边字库的文件名（不含扩展名），默认 25")
    args = parser.parse_args()

    characters = load_charset(args.charset)
    if not characters:
        raise ValueError("charset 为空")
    if args.charset_size <= 0:
        raise ValueError("--charset-size 必须大于 0")
    if args.freeze_cells < 0:
        raise ValueError("--freeze-cells 不能为负数")

    frozen_normal = frozen_outline = None
    if args.freeze_cells:
        frozen_normal = load_frozen_atlas(args.reference_dir, args.reference_normal,
                                          COLUMNS, NORMAL_CELL)
        frozen_outline = load_frozen_atlas(args.reference_dir, args.reference_outline,
                                           COLUMNS, OUTLINE_CELL)
        available = (frozen_normal.height // NORMAL_CELL) * COLUMNS
        if available < args.freeze_cells:
            raise ValueError(
                f"参考字库只有 {available} 个格子，少于 --freeze-cells {args.freeze_cells}")
        if len(characters) < args.freeze_cells:
            print(f"charset 只有 {len(characters)} 个字符，"
                  f"补空格到 {args.freeze_cells} 以保证冻结前缀完整")
            characters = characters.ljust(args.freeze_cells, " ")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "widths.bin").write_bytes(b"")
    compounds = read_compound_table(args.compound)
    font, font_cmap = load_font(resolve_font(args.font), args.font_size)
    fallback_font = None
    fallback_cmap: set[int] = set()
    if args.fallback_font and args.fallback_font.casefold() not in ("none", "off"):
        fallback_path = resolve_font(args.fallback_font)
        fallback_font, fallback_cmap = load_font(fallback_path, args.font_size)

    batch_count = math.ceil(len(characters) / args.charset_size)
    for batch_index in range(batch_count):
        start = batch_index * args.charset_size
        batch = characters[start:start + args.charset_size]
        suffix = chr(ord("A") + batch_index) if batch_index < 26 else str(batch_index)
        write_batch(batch, compounds, font, font_cmap, fallback_font, fallback_cmap,
                args.out_dir, suffix, batch_count == 1, args, start,
                frozen_normal, frozen_outline)


if __name__ == "__main__":
    main()