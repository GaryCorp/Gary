"""Typed request models. All input from Gary is untrusted and is validated
here before any service or repository sees it."""

from gary.models.common import RequestModel, validate_request

__all__ = ["RequestModel", "validate_request"]
