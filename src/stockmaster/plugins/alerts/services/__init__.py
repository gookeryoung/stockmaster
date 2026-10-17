"""Alert layer: declarative rules, an idempotent evaluation engine, and channels."""

from stockmaster.plugins.alerts.services.channels import (
    BarkChannel,
    Channel,
    DatabaseChannel,
    Notification,
    WebhookChannel,
    build_channel,
    channels_for_workspace,
    get_channels,
    notify,
    send_test,
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
    "build_channel",
    "channels_for_workspace",
    "describe_rule",
    "get_channels",
    "normalize_params",
    "notify",
    "send_test",
]
