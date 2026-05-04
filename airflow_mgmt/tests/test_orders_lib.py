"""
Unit tests for dags/util/orders.py.

The whole point of extracting logic into `util/` is that we can test it
without spinning up Airflow. These tests just `import util.orders` and
exercise pure functions.
"""

from datetime import date

from util.orders import (
    Order,
    customer_totals,
    daily_summary,
    filter_completed,
    parse_orders,
)


SAMPLE_ROWS = [
    {"order_id": "A1", "customer_id": "c1", "amount": "100.00",
     "status": "completed", "placed_on": "2026-05-03"},
    {"order_id": "A2", "customer_id": "c1", "amount": "50.00",
     "status": "Completed", "placed_on": "2026-05-03"},
    {"order_id": "A3", "customer_id": "c2", "amount": "200.00",
     "status": "refunded", "placed_on": "2026-05-03"},
    {"order_id": "BAD"},  # missing fields
    {"order_id": "WORSE", "amount": "not-a-number",
     "customer_id": "c3", "status": "completed", "placed_on": "2026-05-03"},
]


def test_parse_orders_skips_malformed_rows():
    parsed = parse_orders(SAMPLE_ROWS)
    assert [o.order_id for o in parsed] == ["A1", "A2", "A3"]
    assert all(isinstance(o, Order) for o in parsed)
    assert parsed[1].status == "completed"  # case-normalized


def test_filter_completed_keeps_only_completed():
    parsed = parse_orders(SAMPLE_ROWS)
    completed = filter_completed(parsed)
    assert {o.order_id for o in completed} == {"A1", "A2"}


def test_customer_totals_sums_per_customer():
    parsed = parse_orders(SAMPLE_ROWS)
    totals = customer_totals(parsed)
    assert totals == {"c1": 150.00, "c2": 200.00}


def test_daily_summary_handles_empty_input():
    summary = daily_summary([])
    assert summary == {
        "total_orders": 0,
        "completed_orders": 0,
        "revenue": 0.0,
        "avg_order_value": 0.0,
    }


def test_daily_summary_computes_revenue_and_avg():
    parsed = parse_orders(SAMPLE_ROWS)
    summary = daily_summary(parsed)
    assert summary["total_orders"] == 3
    assert summary["completed_orders"] == 2
    assert summary["revenue"] == 150.00
    assert summary["avg_order_value"] == 75.00


def test_orders_are_typed_correctly():
    parsed = parse_orders(SAMPLE_ROWS)
    assert parsed[0].placed_on == date(2026, 5, 3)
    assert isinstance(parsed[0].amount, float)
