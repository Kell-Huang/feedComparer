"""Small layout helpers shared by the mode tabs."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QScrollArea, QVBoxLayout, QWidget


def scrollable_container(host: QWidget, margins=(12, 12, 12, 12), spacing=8):
    """Give ``host`` a vertically scrolling content area.

    The mode tabs stack several group boxes plus a table; on a laptop-height
    window the total exceeded the available height and Qt silently squeezed
    the tables down to a few pixels. Wrapping the content keeps every control
    at a usable size and lets the page scroll instead.

    Returns ``(layout, scroll_area)``. Add child widgets to ``layout``.
    """
    outer = QVBoxLayout(host)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)

    scroll = QScrollArea(host)
    scroll.setObjectName("TabScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    content = QWidget()
    content.setObjectName("TabContent")
    content.setAutoFillBackground(False)

    layout = QVBoxLayout(content)
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)

    scroll.setWidget(content)
    outer.addWidget(scroll)
    return layout, scroll
