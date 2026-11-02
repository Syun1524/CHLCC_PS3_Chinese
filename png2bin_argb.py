import argparse
import os
import struct
import sys
from PIL import Image

HEADER_SIZE = 8
BYTES_PER_PIXEL = 4
PNG_EXTENSIONS = (".png",)


def load_png(input_path):
    print(f"Input file: {input_path}")
    image = Image.open(input_path)
    print(f"Original image mode: {image.mode}")
    print(f"Image size: {image.width} x {image.height}")

    # The ARGB container stores 16-bit width/height, so dimensions
    # must fit in an unsigned short.
    if not (0 < image.width <= 0xFFFF and 0 < image.height <= 0xFFFF):
        raise ValueError(
            f"Image dimensions out of range: "
            f"{image.width} x {image.height} "
            f"(must be 1..65535)"
        )

    # Normalize to RGBA regardless of source mode
    # (RGB, P, L, LA, ... all become RGBA here).
    rgba_image = image.convert("RGBA")
    return rgba_image, image.width, image.height


def build_argb_data(rgba_image, width, height):
    pixel_count = width * height
    pixel_size = pixel_count * BYTES_PER_PIXEL
    print(f"Pixel count: {pixel_count}")
    print(f"Pixel data size: {pixel_size} bytes")

    print("Converting RGBA pixels to ARGB")
    # Pillow gives us:
    #
    #   R G B A
    #
    # The container expects:
    #
    #   A R G B
    #
    rgba_data = rgba_image.tobytes()
    argb_data = bytearray(pixel_size)
    for source_pos in range(0, pixel_size, 4):
        red = rgba_data[source_pos + 0]
        green = rgba_data[source_pos + 1]
        blue = rgba_data[source_pos + 2]
        alpha = rgba_data[source_pos + 3]

        argb_data[source_pos + 0] = alpha
        argb_data[source_pos + 1] = red
        argb_data[source_pos + 2] = green
        argb_data[source_pos + 3] = blue

    print("Pixel conversion completed")
    return bytes(argb_data)


def convert_file(input_path, output_path):
    rgba_image, width, height = load_png(input_path)
    argb_data = build_argb_data(rgba_image, width, height)

    # Bytes 0-1: width, little-endian
    # Bytes 2-3: height, little-endian
    header = struct.pack("<HH I", width, height, 0x20)
    # header = struct.pack("<HH", width, height)
    # header = header.ljust(HEADER_SIZE, b"\x00")
    print(
        "Header: " +
        " ".join(f"{byte:02X}" for byte in header)
    )

    output_directory = os.path.dirname(os.path.abspath(output_path))
    if not os.path.exists(output_directory):
        os.makedirs(output_directory)

    print(f"Writing custom image file: {output_path}")
    with open(output_path, "wb") as file:
        file.write(header)
        file.write(argb_data)

    expected_file_size = HEADER_SIZE + len(argb_data)
    output_size = os.path.getsize(output_path)
    print(f"Expected file size: {expected_file_size} bytes")
    print(f"Output file size: {output_size} bytes")
    print(f"Conversion successful: {output_path}")
    print(f"Final image size: {width} x {height}")


def find_png_files(folder_path):
    """Return a sorted list of .png files directly inside folder_path."""
    entries = []
    for name in os.listdir(folder_path):
        full_path = os.path.join(folder_path, name)
        if not os.path.isfile(full_path):
            continue
        if name.lower().endswith(PNG_EXTENSIONS):
            entries.append(full_path)
    entries.sort()
    return entries


def convert_folder(input_folder, output_folder):
    print(f"Input folder: {input_folder}")
    print(f"Output folder: {output_folder}")

    if not os.path.isdir(input_folder):
        raise NotADirectoryError(
            f"Input folder not found: {input_folder}"
        )

    png_files = find_png_files(input_folder)
    total = len(png_files)
    print(f"Found {total} PNG file(s)")

    if total == 0:
        print("Nothing to do.")
        return 0

    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    succeeded = 0
    failed = 0
    for index, input_path in enumerate(png_files, start=1):
        file_name = os.path.basename(input_path)
        output_name = os.path.splitext(file_name)[0] + ".bin"
        output_path = os.path.join(output_folder, output_name)

        print("")
        print(f"--- [{index}/{total}] {file_name} ---")
        try:
            convert_file(input_path, output_path)
            succeeded += 1
        except Exception as error:
            # One bad file should not stop the whole batch.
            failed += 1
            print(
                f"Conversion failed for {input_path}: {error}",
                file=sys.stderr
            )

    print("")
    print("Batch complete")
    print(f"Succeeded: {succeeded}")
    print(f"Failed:    {failed}")
    print(f"Total:     {total}")

    # Non-zero exit if anything failed.
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Convert PNG images to custom ARGB image files. "
            "Input may be a single PNG file or a folder of PNG files."
        )
    )
    parser.add_argument(
        "input",
        help="Input PNG file or input folder"
    )
    parser.add_argument(
        "output",
        help=(
            "Output custom image file (for a single input) or "
            "output folder (for a folder input)"
        )
    )
    args = parser.parse_args()

    try:
        if os.path.isdir(args.input):
            return convert_folder(args.input, args.output)
        else:
            convert_file(args.input, args.output)
            return 0
    except FileNotFoundError:
        print(
            f"Error: input not found: {args.input}",
            file=sys.stderr
        )
        return 1
    except NotADirectoryError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except PermissionError as error:
        print(
            f"Error: permission denied: {error}",
            file=sys.stderr
        )
        return 1
    except Exception as error:
        print(
            f"Conversion failed: {error}",
            file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())