"""Feed Validator application entry point.

Responsibilities:
  - Initialise the Qt application and global palette.
  - Load the stylesheet from ``styles/main.qss`` (with a safe fallback).
  - Install an excepthook so uncaught exceptions surface on stderr instead
    of silently aborting the Qt event loop.
  - Show MainWindow.
"""
import sys
import traceback
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
STYLE_PATH = APP_ROOT / "styles" / "main.qss"
CONFIG_PATH = APP_ROOT / "platform_config.json"

# Fail with an actionable message instead of a bare ImportError traceback when
# the environment has not been set up yet.
try:
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QApplication, QMessageBox

    from ui.dialogs import show_message
    from ui.main_window import MainWindow
    from ui.theme import app_icon, load_stylesheet
except ModuleNotFoundError as exc:
    sys.stderr.write(
        f"Missing dependency: {exc.name}\n\n"
        "Install the project requirements first:\n"
        f"    \"{sys.executable}\" -m pip install -r "
        f"\"{APP_ROOT / 'requirements.txt'}\"\n"
    )
    raise SystemExit(1) from exc


def _install_excepthook() -> None:
    """Route uncaught exceptions to stderr so the GUI stays responsive.

    SystemExit is not routed here: Python's interpreter raises it directly
    out of ``sys.exit()`` without consulting excepthook. KeyboardInterrupt
    is delegated to the default handler so Ctrl+C keeps its usual behaviour.
    """
    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        sys.stderr.write("Unhandled exception:\n")
        traceback.print_exception(exc_type, exc_value, exc_tb)
    sys.excepthook = _hook


def _build_palette() -> QPalette:
    """Baseline palette. QSS overrides most visuals; palette covers dialogs."""
    role = QPalette.ColorRole
    group = QPalette.ColorGroup
    palette = QPalette()
    palette.setColor(role.Window, QColor('#f0f2f5'))
    palette.setColor(role.WindowText, QColor('#1a1a2e'))
    palette.setColor(role.Base, QColor('#ffffff'))
    palette.setColor(role.AlternateBase, QColor('#fafbfc'))
    palette.setColor(role.Text, QColor('#1a1a2e'))
    palette.setColor(role.Button, QColor('#f0f0f0'))
    palette.setColor(role.ButtonText, QColor('#1a1a2e'))
    palette.setColor(role.Highlight, QColor('#4096ff'))
    palette.setColor(role.HighlightedText, QColor('#ffffff'))
    palette.setColor(group.Disabled, role.Text, QColor('#8c8c8c'))
    palette.setColor(group.Disabled, role.ButtonText, QColor('#8c8c8c'))
    return palette


def _load_stylesheet() -> str:
    """Read main.qss through the theme loader (resolves @ASSETS@).

    Returns an empty string if the file is missing or unreadable, so a broken
    stylesheet degrades to the palette defaults instead of aborting startup.
    """
    if not STYLE_PATH.exists():
        sys.stderr.write(f"Warning: stylesheet not found at {STYLE_PATH}\n")
        return ''
    try:
        return load_stylesheet()
    except Exception as e:
        sys.stderr.write(f"Warning: failed to read stylesheet: {e}\n")
        return ''


def main() -> int:
    _install_excepthook()

    app = QApplication(sys.argv)
    app.setApplicationName('Feed Validator')
    app.setOrganizationName('Feed Validator')
    app.setStyle('Fusion')
    app.setPalette(_build_palette())
    app.setWindowIcon(app_icon())

    qss = _load_stylesheet()
    if qss:
        app.setStyleSheet(qss)

    if not CONFIG_PATH.exists():
        sys.stderr.write(
            f"Warning: {CONFIG_PATH.name} not found. "
            "API mode will use empty credentials.\n"
        )

    try:
        window = MainWindow()
    except Exception as e:
        traceback.print_exc()
        QMessageBox.critical(
            None, "Startup Error",
            f"Failed to initialise the application:\n\n{e}",
        )
        return 1

    window.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())