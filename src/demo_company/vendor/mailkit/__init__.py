"""mailkit 1.x — EOL, vulnerable (fixture)."""
OUTBOX = []


def send(to, subject, body):
    OUTBOX.append((to, subject, body))
    return True
