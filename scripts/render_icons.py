"""Render Dawn's SVG character and native app icons with existing dependencies."""
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPen
from PySide6.QtSvg import QSvgRenderer


ROOT = Path(__file__).resolve().parents[1] / "dawn"
SIZE = 1024


def render_icons() -> None:
    renderer = QSvgRenderer(str(ROOT / "mascot.svg"))
    if not renderer.isValid():
        raise SystemExit("Cannot render dawn/mascot.svg")
    for name in ("mascot", "icon"):
        image = QImage(SIZE, SIZE, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing)
        if name == "icon":
            background = QLinearGradient(0, 0, SIZE, SIZE)
            background.setColorAt(0, QColor("#FFF9EE"))
            background.setColorAt(1, QColor("#F5E7D4"))
            painter.setBrush(background)
            painter.setPen(QPen(QColor("#E9D9C1"), 2))
            painter.drawRoundedRect(QRectF(32, 32, 960, 960), 216, 216)
        renderer.render(painter, QRectF(0, 0, SIZE, SIZE))
        painter.end()
        if not image.save(str(ROOT / f"{name}.png")):
            raise SystemExit(f"Cannot save {name}.png")
    with Image.open(ROOT / "icon.png") as icon:
        icon.save(ROOT / "icon.ico", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
        icon.save(ROOT / "icon.icns")
    print("Rendered mascot.png, icon.png, icon.ico and icon.icns from mascot.svg")


if __name__ == "__main__":
    render_icons()
