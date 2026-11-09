"""Live per-pair status cells for the pairing tables.

The API and Manual tabs both show a pairing table whose last column holds a
"Remove" button. While a run is in progress that column is repurposed into a
status column, so the user can see which pair is downloading, which finished
and which failed — without leaving the tab and without waiting for the run to
end.

Only the rows that are actively downloading get a ``QProgressBar`` widget;
finished rows fall back to a plain item. That keeps the widget count bounded
by the worker count rather than by the number of pairs.
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout, QProgressBar, QTableWidget, QTableWidgetItem, QWidget,
)

from core.progress import (
    PAIR_CANCELLED,
    PAIR_DONE,
    PAIR_DOWNLOADING,
    PAIR_FAILED,
)

_COLOR_PENDING = QColor('#8c8c8c')
_COLOR_ACTIVE = QColor('#1677ff')
_COLOR_DONE = QColor('#52c41a')
_COLOR_FAILED = QColor('#cf1322')

MAX_ERROR_CHARS = 28


class PairTableStatus:
    """Drives one status column of a ``QTableWidget``."""

    def __init__(self, table: QTableWidget, column: int = 4):
        self.table = table
        self.column = column
        self.active = False
        self._states = {}

    # ------------------------------------------------------------------
    # Mode switching
    # ------------------------------------------------------------------

    def begin(self, row_count: int) -> None:
        """Switch the column from "Remove buttons" to "live status".

        Rows are not painted here; the owner calls ``render_row`` from its own
        ``_render_pairs`` so there is exactly one place that builds cells.
        """
        self.active = True
        self._states = {row: None for row in range(row_count)}
        header = QTableWidgetItem('Status')
        header.setTextAlignment(Qt.AlignCenter)
        self.table.setHorizontalHeaderItem(self.column, header)

    def end(self) -> None:
        """Switch back; the owner rebuilds the column on its next render."""
        self.active = False
        self._states.clear()
        header = QTableWidgetItem('')
        self.table.setHorizontalHeaderItem(self.column, header)
        for row in range(self.table.rowCount()):
            self._drop_widget(row)
            self.table.setItem(row, self.column, None)

    # ------------------------------------------------------------------
    # Updates
    # ------------------------------------------------------------------

    def apply(self, status) -> None:
        if not self.active:
            return
        self._states[status.index] = status
        self.render_row(status.index)

    def render_row(self, row: int) -> None:
        if not self.active:
            return
        if row < 0 or row >= self.table.rowCount():
            return

        status = self._states.get(row)
        state = status.state if status is not None else None

        if state == PAIR_DOWNLOADING:
            self._render_active(row, status)
            return

        # Anything else is a plain item; drop the progress bar if there is one.
        self._drop_widget(row)
        item = QTableWidgetItem(self._text_for(status))
        item.setTextAlignment(Qt.AlignCenter)
        item.setForeground(self._color_for(state))
        if status is not None and status.error:
            item.setToolTip(status.error)
        elif status is not None and status.detail:
            item.setToolTip(status.detail)
        self.table.setItem(row, self.column, item)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _render_active(self, row: int, status) -> None:
        bar = self._existing_bar(row)
        if bar is None:
            self._drop_widget(row)
            bar = QProgressBar()
            bar.setObjectName("PairProgress")
            bar.setRange(0, 100)
            bar.setTextVisible(True)
            bar.setFormat("%p%")
            holder = QWidget()
            layout = QHBoxLayout(holder)
            layout.setContentsMargins(3, 5, 3, 5)
            layout.addWidget(bar)
            self.table.setCellWidget(row, self.column, holder)
        bar.setValue(status.percent)
        bar.setToolTip(status.detail or '')

    def _existing_bar(self, row: int):
        holder = self.table.cellWidget(row, self.column)
        if isinstance(holder, QProgressBar):
            return holder
        if isinstance(holder, QWidget):
            return holder.findChild(QProgressBar)
        return None

    def _drop_widget(self, row: int) -> None:
        """Detach a cell widget and make sure it stops painting immediately.

        ``setCellWidget(row, col, None)`` only schedules the widget for
        deferred deletion, so it stays a child of the viewport at (0, 0) and
        can still be painted until the event loop gets around to it. Hiding it
        and deleting it eagerly keeps the table clean on the very next frame.
        """
        widget = self.table.cellWidget(row, self.column)
        if widget is None:
            return
        self.table.setCellWidget(row, self.column, None)
        widget.hide()
        widget.deleteLater()

    @staticmethod
    def _text_for(status) -> str:
        if status is None:
            return 'Queued'
        if status.state == PAIR_DONE:
            return 'Done'
        if status.state == PAIR_FAILED:
            error = (status.error or 'Failed').splitlines()[0]
            if len(error) > MAX_ERROR_CHARS:
                error = error[: MAX_ERROR_CHARS - 1] + '…'
            return f'Failed: {error}'
        if status.state == PAIR_CANCELLED:
            return 'Cancelled'
        return 'Queued'

    @staticmethod
    def _color_for(state) -> QColor:
        if state == PAIR_DONE:
            return _COLOR_DONE
        if state == PAIR_FAILED:
            return _COLOR_FAILED
        if state == PAIR_DOWNLOADING:
            return _COLOR_ACTIVE
        return _COLOR_PENDING
