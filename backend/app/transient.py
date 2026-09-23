"""Telling a network blip apart from a real failure.

The unattended loops in main.py run for weeks. DNS goes away for a few
seconds when a VPN reconnects or a router reboots, every outbound call in
flight fails at once, and the next tick succeeds. Logging that as an
unexpected crash buries the failures that actually need Alex: forty lines of
traceback for something that has already fixed itself, several times an hour.

So a transient network failure is one calm line, and everything else keeps
its traceback. The test is the cause chain, not the exception type, because
each client wraps the same underlying error in its own class: httplib2 raises
ServerNotFoundError, urllib raises URLError, both from socket.gaierror. A
library we have never heard of that does the same thing is still recognised.
"""

import logging
import socket
import ssl

# Walking the chain is bounded: an exception chain can be circular.
MAX_CAUSE_DEPTH = 10

# The underlying conditions that mean "the network was not there just now".
# ConnectionError covers refused, reset and aborted; OSError with no more
# specific type is deliberately not included, because that is most of them.
TRANSIENT = (
    socket.gaierror,      # name resolution failed
    socket.herror,
    socket.timeout,
    ConnectionError,
    TimeoutError,
    ssl.SSLEOFError,
    ssl.SSLZeroReturnError,
)


def is_transient_network_error(exc: BaseException) -> bool:
    """Whether this failure is the network being briefly absent."""
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(MAX_CAUSE_DEPTH):
        if current is None or id(current) in seen:
            return False
        if isinstance(current, TRANSIENT):
            return True
        seen.add(id(current))
        # __cause__ is an explicit `raise ... from`; __context__ is what was
        # being handled when this was raised. Both carry the original.
        current = current.__cause__ or current.__context__
    return False


def log_loop_failure(logger: logging.Logger, exc: BaseException, what: str) -> None:
    """Report a failure in an unattended loop, at the volume it deserves.

    ``what`` reads as the thing that failed: "Reading Alex's command email".
    """
    if is_transient_network_error(exc):
        logger.warning("%s failed: the network was briefly unreachable; retrying", what)
    else:
        logger.exception("%s failed", what, exc_info=exc)


__all__ = ["is_transient_network_error", "log_loop_failure"]
