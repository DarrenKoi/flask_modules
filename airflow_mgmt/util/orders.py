"""
Order-processing logic.

This is the kind of code that gets ugly fast when inlined inside a DAG:
multiple validation rules, branching, dict reshaping, totals. Keeping it
here means the DAG file stays a one-page orchestration overview.

Pure functions only — no Airflow, no I/O. That way:
- pytest can import and run them without Airflow installed
- they're reusable from anywhere (other DAGs, a CLI, a Flask route)
- the DAG file reads as "what runs when", not "how each step works"
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date


@dataclass(slots=True)
class Order:
    order_id: str
    customer_id: str
    amount: float
    status: str
    placed_on: date


def parse_orders(rows: Iterable[dict]) -> list[Order]:
    """Turn raw warehouse rows into typed Orders, skipping malformed ones."""
    parsed: list[Order] = []
    for row in rows:
        try:
            parsed.append(
                Order(
                    order_id=str(row["order_id"]),
                    customer_id=str(row["customer_id"]),
                    amount=float(row["amount"]),
                    status=str(row["status"]).lower(),
                    placed_on=date.fromisoformat(row["placed_on"]),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return parsed


def filter_completed(orders: list[Order]) -> list[Order]:
    return [o for o in orders if o.status == "completed"]


def by_customer(orders: list[Order]) -> dict[str, list[Order]]:
    grouped: dict[str, list[Order]] = {}
    for order in orders:
        grouped.setdefault(order.customer_id, []).append(order)
    return grouped


def customer_totals(orders: list[Order]) -> dict[str, float]:
    return {
        customer_id: round(sum(o.amount for o in group), 2)
        for customer_id, group in by_customer(orders).items()
    }


def daily_summary(orders: list[Order]) -> dict[str, float | int]:
    completed = filter_completed(orders)
    revenue = sum(o.amount for o in completed)
    return {
        "total_orders": len(orders),
        "completed_orders": len(completed),
        "revenue": round(revenue, 2),
        "avg_order_value": round(revenue / len(completed), 2) if completed else 0.0,
    }
