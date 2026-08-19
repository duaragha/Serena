"""Draw Serena's silk-ribbon S app icon.

The Linux build carries this icon as committed PNG/ICO files, but `build/` is
git-ignored and this checkout has no remote, so the Windows machine had nothing
to package and electron-builder refused to make an installer. Generating the
mark from code means either machine can rebuild it identically instead of
depending on a binary that has to be copied around by hand.

Run:  python desktop-electron/windows/make-icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BUILD_DIR = Path(__file__).resolve().parents[1] / "build"

# Dark tile, ribbon running warm pink into violet. The mark reads at 16px
# because the S is drawn as one thick continuous stroke, not a font glyph.
TILE = (17, 15, 24, 255)
RIBBON_TOP = (233, 138, 191)
RIBBON_BOTTOM = (150, 108, 232)
SIZE = 1024
SUPERSAMPLE = 2


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _catmull_rom(points: list[tuple[float, float]], samples: int) -> list[tuple[float, float]]:
    """Smooth the control points into a continuous curve.

    Drawing straight segments between the control points left visible facets on
    the outside of each bend, which is exactly where the eye looks on an S.
    """

    padded = [points[0], *points, points[-1]]
    curve: list[tuple[float, float]] = []
    for index in range(len(padded) - 3):
        p0, p1, p2, p3 = padded[index : index + 4]
        for step in range(samples):
            t = step / samples
            t2, t3 = t * t, t * t * t
            curve.append(
                (
                    0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t
                           + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                           + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3),
                    0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t
                           + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                           + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3),
                )
            )
    curve.append(points[-1])
    return curve


def _ribbon_mask(size: int) -> Image.Image:
    """The S as a single swept stroke, thick in the middle, tapering at the tips."""

    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    unit = size / 100.0

    # Control points traced down the spine of the S.
    spine = [
        (73, 27), (62, 19), (47, 18), (36, 24), (32, 34), (37, 43),
        (47, 48), (58, 52), (67, 58), (70, 67), (64, 77), (51, 83),
        (36, 82), (27, 74),
    ]
    curve = _catmull_rom([(x * unit, y * unit) for x, y in spine], samples=48)

    # A stroke that thins toward both ends reads as ribbon rather than as a
    # letter set in a heavy font. Stamping a disc per sample keeps the outline
    # smooth instead of faceted.
    count = len(curve)
    for index, (x, y) in enumerate(curve):
        position = index / max(1, count - 1)
        taper = 1.0 - abs(position - 0.5) * 2.0
        radius = unit * (3.4 + 3.1 * (taper ** 0.55)) 
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=255)
    return mask


def render(size: int = SIZE) -> Image.Image:
    work = size * SUPERSAMPLE
    icon = Image.new("RGBA", (work, work), (0, 0, 0, 0))

    # Rounded tile so the mark has a body of its own in a taskbar.
    tile = Image.new("RGBA", (work, work), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle(
        [0, 0, work - 1, work - 1], radius=round(work * 0.22), fill=TILE
    )
    icon.alpha_composite(tile)

    gradient = Image.new("RGBA", (work, work))
    pixels = gradient.load()
    for y in range(work):
        colour = _lerp(RIBBON_TOP, RIBBON_BOTTOM, y / max(1, work - 1))
        for x in range(work):
            pixels[x, y] = (*colour, 255)

    icon.paste(gradient, (0, 0), _ribbon_mask(work))
    return icon.resize((size, size), Image.LANCZOS)


def main() -> None:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    icon = render()

    png_path = BUILD_DIR / "icon.png"
    icon.save(png_path)

    # Windows picks the closest size from the ICO, so ship the whole ladder
    # rather than letting it downscale 1024px into a 16px tray slot.
    ico_path = BUILD_DIR / "icon.ico"
    icon.save(
        ico_path,
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    print(f"wrote {png_path} ({png_path.stat().st_size} bytes)")
    print(f"wrote {ico_path} ({ico_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
