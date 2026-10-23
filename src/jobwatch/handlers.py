"""AWS Lambda entry points."""

from __future__ import annotations

import logging

from . import dashboard, pipeline

logging.getLogger().setLevel(logging.INFO)
logging.getLogger("urllib3").setLevel(logging.WARNING)


def scrape_handler(event, context):
    """Hourly EventBridge schedule."""
    budget = context.get_remaining_time_in_millis() / 1000 - 30 if context else 840
    return pipeline.run(time_budget_s=budget)


def dashboard_handler(event, context):
    """Lambda Function URL."""
    return dashboard.handler(event, context)
