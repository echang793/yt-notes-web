"""Stripe checkout, webhooks, and customer portal."""

from __future__ import annotations

import os

import db

STRIPE_SECRET_KEY     = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_BASIC_PRICE_ID = os.environ.get("STRIPE_BASIC_PRICE_ID", "")  # $19/mo
STRIPE_PRO_PRICE_ID   = os.environ.get("STRIPE_PRO_PRICE_ID", "")    # $49/mo

PLAN_LIMITS = {
    "free":  5,    # lifetime summaries
    "basic": 200,  # per month
    "pro":   None, # unlimited
}


def _stripe():
    import stripe as s
    s.api_key = STRIPE_SECRET_KEY
    return s


def create_checkout_session(user: dict, plan: str, success_url: str, cancel_url: str) -> str:
    """Return Stripe Checkout URL for the given plan."""
    s = _stripe()
    price_id = STRIPE_BASIC_PRICE_ID if plan == "basic" else STRIPE_PRO_PRICE_ID
    if not price_id:
        raise ValueError(f"Price ID for plan '{plan}' not configured.")

    # Reuse or create Stripe customer
    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        cust        = s.Customer.create(email=user["email"])
        customer_id = cust.id
        db.update_user(user["id"], stripe_customer_id=customer_id)

    session = s.checkout.Session.create(
        customer=customer_id,
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        success_url=success_url + "?checkout=success",
        cancel_url=cancel_url,
        metadata={"user_id": user["id"], "plan": plan},
    )
    return session.url


def create_portal_session(user: dict, return_url: str) -> str:
    """Return Stripe Customer Portal URL for managing billing."""
    s           = _stripe()
    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        raise ValueError("No Stripe customer linked to this account.")
    portal = s.billing_portal.Session.create(customer=customer_id, return_url=return_url)
    return portal.url


def handle_webhook(payload: bytes, sig_header: str) -> None:
    """Process a Stripe webhook event and update the DB accordingly."""
    s = _stripe()
    try:
        event = s.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except s.error.SignatureVerificationError:
        raise ValueError("Invalid Stripe webhook signature.")

    etype = event["type"]
    data  = event["data"]["object"]

    if etype in ("customer.subscription.created", "customer.subscription.updated"):
        _sync_subscription(data)
    elif etype == "customer.subscription.deleted":
        user = db.get_user_by_stripe_customer(data["customer"])
        if user:
            db.update_user(user["id"], plan="free", stripe_subscription_id=None)
    elif etype == "invoice.payment_succeeded":
        # Reset monthly usage count for basic plan users
        user = db.get_user_by_stripe_customer(data["customer"])
        if user and user["plan"] == "basic":
            db.reset_monthly_count(user["id"])


def _sync_subscription(sub: dict) -> None:
    user = db.get_user_by_stripe_customer(sub["customer"])
    if not user:
        return
    # Determine plan from price ID
    price_id = sub["items"]["data"][0]["price"]["id"] if sub["items"]["data"] else ""
    if price_id == STRIPE_PRO_PRICE_ID:
        plan = "pro"
    elif price_id == STRIPE_BASIC_PRICE_ID:
        plan = "basic"
    else:
        plan = "free"
    db.update_user(user["id"], plan=plan, stripe_subscription_id=sub["id"])


def can_summarize(user: dict) -> tuple[bool, str]:
    """Return (allowed, reason). Checks plan limits."""
    plan  = user.get("plan", "free")
    limit = PLAN_LIMITS.get(plan)
    if limit is None:
        return True, ""
    count = user.get("summary_count", 0)
    if count >= limit:
        if plan == "free":
            return False, f"Free plan limit reached ({limit} summaries). Upgrade to continue."
        return False, f"Monthly limit reached ({limit} summaries). Upgrade or wait for reset."
    return True, ""
