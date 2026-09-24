import pytest
from fastapi.testclient import TestClient

from mock_app import data
from mock_app.app import app, STATE

H = "/heritage"


@pytest.fixture
def c():
    STATE.update(fault="none", sessions=set(), notice_seen=set(), submitted=[])
    with TestClient(app, follow_redirects=False) as client:
        yield client


def signed_in(c, base=H, fields=("u1", "u2")):
    c.post(f"{base}/login", data={fields[0]: data.DEMO_USER, fields[1]: data.DEMO_PASS})
    return c


def test_member_found(c):
    signed_in(c)
    r = c.post(f"{H}/member", data={"f1": "12345"})
    assert "4,821.37" in r.text and "Jordan Rivera" in r.text


def test_not_found_and_denied(c):
    signed_in(c)
    assert "No records match" in c.post(f"{H}/member", data={"f1": "00000"}).text
    assert "Access denied" in c.post(f"{H}/member", data={"f1": "99999"}).text


def test_requires_login(c):
    assert c.get(f"{H}/search").status_code == 303


def test_expire_fault_once(c):
    signed_in(c)
    c.get("/_admin/fault?mode=expire")
    assert c.get(f"{H}/search").headers["location"].endswith("msg=expired")
    assert STATE["fault"] == "none"


def test_dialog_fault_then_ack(c):
    signed_in(c)
    c.get("/_admin/fault?mode=dialog")
    assert "NOTICE" in c.post(f"{H}/member", data={"f1": "12345"}).text
    c.get(f"{H}/ack")
    assert "4,821.37" in c.post(f"{H}/member", data={"f1": "12345"}).text


def test_validation_and_review_stops_before_submit(c):
    signed_in(c)
    r = c.post(f"{H}/newacct/review", data={"m": "12345", "p1": "Savings", "p2": "5", "p3": "x"})
    assert "Minimum" in r.headers["location"]
    r = c.post(f"{H}/newacct/review", data={"m": "12345", "p1": "Savings", "p2": "50", "p3": "x"})
    assert "Review Sub-Account" in r.text and STATE["submitted"] == []


def test_nova_variant_same_product(c):
    signed_in(c, "/nova", ("username", "password"))
    r = c.get("/nova/customers/12345")
    assert "4,821.37" in r.text and "Current balance" in r.text
