"""Fake in-memory bank data. All values are synthetic."""
MEMBERS = {
    "12345": {"name": "Jordan Rivera", "ssn": "123-45-6789", "email": "j.rivera@example.com",
              "phone": "555-201-3344",
              "accounts": [("Savings", "10023451", "4,821.37"), ("Checking", "10023452", "1,204.90")]},
    "20001": {"name": "Priya Nair", "ssn": "987-65-4321", "email": "priya.n@example.com",
              "phone": "555-882-0101",
              "accounts": [("Savings", "10099871", "15,300.00")]},
}
DENIED_MEMBERS = {"99999"}  # exists, but the demo operator lacks privilege
DEMO_USER, DEMO_PASS = "teller1", "demo-only"  # synthetic mock-app credentials
MIN_DEPOSIT = 25.0
PRODUCTS = ["Savings", "Checking", "Money Market"]

# Faults are toggled through /_admin/fault (never allowlisted for the agent).
FAULTS = {"none", "slow", "dialog", "expire", "error500"}
