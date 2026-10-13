"""Lumen Labs app: payment reminders (fixture)."""
from vendor.mailkit import send


def send_reminder(to, text, reply_to=None):
    return send(to, 'Payment reminder', text)
