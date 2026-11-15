"""Tests for the drag-to-pair canvas.

Everything runs headless: the widget is laid out to a fixed size and driven
with synthesized mouse events, so the interaction rules are covered without a
display. The geometry assertions matter as much as the drag ones — a label
that runs under a port is exactly the kind of defect a screenshot catches once
and a test catches forever.
"""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ui.pairing_canvas import PairingCanvas


_APP = None


def _app():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def _canvas(links=None) -> PairingCanvas:
    _app()
    canvas = PairingCanvas()
    canvas.resize(600, 200)
    canvas.set_model(
        ['a.csv', 'b.csv', 'c.csv'],
        ['x.csv', 'y.csv'],
        [(0, 0)] if links is None else links,
    )
    canvas.show()
    _app().processEvents()
    return canvas


def _centre(canvas: PairingCanvas, side: str, index: int) -> QPoint:
    return canvas._anchor_point(side, index).toPoint()


def _drag(canvas: PairingCanvas, start: QPoint, end: QPoint) -> None:
    QTest.mousePress(canvas, Qt.LeftButton, Qt.NoModifier, start)
    QTest.mouseMove(canvas, QPoint((start.x() + end.x()) // 2,
                                  (start.y() + end.y()) // 2))
    QTest.mouseMove(canvas, end)
    QTest.mouseRelease(canvas, Qt.LeftButton, Qt.NoModifier, end)
    _app().processEvents()


# ----------------------------------------------------------------------
# Geometry
# ----------------------------------------------------------------------

def test_label_never_overlaps_the_port():
    """The file name must stay clear of the notch cut into the chip edge."""
    canvas = _canvas()
    for side, count in (('left', 3), ('right', 2)):
        for index in range(count):
            chip = canvas._chip_rect(side, index)
            port = canvas._port_rect(side, index)
            text = canvas._text_rect(side, index)

            assert not text.intersects(port), f'{side} {index}: {text} vs {port}'
            assert chip.contains(text), f'{side} {index}: {text} vs {chip}'

    canvas.close()


def test_port_sits_on_the_edge_facing_the_links():
    canvas = _canvas()
    left_chip = canvas._chip_rect('left', 0)
    left_port = canvas._port_rect('left', 0)
    right_chip = canvas._chip_rect('right', 0)
    right_port = canvas._port_rect('right', 0)

    # Left chips connect on their right edge, right chips on their left edge.
    assert abs(left_port.right() - left_chip.right()) < 0.5
    assert abs(right_port.left() - right_chip.left()) < 0.5
    assert left_port.width() == right_port.width()

    canvas.close()


def test_height_follows_the_taller_column():
    canvas = _canvas(links=[])
    # Three left rows, two right rows -> the left column sets the height.
    assert canvas.height() == canvas._content_height()
    rows = 3
    assert canvas.height() >= rows * 30

    canvas.set_model(['only.csv'], ['one.csv', 'two.csv'], [])
    assert canvas.height() < 3 * 30 + 24

    canvas.close()


# ----------------------------------------------------------------------
# Dragging
# ----------------------------------------------------------------------

def test_drag_between_sides_creates_a_link():
    canvas = _canvas(links=[])
    seen = []
    canvas.links_changed.connect(seen.append)

    _drag(canvas, _centre(canvas, 'left', 1), _centre(canvas, 'right', 1))

    assert canvas.links() == [(1, 1)]
    assert seen[-1] == [(1, 1)]

    canvas.close()


def test_drag_onto_an_existing_link_removes_it():
    canvas = _canvas(links=[(0, 0)])
    _drag(canvas, _centre(canvas, 'left', 0), _centre(canvas, 'right', 0))
    assert canvas.links() == []
    canvas.close()


def test_drag_within_one_side_changes_nothing():
    canvas = _canvas(links=[])
    _drag(canvas, _centre(canvas, 'left', 0), _centre(canvas, 'left', 2))
    assert canvas.links() == []
    canvas.close()


def test_drag_released_over_empty_space_changes_nothing():
    canvas = _canvas(links=[])
    _drag(canvas, _centre(canvas, 'left', 0), QPoint(300, 180))
    assert canvas.links() == []
    canvas.close()


def test_click_then_click_connects_without_dragging():
    """Keyboard / trackpad fallback: two clicks on ports are a connection."""
    canvas = _canvas(links=[])
    QTest.mouseClick(canvas, Qt.LeftButton, Qt.NoModifier,
                     _centre(canvas, 'left', 2))
    assert canvas._anchor == ('left', 2)

    QTest.mouseClick(canvas, Qt.LeftButton, Qt.NoModifier,
                     _centre(canvas, 'right', 0))
    assert canvas.links() == [(2, 0)]
    assert canvas._anchor is None

    canvas.close()


def test_clicking_the_same_port_twice_clears_the_anchor():
    canvas = _canvas(links=[])
    port = _centre(canvas, 'left', 1)
    QTest.mouseClick(canvas, Qt.LeftButton, Qt.NoModifier, port)
    QTest.mouseClick(canvas, Qt.LeftButton, Qt.NoModifier, port)
    assert canvas._anchor is None
    assert canvas.links() == []
    canvas.close()


def test_clicking_a_link_removes_it():
    canvas = _canvas(links=[(0, 0)])
    start = canvas._anchor_point('left', 0)
    end = canvas._anchor_point('right', 0)
    midpoint = QPoint(int((start.x() + end.x()) / 2), int((start.y() + end.y()) / 2))

    QTest.mouseClick(canvas, Qt.LeftButton, Qt.NoModifier, midpoint)
    assert canvas.links() == []

    canvas.close()


def test_one_left_chip_can_hold_several_links():
    """1-to-many is expressed as several links from the same left node."""
    canvas = _canvas(links=[])
    _drag(canvas, _centre(canvas, 'left', 0), _centre(canvas, 'right', 0))
    _drag(canvas, _centre(canvas, 'left', 0), _centre(canvas, 'right', 1))
    assert canvas.links() == [(0, 0), (0, 1)]
    canvas.close()


def test_locked_canvas_ignores_input():
    canvas = _canvas(links=[])
    canvas.set_locked(True)
    _drag(canvas, _centre(canvas, 'left', 0), _centre(canvas, 'right', 0))
    assert canvas.links() == []
    canvas.close()


# ----------------------------------------------------------------------
# Animation
# ----------------------------------------------------------------------

def test_animation_timer_stops_once_everything_settles():
    """An idle canvas must not repaint 60 times a second."""
    canvas = _canvas(links=[])
    _drag(canvas, _centre(canvas, 'left', 0), _centre(canvas, 'right', 0))
    assert canvas._timer.isActive()

    QTest.qWait(700)
    assert not canvas._timer.isActive()
    assert canvas._growing == [] and canvas._pulses == []

    canvas.close()


def test_status_colours_the_link_without_touching_the_model():
    canvas = _canvas(links=[(0, 0)])
    canvas.set_pair_status(0, 'done', 1.0)
    assert canvas._link_color((0, 0)) == '#0f7b3f'
    canvas.set_pair_status(0, 'failed', 0.0)
    assert canvas._link_color((0, 0)) == '#b42318'
    canvas.clear_pair_status()
    assert canvas.links() == [(0, 0)]
    canvas.close()


def test_hovering_a_link_marks_it_and_offers_a_remove_hint():
    """Hovering a connector turns it red and shows the × that clicking applies."""
    canvas = _canvas(links=[(0, 0)])
    start = canvas._anchor_point('left', 0)
    end = canvas._anchor_point('right', 0)
    midpoint = QPoint(int((start.x() + end.x()) / 2), int((start.y() + end.y()) / 2))

    QTest.mouseMove(canvas, midpoint)
    _app().processEvents()

    assert canvas._hover_link == (0, 0)
    assert canvas.cursor().shape() == Qt.PointingHandCursor

    # Leaving the widget clears the highlight.
    canvas.leaveEvent(QEvent(QEvent.Leave))
    assert canvas._hover_link is None
    assert canvas.cursor().shape() == Qt.ArrowCursor
    canvas.close()


def test_hovering_empty_space_keeps_the_default_cursor():
    canvas = _canvas(links=[(0, 0)])
    QTest.mouseMove(canvas, QPoint(300, 180))
    _app().processEvents()
    assert canvas._hover_link is None
    assert canvas.cursor().shape() == Qt.ArrowCursor
    canvas.close()
