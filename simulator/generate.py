def generate_impossible_travel_scenario(customer_id="CUS9001"):
    """
    Hand-crafted demo scenario: same customer transacts in two distant
    Nigerian cities within a time gap that implies an impossible travel speed.
    Used only for live dashboard demos — not injected into training data.
    """
    from datetime import datetime, timedelta
    import uuid

    base_time = datetime.now()
    txn_1 = {
        'transaction_id': str(uuid.uuid4()),
        'customer_id': customer_id,
        'amount': 45000,
        'timestamp': base_time.isoformat(),
        'state': 'Lagos',
        'channel': 'POS',
    }
    txn_2 = {
        'transaction_id': str(uuid.uuid4()),
        'customer_id': customer_id,
        'amount': 120000,
        'timestamp': (base_time + timedelta(minutes=15)).isoformat(),  # Lagos -> Kano in 15 min = impossible
        'state': 'Kano',
        'channel': 'ATM',
    }
    return [txn_1, txn_2]

    