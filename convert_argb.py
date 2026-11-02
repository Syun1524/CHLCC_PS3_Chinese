import argparse
import os
import struct
import sys

from PIL import Image


HEADER_SIZE = 8
BYTES_PER_PIXEL = 4


def read_image_file(input_path):
    print(f"Input file: {input_path}")

    with open(input_path, "rb") as file:
        file_data = file.read()

    file_size = len(file_data)

    print(f"Actual file size: {file_size} bytes")

    if file_size < HEADER_SIZE:
        raise ValueError(
            f"Input file is too small. "
            f"At least {HEADER_SIZE} bytes are required."
        )

    header = file_data[:HEADER_SIZE]

    print(
        "Header: " +
        " ".join(f"{byte:02X}" for byte in header)
    )

    # Bytes 0-1: width, little-endian
    # Bytes 2-3: height, little-endian
    width, height = struct.unpack_from("<HH", header, 0)

    print(f"Image size: {width} x {height}")

    if width <= 0 or height <= 0:
        raise ValueError(
            f"Invalid image dimensions: {width} x {height}"
        )

    pixel_count = width * height
    pixel_size = pixel_count * BYTES_PER_PIXEL
    expected_file_size = HEADER_SIZE + pixel_size

    print(f"Pixel data size: {pixel_size} bytes")
    print(f"Expected file size: {expected_file_size} bytes")

    if file_size < expected_file_size:
        raise ValueError(
            "Input file is too small.\n"
            f"Expected at least: {expected_file_size} bytes\n"
            f"Actual size:       {file_size} bytes"
        )

    if file_size > expected_file_size:
        extra_size = file_size - expected_file_size
        print(
            f"Warning: input file contains "
            f"{extra_size} extra bytes at the end"
        )

    # Read exactly width * height * 4 bytes after the 8-byte header.
    argb_data = file_data[
        HEADER_SIZE:HEADER_SIZE + pixel_size
    ]

    print("Converting ARGB pixels to RGBA")

    # Input format:
    #
    #   A R G B
    #
    # Pillow expects:
    #
    #   R G B A
    #
    rgba_data = bytearray(pixel_size)

    for source_pos in range(0, pixel_size, 4):
        alpha = argb_data[source_pos + 0]
        red = argb_data[source_pos + 1]
        green = argb_data[source_pos + 2]
        blue = argb_data[source_pos + 3]

        rgba_data[source_pos + 0] = red
        rgba_data[source_pos + 1] = green
        rgba_data[source_pos + 2] = blue
        rgba_data[source_pos + 3] = alpha

    print("Pixel conversion completed")

    image = Image.frombytes(
        "RGBA",
        (width, height),
        bytes(rgba_data)
    )

    return image, width, height


def convert_file(input_path, output_path):
    image, width, height = read_image_file(input_path)

    output_directory = os.path.dirname(os.path.abspath(output_path))

    if not os.path.exists(output_directory):
        os.makedirs(output_directory)

    print(f"Saving PNG file: {output_path}")

    image.save(
        output_path,
        format="PNG"
    )

    output_size = os.path.getsize(output_path)

    print(f"Output PNG size: {output_size} bytes")
    print(f"Conversion successful: {output_path}")
    print(f"Final image size: {width} x {height}")


def main():
    parser = argparse.ArgumentParser(
        description="Convert custom ARGB image files to PNG."
    )

    parser.add_argument(
        "input_file",
        help="Input custom image file"
    )

    parser.add_argument(
        "output_file",
        help="Output PNG file"
    )

    args = parser.parse_args()

    try:
        convert_file(
            args.input_file,
            args.output_file
        )

    except FileNotFoundError:
        print(
            f"Error: input file not found: {args.input_file}",
            file=sys.stderr
        )
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

    return 0


if __name__ == "__main__":
    sys.exit(main())