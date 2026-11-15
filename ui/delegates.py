"""Item delegates for painted table cells."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import QStyledItemDelegate


class BadgeDelegate(QStyledItemDelegate):
    """Draws a soft, rounded status pill instead of a filled table cell.

    The previous rendering painted the whole cell with a saturated colour,
    which turned a column of eight rows into a colour wall competing with the
    table header. A pill carries the same information with a fraction of the
    visual weight.

    The payload is read from ``Qt.UserRole`` as ``(label, background,
    foreground)``; the item's own text is kept in sync so sizing, tooltips and
    accessibility still work.
    """

    def paint(self, painter, option, index):
        payload = index.data(Qt.UserRole)
        if not payload:
            super().paint(painter, option, index)
            return

        label, background, foreground = payload
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)

        rect = option.rect.adjusted(8, 5, -8, -5)
        if rect.height() < 12:
            rect = option.rect.adjusted(8, 2, -8, -2)

        radius = rect.height() / 2.0
        path = QPainterPath()
        path.addRoundedRect(QRectF(rect), radius, radius)
        painter.fillPath(path, QColor(background))

        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(foreground))
        painter.drawText(rect, Qt.AlignCenter, label)
        painter.restore()
