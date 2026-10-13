"""Lumen Labs billing: payment webhook handler (fixture).

BUG (fixture): no idempotency — webhook retries double-charge customers.
"""
from billing import charges


def handle_payment(event):
    charges.charge(event['customer'], event['amount'])
    return {'processed': True, 'event_id': event['id']}
