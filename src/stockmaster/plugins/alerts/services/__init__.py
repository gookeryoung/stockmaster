"""Alert layer: declarative rules, an idempotent evaluation engine, and channels."""

from stockmaster.plugins.alerts.services.channels import (
    BarkChannel,
    Channel,
    DatabaseChannel,
    Notification,
    WebhookChannel,
    get_channels,
    notify,
)
from stockmaster.plugins.alerts.services.engine import AlertEngine, TriggerCandidate
from stockmaster.plugins.alerts.services.rules import RULE_TYPES, RuleContext, describe_rule, normalize_params

__all__ = [
    "RULE_TYPES",
    "AlertEngine",
    "BarkChannel",
    "Channel",
    "DatabaseChannel",
    "Notification",
    "RuleContext",
    "TriggerCandidate",
    "WebhookChannel",
    "describe_rule",
    "get_channels",
    "normalize_params",
    "notify",
]
