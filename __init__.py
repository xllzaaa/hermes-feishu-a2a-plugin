"""Directory plugin loader for Hermes."""

try:
    from .hermes_feishu_a2a import register
except ImportError:  # Allows pytest/imports when the repo root is not a package.
    from hermes_feishu_a2a import register

__all__ = ["register"]
