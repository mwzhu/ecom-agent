"""Webhook receivers for Shopify, Stripe, Gorgias, and fulfillment providers."""
from api.webhooks.routes import router

__all__ = ["router"]
