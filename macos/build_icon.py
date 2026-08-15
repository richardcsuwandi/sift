"""Convert a square PNG into the multi-resolution ICNS used by the launcher."""

import sys

from PIL import Image


source, destination = sys.argv[1:3]
image = Image.open(source).convert("RGBA")
image.save(
    destination,
    format="ICNS",
    append_images=[
        image.resize((size, size), Image.Resampling.LANCZOS)
        for size in (16, 32, 64, 128, 256, 512)
    ],
)
