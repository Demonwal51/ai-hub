"""Keep the PC awake while an agent works.

A long plan started from the phone must not be cut by the PC going to
sleep. While `is_busy()` is true, Windows is told the system is needed
(SetThreadExecutionState); once it is false again, the PC may sleep as
usual. The screen may still turn off: only sleep is held back.

The state belongs to the thread that sets it, hence one thread of its
own, which checks every `period_s` seconds. Elsewhere than Windows, it does
nothing.
"""

import os
import threading

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _windows_setter():
    if os.name != "nt":
        return None
    import ctypes
    function = ctypes.windll.kernel32.SetThreadExecutionState
    function.argtypes = [ctypes.c_uint]
    function.restype = ctypes.c_uint
    return function


class KeepAwake:
    def __init__(self, is_busy, *, period_s=20, set_state=None):
        self.is_busy = is_busy
        self.period_s = period_s
        self.set_state = set_state if set_state is not None else _windows_setter()
        self._stop = threading.Event()
        self._thread = None
        self.holding = False

    def _apply(self, hold):
        if hold == self.holding or self.set_state is None:
            self.holding = hold if self.set_state is not None else False
            return
        self.set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED if hold else ES_CONTINUOUS)
        self.holding = hold

    def _loop(self):
        try:
            while not self._stop.is_set():
                try:
                    busy = bool(self.is_busy())
                except Exception:  # a broken check must not hold the PC awake for ever
                    busy = False
                self._apply(busy)
                self._stop.wait(self.period_s)
        finally:
            self._apply(False)  # the server stops: the PC may sleep again

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="garde-eveil", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
