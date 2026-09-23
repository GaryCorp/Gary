"""A network blip is one line; a real failure keeps its traceback."""

import logging
import socket

import pytest

from app.transient import is_transient_network_error, log_loop_failure


class ServerNotFoundError(Exception):
    """httplib2's shape: its own class, raised from socket.gaierror."""


def gmail_dns_failure() -> Exception:
    """Exactly what the Gmail client raises when DNS is briefly gone."""
    try:
        try:
            raise socket.gaierror(-3, "Temporary failure in name resolution")
        except socket.gaierror as cause:
            raise ServerNotFoundError(
                "Unable to find the server at gmail.googleapis.com"
            ) from cause
    except ServerNotFoundError as exc:
        return exc


def test_a_dns_failure_wrapped_by_an_unknown_client_is_transient():
    # The class is not one we import; the cause chain is what identifies it.
    assert is_transient_network_error(gmail_dns_failure())


@pytest.mark.parametrize(
    "exc",
    [
        socket.gaierror(-3, "Temporary failure in name resolution"),
        ConnectionResetError("reset by peer"),
        ConnectionRefusedError("refused"),
        TimeoutError("timed out"),
    ],
)
def test_the_network_being_absent_is_transient(exc):
    assert is_transient_network_error(exc)


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("a planner returned an invalid action"),
        KeyError("task_id"),
        OSError(28, "No space left on device"),
        PermissionError("token file is not readable"),
    ],
)
def test_a_real_failure_is_not_transient(exc):
    assert not is_transient_network_error(exc)


def test_a_circular_cause_chain_terminates():
    first = ValueError("first")
    second = ValueError("second")
    first.__cause__ = second
    second.__cause__ = first
    assert not is_transient_network_error(first)


def test_a_blip_is_logged_as_a_warning_without_a_traceback(caplog):
    logger = logging.getLogger("test.transient")
    with caplog.at_level(logging.WARNING, logger="test.transient"):
        log_loop_failure(logger, gmail_dns_failure(), "Reading Alex's command email")

    record = caplog.records[-1]
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert "Reading Alex's command email" in record.getMessage()


def test_a_real_failure_keeps_its_traceback(caplog):
    logger = logging.getLogger("test.transient")
    with caplog.at_level(logging.ERROR, logger="test.transient"):
        log_loop_failure(logger, ValueError("the planner returned nonsense"), "Planning cycle")

    record = caplog.records[-1]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
