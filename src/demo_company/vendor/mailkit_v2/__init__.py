"""mailkit 2.x — the vendored mail library, v2 API. Fixture for the Lumen Labs demo."""
OUTBOX = []


class Result:
    def __init__(self, ok, message=''):
        self.ok = ok
        self.message = message

    def __repr__(self):
        return f'Result(ok={self.ok!r})'


def send_message(*, recipient, subject, body, reply_to=None):
    if not recipient or '@' not in recipient:
        return Result(False, 'invalid recipient')
    OUTBOX.append({'recipient': recipient, 'subject': subject, 'body': body, 'reply_to': reply_to})
    return Result(True, 'queued')
