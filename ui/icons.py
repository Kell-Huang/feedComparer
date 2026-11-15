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

    # Message-dialog glyphs. Drawn on the same 16x16 grid as the button icons
    # so a dialog and the toolbar above it share one visual language.
    'alert': (
        '<path d="M8 2.5 14.3 13.3H1.7z" fill="none" stroke="{c}" '
        'stroke-width="1.5" stroke-linejoin="round"/>'
        '<path d="M8 6.5v3.2" fill="none" stroke="{c}" stroke-width="1.6" '
        'stroke-linecap="round"/>'
        '<circle cx="8" cy="11.8" r="0.9" fill="{c}"/>'
    ),

    'error': (
        '<circle cx="8" cy="8" r="6" fill="none" stroke="{c}" stroke-width="1.5"/>'
        '<path d="M5.9 5.9 10.1 10.1M10.1 5.9 5.9 10.1" fill="none" stroke="{c}" '
        'stroke-width="1.6" stroke-linecap="round"/>'
    ),

    'question': (
        '<circle cx="8" cy="8" r="6" fill="none" stroke="{c}" stroke-width="1.5"/>'
        '<path d="M6.2 6.4a1.9 1.9 0 1 1 2.4 1.8c-.5.2-.7.6-.7 1.1v.3" '
        'fill="none" stroke="{c}" stroke-width="1.5" stroke-linecap="round"/>'
        '<circle cx="8" cy="11.7" r="0.85" fill="{c}"/>'
    ),

    'info': (
        '<circle cx="8" cy="8" r="6" fill="none" stroke="{c}" stroke-width="1.5"/>'
        '<path d="M8 7.4v3.5" fill="none" stroke="{c}" stroke-width="1.6" '
        'stroke-linecap="round"/>'
        '<circle cx="8" cy="5.1" r="0.9" fill="{c}"/>'
    ),
}


def make_pixmap(name: str, color: str = '#ffffff', size: int = 16) -> QPixmap:
    """Render one icon to a transparent pixmap at ``size`` pixels.

    Returns a null pixmap for an unknown name or a missing SVG plugin, so
    callers can test ``isNull()`` instead of catching exceptions.
    """
    body = _BODIES.get(name)
    if body is None or QSvgRenderer is None:
        return QPixmap()

    svg = _SVG_TEMPLATE.format(body=body.format(c=color)).encode('utf-8')
    renderer = QSvgRenderer(QByteArray(svg))
    if not renderer.isValid():
        return QPixmap()

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    return pixmap


def make_icon(name: str, color: str = '#ffffff', size: int = 16) -> QIcon:
    """Build a QIcon for ``name``, tinted ``color``.

    Returns a null QIcon when the SVG plugin is unavailable or the name is
    unknown, so callers can use the result unconditionally.
    """
    icon = QIcon()
    # 1x and 2x keep the icon sharp on high-DPI screens.
    for scale in (1, 2):
        pixmap = make_pixmap(name, color, size * scale)
        if pixmap.isNull():
            return QIcon()
        icon.addPixmap(pixmap)
    return icon
