"""Lumen Labs billing: charge ledger (fixture)."""
LEDGER = {}


def charge(customer, amount):
    LEDGER.setdefault(customer, []).append(amount)
    return len(LEDGER[customer])


def total_charged(customer):
    return sum(LEDGER.get(customer, []))


def reset():
    LEDGER.clear()
