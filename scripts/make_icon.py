"""Генерирует assets/icon.ico — нужен PyInstaller'у как иконка exe."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from icon_draw import ico_sizes, make_image

OUT = Path(__file__).resolve().parent.parent / "assets" / "icon.ico"
OUT.parent.mkdir(parents=True, exist_ok=True)

images = ico_sizes()
images[-1].save(OUT, format="ICO", sizes=[(i.width, i.height) for i in images])
print(f"записано: {OUT}  ({OUT.stat().st_size} байт)")

# на всякий случай отдельным png — удобно смотреть глазами
png = OUT.with_suffix(".png")
make_image(256).save(png)
print(f"записано: {png}")
