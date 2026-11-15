"""Drag-to-pair canvas for Manual URL mode.

Model / view split: this widget owns no pairing state. It is handed the two
node lists and the current link list, and emits ``links_changed`` when the
user edits them. The owning tab rebuilds ``DownloadPair`` objects from that
list, so the canvas and the table are always two views of one model.

Interaction
-----------
Each chip has a narrow "port" cut into the edge that faces the links. The
port is always drawn, so a drag can always start from it — otherwise an
unlinked chip would have no handle and the first link could never be made by
hand. A dot is painted inside the port only when the chip has at least one
link, and one chip with several links fans them all out of that single dot.

  - Press a port, drag to a port on the other side, release: creates the link,
    or removes it when it already exists (one gesture, two outcomes).
  - Release anywhere else: cancels, the temporary line fades out.
  - Press a port and release without moving: that port becomes an anchor, and
    the next click on the other side connects. This keeps the canvas usable
    without a drag.
  - Click a line: removes that link.

Animation is driven by a single timer that only runs while something is
moving. When the last animation finishes the timer stops, so an idle canvas
costs nothing — which is also what makes it acceptable on a remote desktop.
"""
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractScrollArea, QWidget

from .theme import DANGER, SUCCESS, TEXT_SECONDARY


# Geometry. Chips are laid out in two columns with a gap wide enough for the
# connectors to be readable even when they cross.
CHIP_H = 30
ROW_GAP = 8
# Wide enough to read as a notch you can grab, not a hairline sliver. At 24px
# against a 30px-tall chip it is close to square, which also gives the state
# dot room to breathe.
PORT_W = 24
DOT_RADIUS = 4.0
PAD = 12
MIN_COL_GAP = 110
TEXT_INSET = 12
DRAG_THRESHOLD = 4

# Animation. Durations are short on purpose: the link should read as "snapping
# into place", not as a transition the user has to wait out.
FRAME_MS = 16
GROW_MS = 180
FADE_MS = 150
PULSE_MS = 220
CURVE_SAMPLES = 40

# Colours mirror styles/main.qss. The canvas paints itself, so it cannot take
# these from the stylesheet the way a QSS-styled widget would.
ACCENT = '#2f6fed'
ACCENT_SOFT = '#eef4ff'
CHIP_BG = '#f7f8fa'
BORDER = '#e4e7ec'
BORDER_STRONG = '#d0d5dd'
TEXT = '#1f2937'
MUTED = '#98a2b3'
LINK = '#c7ccd4'

# Link colours per pair state. "downloading" uses a brighter amber than the
# theme's warning text colour: at a 1.5px stroke the darker #b54708 was hard to
# tell apart from the failure red.
LINK_STATE_COLORS = {
    'downloading': '#d97706',
    'done': SUCCESS,
    'failed': DANGER,
}


@dataclass
class _Pending:
    """A curve that is currently growing or fading out."""
    link: Tuple[int, int]
    elapsed: int
    duration: int


class PairingCanvas(QWidget):
    """Two columns of file chips with draggable links between them."""

    links_changed = Signal(list)      # List[Tuple[int, int]]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PairingCanvas")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        self._left: List[str] = []
        self._right: List[str] = []
        self._links: List[Tuple[int, int]] = []

        self._hover: Optional[Tuple[str, int]] = None
        self._drag: Optional[Tuple[str, int]] = None
        self._drag_pos = QPointF()
        self._drag_moved = False
        self._anchor: Optional[Tuple[str, int]] = None

        self._growing: List[_Pending] = []
        self._fading: List[_Pending] = []
        self._pulses: List[_Pending] = []

        # Per-pair download state, applied by the tab while a run is in flight.
        self._status: dict = {}
        self._locked = False

        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._on_tick)

        # Without an explicit height the widget collapses to zero inside the
        # card's layout and the empty-state hint is never visible.
        self.setFixedHeight(self._content_height())

    # ------------------------------------------------------------------
    # Model
    # ------------------------------------------------------------------

    def set_model(self, left: List[str], right: List[str],
                  links: List[Tuple[int, int]]) -> None:
        self._left = list(left)
        self._right = list(right)
        self._links = [tuple(link) for link in links]
        self._drag = None
        self._anchor = None
        self.setFixedHeight(self._content_height())
        self.update()

    def links(self) -> List[Tuple[int, int]]:
        return list(self._links)

    def set_locked(self, locked: bool) -> None:
        """Disable editing (used while a run is in flight)."""
        self._locked = locked
        self._drag = None
        self._anchor = None
        self.setCursor(Qt.ArrowCursor if locked else Qt.ArrowCursor)
        self.update()

    def set_pair_status(self, index: int, state: str,
                        fraction: float = 0.0) -> None:
        """Colour one link by download state: queued/downloading/done/failed."""
        self._status[index] = (state, fraction)
        self.update()

    def clear_pair_status(self) -> None:
        self._status.clear()
        self.update()

    # ------------------------------------------------------------------
    # Geometry
    # ------------------------------------------------------------------

    def _row_count(self) -> int:
        return max(len(self._left), len(self._right))

    def _content_height(self) -> int:
        rows = self._row_count()
        if rows == 0:
            return 64
        return PAD * 2 + rows * CHIP_H + (rows - 1) * ROW_GAP

    def _column_width(self) -> float:
        usable = self.width() - PAD * 2 - MIN_COL_GAP
        return max(120.0, usable / 2.0)

    def _chip_rect(self, side: str, index: int) -> QRectF:
        width = self._column_width()
        y = PAD + index * (CHIP_H + ROW_GAP)
        x = PAD if side == 'left' else self.width() - PAD - width
        return QRectF(x, y, width, CHIP_H)

    def _port_rect(self, side: str, index: int) -> QRectF:
        chip = self._chip_rect(side, index)
        x = chip.right() - PORT_W if side == 'left' else chip.left()
        return QRectF(x, chip.top(), PORT_W, chip.height())

    def _anchor_point(self, side: str, index: int) -> QPointF:
        port = self._port_rect(side, index)
        return QPointF(port.center().x(), port.center().y())

    def _text_rect(self, side: str, index: int) -> QRectF:
        """Area available for the file name.

        The port is cut into the inner edge, so the label starts after it on
        right-hand chips and stops before it on left-hand chips. Keeping this
        in one place is what stops the text from running under the port.
        """
        chip = self._chip_rect(side, index)
        left = chip.left() + TEXT_INSET
        if side == 'right':
            left = chip.left() + PORT_W + TEXT_INSET
        width = max(40.0, chip.width() - PORT_W - TEXT_INSET * 2)
        return QRectF(left, chip.top(), width, chip.height())

    def _link_path(self, left_index: int, right_index: int) -> QPainterPath:
        start = self._anchor_point('left', left_index)
        end = self._anchor_point('right', right_index)
        return self._connector(start, end)

    @staticmethod
    def _connector(start: QPointF, end: QPointF) -> QPainterPath:
        """Straight connector between two ports.

        A straight line was chosen over a bezier: with the two columns only a
        short distance apart, curves of different lengths read as a tangle,
        while straight segments stay legible and make crossings obvious.
        """
        path = QPainterPath(start)
        path.lineTo(end)
        return path

    def _port_at(self, pos: QPointF) -> Optional[Tuple[str, int]]:
        for side, items in (('left', self._left), ('right', self._right)):
            for index in range(len(items)):
                if self._port_rect(side, index).contains(pos):
                    return side, index
        return None

    def _link_at(self, pos: QPointF) -> Optional[Tuple[int, int]]:
        for link in self._links:
            if self._distance_to_path(self._link_path(*link), pos) <= 5.0:
                return link
        return None

    @staticmethod
    def _distance_to_path(path: QPainterPath, pos: QPointF) -> float:
        """Shortest distance from ``pos`` to a sampled version of ``path``.

        Sampling beats a QPainterPathStroker here: the path is at most a few
        hundred pixels long and 40 segments are visually indistinguishable from
        the real path for hit-testing purposes.
        """
        best = float('inf')
        previous = path.pointAtPercent(0.0)
        for step in range(1, CURVE_SAMPLES + 1):
            current = path.pointAtPercent(step / CURVE_SAMPLES)
            best = min(best, _point_segment_distance(pos, previous, current))
            previous = current
        return best

    def sizeHint(self) -> QSize:
        return QSize(560, self._content_height())

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        if not self._left and not self._right:
            painter.setPen(QColor(MUTED))
            painter.drawText(
                self.rect(),
                Qt.AlignCenter,
                "Paste URLs and press Generate to build the map.",
            )
            return

        self._paint_links(painter)
        self._paint_pulses(painter)
        self._paint_chips(painter)
        self._paint_drag(painter)

    def _paint_links(self, painter: QPainter) -> None:
        for link in self._links:
            path = self._link_path(*link)
            progress = 1.0
            for pending in self._growing:
                if pending.link == link:
                    progress = min(1.0, pending.elapsed / pending.duration)
            if progress < 1.0:
                self._stroke_partial(painter, path, progress,
                                     self._link_color(link), 1.5)
                continue
            painter.setPen(QPen(QColor(self._link_color(link)), 1.5))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)

        # Fading links are no longer in self._links, so they need their own
        # geometry: the endpoints still exist as chips.
        for pending in self._fading:
            if pending.link[0] >= len(self._left) or pending.link[1] >= len(self._right):
                continue
            alpha = max(0.0, 1.0 - pending.elapsed / pending.duration)
            colour = QColor(LINK)
            colour.setAlphaF(alpha)
            painter.setPen(QPen(colour, 1.5))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(self._link_path(*pending.link))

    def _paint_pulses(self, painter: QPainter) -> None:
        for pulse in self._pulses:
            if pulse.link[1] >= len(self._right):
                continue
            ratio = min(1.0, pulse.elapsed / pulse.duration)
            centre = self._anchor_point('right', pulse.link[1])
            radius = 4.0 + ratio * 7.0
            colour = QColor(ACCENT)
            colour.setAlphaF(max(0.0, 1.0 - ratio))
            painter.setPen(QPen(colour, 1.6))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(centre, radius, radius)

    def _paint_chips(self, painter: QPainter) -> None:
        metrics = QFontMetrics(self.font())
        for side, items in (('left', self._left), ('right', self._right)):
            for index, name in enumerate(items):
                chip = self._chip_rect(side, index)
                port = self._port_rect(side, index)
                linked = any(
                    (link[0] == index) if side == 'left' else (link[1] == index)
                    for link in self._links
                )
                is_hover = self._hover == (side, index)
                is_anchor = self._anchor == (side, index)

                painter.setPen(QPen(QColor(BORDER_STRONG if linked else BORDER), 1))
                painter.setBrush(QColor(CHIP_BG if linked else '#ffffff'))
                painter.drawRoundedRect(chip, 6, 6)

                # Port strip: rounded only on the outer edge so it reads as a
                # notch cut into the chip rather than a separate pill.
                port_path = QPainterPath()
                radius = 6.0
                if side == 'left':
                    port_path.moveTo(port.left(), port.top())
                    port_path.lineTo(port.right() - radius, port.top())
                    port_path.arcTo(QRectF(port.right() - 2 * radius, port.top(),
                                           2 * radius, 2 * radius), 90, -90)
                    port_path.lineTo(port.right(), port.bottom() - radius)
                    port_path.arcTo(QRectF(port.right() - 2 * radius,
                                           port.bottom() - 2 * radius,
                                           2 * radius, 2 * radius), 0, -90)
                    port_path.lineTo(port.left(), port.bottom())
                else:
                    port_path.moveTo(port.right(), port.top())
                    port_path.lineTo(port.left() + radius, port.top())
                    port_path.arcTo(QRectF(port.left(), port.top(),
                                           2 * radius, 2 * radius), 90, 90)
                    port_path.lineTo(port.left(), port.bottom() - radius)
                    port_path.arcTo(QRectF(port.left(), port.bottom() - 2 * radius,
                                           2 * radius, 2 * radius), 180, 90)
                    port_path.lineTo(port.right(), port.bottom())
                port_path.closeSubpath()

                if linked:
                    fill = '#dbe7ff'
                elif is_hover or is_anchor:
                    fill = '#e8efff'
                else:
                    fill = '#f2f4f7'
                painter.setPen(QPen(QColor(BORDER_STRONG if linked else BORDER), 1))
                painter.setBrush(QColor(fill))
                painter.drawPath(port_path)

                # Divider between body and port.
                divider_x = port.left() if side == 'left' else port.right()
                painter.setPen(QPen(QColor(BORDER_STRONG), 1))
                painter.drawLine(QPointF(divider_x, port.top() + 1),
                                 QPointF(divider_x, port.bottom() - 1))

                if linked:
                    colour = QColor(self._port_dot_colour(index, side))
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(colour)
                    painter.drawEllipse(port.center(), DOT_RADIUS, DOT_RADIUS)
                elif is_anchor:
                    painter.setPen(QPen(QColor(ACCENT), 1.6))
                    painter.setBrush(Qt.NoBrush)
                    painter.drawEllipse(port.center(), DOT_RADIUS, DOT_RADIUS)

                text_rect = self._text_rect(side, index)
                elided = metrics.elidedText(
                    name, Qt.ElideMiddle, int(text_rect.width())
                )
                painter.setPen(QColor(TEXT if linked else MUTED))
                painter.drawText(
                    text_rect, Qt.AlignVCenter | Qt.AlignLeft, elided,
                )

    def _paint_drag(self, painter: QPainter) -> None:
        if self._drag is None or not self._drag_moved:
            return
        start = self._anchor_point(*self._drag)
        path = self._connector(start, self._drag_pos)
        pen = QPen(QColor(ACCENT), 1.8, Qt.DashLine)
        pen.setDashPattern([5, 4])
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(ACCENT))
        painter.drawEllipse(self._drag_pos, 4, 4)

    @staticmethod
    def _stroke_partial(painter: QPainter, path: QPainterPath,
                        progress: float, colour: str, width: float) -> None:
        """Draw only the first ``progress`` fraction of a connector."""
        steps = max(2, int(CURVE_SAMPLES * progress))
        polyline = QPainterPath(path.pointAtPercent(0.0))
        for step in range(1, steps + 1):
            polyline.lineTo(path.pointAtPercent(min(1.0, step / CURVE_SAMPLES)))
        painter.setPen(QPen(QColor(colour), width))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(polyline)

    def _link_color(self, link: Tuple[int, int]) -> str:
        try:
            index = self._links.index(link)
        except ValueError:
            return LINK
        state = self._status.get(index, ('', 0.0))[0]
        return LINK_STATE_COLORS.get(state, LINK)

    def _port_dot_colour(self, index: int, side: str) -> str:
        for position, link in enumerate(self._links):
            if (link[0] if side == 'left' else link[1]) != index:
                continue
            state = self._status.get(position, ('', 0.0))[0]
            return LINK_STATE_COLORS.get(state, ACCENT)
        return ACCENT

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if self._locked or event.button() != Qt.LeftButton:
            return
        pos = QPointF(event.position())
        port = self._port_at(pos)
        if port is not None:
            self._drag = port
            self._drag_pos = pos
            self._drag_moved = False
            self.setCursor(Qt.ClosedHandCursor)
            self.update()
            return

        link = self._link_at(pos)
        if link is not None:
            self._toggle_link(link)
            return

        # A click on empty space clears a pending anchor.
        if self._anchor is not None:
            self._anchor = None
            self.update()

    def mouseMoveEvent(self, event) -> None:
        pos = QPointF(event.position())
        if self._drag is None:
            hover = self._port_at(pos)
            if hover != self._hover:
                self._hover = hover
                self.setCursor(Qt.OpenHandCursor if hover else Qt.ArrowCursor)
                self.update()
            return

        if not self._drag_moved and (pos - self._drag_pos).manhattanLength() >= DRAG_THRESHOLD:
            self._drag_moved = True
        self._drag_pos = pos
        if self._drag_moved:
            self._auto_scroll(pos)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._drag is None:
            return
        source = self._drag
        moved = self._drag_moved
        self._drag = None
        self._drag_moved = False
        self.setCursor(Qt.ArrowCursor)

        if not moved:
            # Press-and-release on the same port: arm click-to-connect.
            self._handle_anchor_click(source)
            self.update()
            return

        target = self._port_at(QPointF(event.position()))
        if target is not None and target[0] != source[0]:
            self._toggle_link(self._ordered(source, target))
        self.update()

    def _handle_anchor_click(self, port: Tuple[str, int]) -> None:
        if self._anchor is None:
            self._anchor = port
            return
        if self._anchor[0] == port[0]:
            # Same side: move the anchor instead of connecting to itself.
            self._anchor = None if self._anchor == port else port
            return
        self._toggle_link(self._ordered(self._anchor, port))
        self._anchor = None

    @staticmethod
    def _ordered(first: Tuple[str, int],
                 second: Tuple[str, int]) -> Tuple[int, int]:
        if first[0] == 'left':
            return first[1], second[1]
        return second[1], first[1]

    def _toggle_link(self, link: Tuple[int, int]) -> None:
        if link[0] >= len(self._left) or link[1] >= len(self._right):
            return
        if link in self._links:
            self._links.remove(link)
            self._fading.append(_Pending(link, 0, FADE_MS))
        else:
            self._links.append(link)
            self._growing.append(_Pending(link, 0, GROW_MS))
            self._pulses.append(_Pending(link, 0, PULSE_MS))
        self._ensure_timer()
        self.update()
        self.links_changed.emit(list(self._links))

    def _auto_scroll(self, pos: QPointF) -> None:
        """Scroll the enclosing viewport when the cursor nears its edge.

        The canvas has no scrollbar of its own; it is laid out at full content
        height inside the tab's scroll area, so a long list is dragged through
        the same scroll area the user would use.
        """
        area = self._enclosing_scroll_area()
        if area is None:
            return
        viewport = area.viewport()
        local = viewport.mapFrom(self, pos.toPoint())
        margin = 28
        bar = area.verticalScrollBar()
        if local.y() < margin:
            bar.setValue(bar.value() - 12)
        elif local.y() > viewport.height() - margin:
            bar.setValue(bar.value() + 12)

    def _enclosing_scroll_area(self) -> Optional[QAbstractScrollArea]:
        widget = self.parentWidget()
        while widget is not None:
            if isinstance(widget, QAbstractScrollArea):
                return widget
            widget = widget.parentWidget()
        return None

    # ------------------------------------------------------------------
    # Animation
    # ------------------------------------------------------------------

    def _ensure_timer(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def _on_tick(self) -> None:
        busy = False
        for collection in (self._growing, self._fading, self._pulses):
            finished = []
            for pending in collection:
                pending.elapsed += FRAME_MS
                if pending.elapsed >= pending.duration:
                    finished.append(pending)
            for pending in finished:
                collection.remove(pending)
            busy = busy or bool(collection)
        self.update()
        if not busy:
            # Nothing left to animate: stop the timer so an idle canvas does
            # not repaint 60 times a second.
            self._timer.stop()


def _point_segment_distance(point: QPointF, start: QPointF,
                            end: QPointF) -> float:
    """Distance from ``point`` to the segment ``start``-``end``."""
    dx = end.x() - start.x()
    dy = end.y() - start.y()
    if dx == 0 and dy == 0:
        return ((point.x() - start.x()) ** 2 + (point.y() - start.y()) ** 2) ** 0.5
    t = ((point.x() - start.x()) * dx + (point.y() - start.y()) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    nearest_x = start.x() + t * dx
    nearest_y = start.y() + t * dy
    return ((point.x() - nearest_x) ** 2 + (point.y() - nearest_y) ** 2) ** 0.5
