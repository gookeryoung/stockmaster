"""Alert layer: declarative rules, an idempotent evaluation engine, and channels."""

from app.alert.channels import (
    BarkChannel,
    Channel,
    DatabaseChannel,
    Notification,
    WebhookChannel,
    get_channels,
    notify,
)
from app.alert.engine import AlertEngine, TriggerCandidate
from app.alert.rules import RULE_TYPES, RuleContext, describe_rule, normalize_params

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
