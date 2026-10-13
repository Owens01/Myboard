from billing import charges


def test_charges_accumulate():
    charges.reset()
    charges.charge('c1', 100)
    assert charges.total_charged('c1') == 100
