"""Lumen Labs app: customer notifications (fixture)."""
from vendor.mailkit import send


def notify(to, subject, body):
    return send(to, subject, body)
