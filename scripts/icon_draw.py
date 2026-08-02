"""Иконка рисуется кодом, а не лежит файлом: так --onefile сборке нечего терять,
а трей и .ico гарантированно выглядят одинаково."""
from PIL import Image, ImageDraw

BG_ON = (37, 122, 189)     # синий «в работе»
BG_OFF = (110, 118, 129)   # серый «остановлен»
FG = (255, 255, 255)


def make_image(size: int = 64, running: bool = True) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    r = size // 5
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=BG_ON if running else BG_OFF)

    # бумажный самолётик: два треугольника, как в логотипе телеграма
    s = size
    body = [(0.20 * s, 0.52 * s), (0.82 * s, 0.22 * s), (0.46 * s, 0.78 * s)]
    fin = [(0.46 * s, 0.78 * s), (0.44 * s, 0.58 * s), (0.82 * s, 0.22 * s)]
    d.polygon(body, fill=FG)
    d.polygon(fin, fill=(215, 228, 240))
    return img


def ico_sizes() -> list[Image.Image]:
    return [make_image(n) for n in (16, 24, 32, 48, 64, 128, 256)]
