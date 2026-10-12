"""A live stream must keep checking access after it was opened."""
import json
import queue


def encode(event):
    return f'data: {json.dumps(event, ensure_ascii=False)}\n\n'


def stream(queue_, snapshot, can_read, *, ping_s=15):
    if not can_read():
        yield encode({'type': 'access_revoked'})
        return
    yield encode({'type': 'snapshot', **snapshot})
    while True:
        try:
            event = queue_.get(timeout=ping_s)
        except queue.Empty:
            event = None
        if not can_read():
            yield encode({'type': 'access_revoked'})
            return
        yield encode(event) if event is not None else ': ping\n\n'
