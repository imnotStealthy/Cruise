from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
FAVICON = ROOT / "web" / "favicon.svg"
OUT = ROOT / "web" / "icon.ico"
SIZES = (16, 32, 48, 64, 128, 256)
LIME = "#C8FF00"
MAGENTA = "#FF2E88"


def render(size: int) -> Image.Image:
    scale = size / 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def pts(values: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [(x * scale, y * scale) for x, y in values]

    width_loop = max(1, round(5 * scale))
    width_arc = max(1, round(3 * scale))

    # Pillow draws the same source geometry as web/favicon.svg.
    loop = [
        (6, 28), (13, 12), (24, 12), (32, 28), (40, 44), (51, 44),
        (58, 28), (51, 12), (40, 12), (32, 28), (24, 44), (13, 44), (6, 28),
    ]
    draw.line(pts(loop), fill=LIME, width=width_loop, joint="curve")
    draw.arc([11 * scale, 28 * scale, 53 * scale, 78 * scale], 180, 360, fill=MAGENTA, width=width_arc)
    draw.line(pts([(32, 48), (48, 30)]), fill=MAGENTA, width=width_arc)

    for x, y, color in [
        (50, 7, LIME), (57, 14, LIME), (50, 21, LIME),
        (57, 7, MAGENTA), (50, 14, MAGENTA), (57, 21, MAGENTA),
    ]:
        draw.rectangle(
            [x * scale, y * scale, (x + 7) * scale, (y + 7) * scale],
            fill=color,
        )

    return image


def main() -> None:
    if not FAVICON.exists():
        raise FileNotFoundError(FAVICON)

    images = [render(size) for size in SIZES]
    images[-1].save(OUT, sizes=[(size, size) for size in SIZES], append_images=images[:-1])
    print(f"generated {OUT}")


if __name__ == "__main__":
    main()
