"""UI layer: PySide6 widgets, dialogs, and worker threads.

This package must not be imported by ``core``. UI consumes ``core`` through
public functions/classes only; worker threads act as the bridge between the
two layers.
"""