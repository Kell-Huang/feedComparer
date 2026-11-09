"""Vector icons rendered from inline SVG.

Icons are kept as SVG source rather than binary assets so they stay crisp at
any DPI and can be re-tinted for each button role (white on a filled primary
button, muted grey on a secondary one, red for the destructive cancel action).

Everything is embedded in this module — no image files to ship, no network
fetch, and no base64 blobs to decode at import time.
"""
from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap

try:
    from PySide6.QtSvg import QSvgRenderer
except ImportError:                                     # pragma: no cover
    QSvgRenderer = None


_SVG_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">{body}</svg>'
)

# Each body is drawn on a 16x16 grid. {c} is replaced with the tint colour.
_BODIES = {
    'play': '<path d="M5.6 3.3a.6.6 0 0 1 .9-.5l7 4.7a.6.6 0 0 1 0 1l-7 4.7a.6.6 0 0 1-.9-.5z" fill="{c}"/>',

    'download': (
        '<path d="M8 2.8v6.3" fill="none" stroke="{c}" stroke-width="1.7" '
        'stroke-linecap="round"/>'
        '<path d="M5.1 6.6 8 9.5l2.9-2.9" fill="none" stroke="{c}" '
        'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M3.3 11.2v1.1c0 .5.4.9.9.9h7.6c.5 0 .9-.4.9-.9v-1.1" '
        'fill="none" stroke="{c}" stroke-width="1.7" stroke-linecap="round"/>'
    ),

    'trash': (
        '<path d="M2.9 4.4h10.2" fill="none" stroke="{c}" stroke-width="1.5" '
        'stroke-linecap="round"/>'
        '<path d="M6.3 4.4V3.3c0-.5.4-.9.9-.9h1.6c.5 0 .9.4.9.9v1.1" '
        'fill="none" stroke="{c}" stroke-width="1.5" stroke-linecap="round"/>'
        '<path d="M4.7 4.4l.5 7.5c0 .5.4.9.9.9h3.8c.5 0 .9-.4.9-.9l.5-7.5" '
        'fill="none" stroke="{c}" stroke-width="1.5" stroke-linecap="round" '
        'stroke-linejoin="round"/>'
    ),

    'stop': '<rect x="4.4" y="4.4" width="7.2" height="7.2" rx="1.4" fill="{c}"/>',
}


def make_icon(name: str, color: str = '#ffffff', size: int = 16) -> QIcon:
    """Build a QIcon for ``name``, tinted ``color``.

    Returns a null QIcon when the SVG plugin is unavailable or the name is
    unknown, so callers can use the result unconditionally.
    """
    body = _BODIES.get(name)
    if body is None or QSvgRenderer is None:
        return QIcon()

    svg = _SVG_TEMPLATE.format(body=body.format(c=color)).encode('utf-8')
    renderer = QSvgRenderer(QByteArray(svg))
    if not renderer.isValid():
        return QIcon()

    icon = QIcon()
    # 1x and 2x keep the icon sharp on high-DPI screens.
    for scale in (1, 2):
        pixels = size * scale
        pixmap = QPixmap(pixels, pixels)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(pixmap)
    return icon
