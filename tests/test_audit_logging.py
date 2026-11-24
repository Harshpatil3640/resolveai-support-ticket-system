"""Audit logging: emitted by default, and compatible with operator config.

In-process tests use the application's real handlers and read its real
output (capsys); none lowers a level with caplog. Every test that changes
the audit logger restores its exact previous state.

Operator configurations are applied with ``logging.config.dictConfig``,
which changes process-wide logging state, so they run in fresh
interpreters (as a server process applies them) and the actual stdout and
stderr of those processes are inspected. All databases are in memory;
resolveai.db is never used.
"""

import calendar
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.routes.admin import log_admin_action
from app.core import audit_logging
from app.core.audit_logging import (
    AUDIT_LOG_FORMAT,
    AUDIT_LOGGER_NAME,
    UtcIsoFormatter,
    configure_audit_logging,
    get_audit_logger,
    is_default_audit_handler,
)
from app.main import app


PROJECT_ROOT = Path(__file__).resolve().parents[1]
USERS_URL = "/api/admin/users"
PASSWORD = "Audit-Secret-Passphrase-1"
NEW_USER = {
    "name": "Audit Target",
    "email": "audit.target@example.test",
    "password": PASSWORD,
    "role": "agent",
}

UTC_TIMESTAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z"
DEFAULT_LINE = re.compile(UTC_TIMESTAMP + r" INFO resolveai\.audit: admin_action .*")
CREATE_LINE = re.compile(
    UTC_TIMESTAMP
    + r" INFO resolveai\.audit: admin_action action=user\.create "
    r"actor_id=\d+ target_user_id=\d+ role=agent is_active=True"
)
ROLE_CHANGE_LINE = re.compile(
    UTC_TIMESTAMP
    + r" INFO resolveai\.audit: admin_action action=user\.role_change "
    r"actor_id=\d+ target_user_id=\d+ role_before=agent role_after=customer "
    # Phase 7.5: the number of tickets the role change unassigned.
    r"unassigned_tickets=0"
)


def _is_from_pytest(handler: logging.Handler) -> bool:
    return type(handler).__module__.startswith("_pytest")


def _is_pytest_phase_handler(handler: logging.Handler) -> bool:
    # pytest attaches a fresh LogCaptureHandler to every NON-propagating
    # logger for each test phase, and removes it again itself.
    return _is_from_pytest(handler) and type(handler).__name__ == "LogCaptureHandler"


def _configured_handlers() -> list[logging.Handler]:
    """The audit logger's handlers, ignoring those pytest attached."""

    return [h for h in get_audit_logger().handlers if not _is_from_pytest(h)]


def _default_handlers() -> list[logging.Handler]:
    return [h for h in get_audit_logger().handlers if is_default_audit_handler(h)]


def _audit_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if "admin_action" in line]


def _log_sample_action() -> None:
    log_admin_action(
        "user.create",
        actor_id=1,
        target_user_id=2,
        role="agent",
        is_active=True,
    )


def _logger_state(name: str) -> tuple:
    logger = logging.getLogger(name)

    return (
        list(logger.handlers),
        logger.level,
        logger.propagate,
        logger.disabled,
        list(logger.filters),
    )


class _Collect(logging.Handler):
    """An operator's handler: records what it receives."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def saved_audit_logger():
    """The audit logger; its state is restored exactly after the test.

    pytest's per-phase capture handlers are left to pytest, which attaches
    and removes them itself; every other handler is restored.
    """

    logger = get_audit_logger()
    handlers = [h for h in logger.handlers if not _is_pytest_phase_handler(h)]
    level, propagate, disabled = logger.level, logger.propagate, logger.disabled

    yield logger

    for handler in list(logger.handlers):
        if not _is_pytest_phase_handler(handler):
            logger.removeHandler(handler)

    for handler in handlers:
        logger.addHandler(handler)

    logger.setLevel(level)
    logger.propagate = propagate
    logger.disabled = disabled


@pytest.fixture
def pristine_audit_logger(saved_audit_logger):
    """The audit logger as a fresh process has it before any configuration.

    No handlers at all (pytest's included) and propagating, so pytest does
    not attach its capture handlers to it during the test either.
    """

    logger = saved_audit_logger

    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    logger.setLevel(logging.NOTSET)
    logger.propagate = True
    logger.disabled = False

    return logger


# ---------------------------------------------------------------------------
# Default configuration (no operator configuration)
# ---------------------------------------------------------------------------


def test_app_configuration_installs_one_utc_stderr_handler() -> None:
    logger = get_audit_logger()

    assert logger.name == AUDIT_LOGGER_NAME
    assert logger.level == logging.INFO
    assert logger.isEnabledFor(logging.INFO)
    assert not logger.disabled
    assert logger.propagate is False

    [handler] = _configured_handlers()

    assert is_default_audit_handler(handler)
    assert isinstance(handler.formatter, UtcIsoFormatter)


def test_fresh_logger_gets_the_default_handler(pristine_audit_logger) -> None:
    logger = pristine_audit_logger

    configure_audit_logging()

    assert logger.level == logging.INFO
    assert logger.propagate is False
    # Exactly one handler in total: nothing from pytest on a fresh logger.
    assert [is_default_audit_handler(h) for h in logger.handlers] == [True]


def test_repeated_configuration_gives_one_handler_and_one_line(
    pristine_audit_logger,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for _repeat in range(5):
        configure_audit_logging()

    _log_sample_action()

    assert len(_default_handlers()) == 1
    assert len(pristine_audit_logger.handlers) == 1
    assert len(_audit_lines(capsys.readouterr().err)) == 1


def test_audit_configuration_leaves_root_and_other_loggers_alone() -> None:
    names = (
        "",
        "app",
        "app.main",
        "app.api.routes.admin",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "sqlalchemy",
    )
    before = [_logger_state(name) for name in names]

    configure_audit_logging()
    _log_sample_action()

    with TestClient(app):
        pass

    assert [_logger_state(name) for name in names] == before


def test_admin_action_reaches_stderr_without_changing_log_levels(
    client: TestClient,
    admin_user,
    admin_headers: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    capsys.readouterr()

    response = client.post(USERS_URL, json=NEW_USER, headers=admin_headers)

    assert response.status_code == 201

    captured = capsys.readouterr()
    [line] = _audit_lines(captured.err)

    assert CREATE_LINE.fullmatch(line)
    assert f"actor_id={admin_user.id} " in line
    assert _audit_lines(captured.out) == []
    assert get_audit_logger().level == logging.INFO

    token = admin_headers["Authorization"].removeprefix("Bearer ")

    for secret in (
        PASSWORD,
        token,
        "$argon2",
        NEW_USER["email"],
        NEW_USER["name"],
        "admin@example.test",
        "Test Admin",
    ):
        assert secret not in captured.err
        assert secret not in captured.out


def test_repeated_initialisation_never_duplicates_handlers_or_lines(
    client: TestClient,
    admin_headers: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    for _repeat in range(3):
        configure_audit_logging()

    for _restart in range(2):
        with TestClient(app):
            pass

    assert len(_default_handlers()) == 1
    assert len(_configured_handlers()) == 1

    capsys.readouterr()
    response = client.post(USERS_URL, json=NEW_USER, headers=admin_headers)

    assert response.status_code == 201
    assert len(_audit_lines(capsys.readouterr().err)) == 1


def test_start_up_re_enables_a_disabled_audit_logger(
    saved_audit_logger,
    client: TestClient,
    admin_headers: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """disable_existing_loggers in a later configuration must not silence it."""

    saved_audit_logger.disabled = True

    with TestClient(app):
        pass

    assert not saved_audit_logger.disabled

    capsys.readouterr()
    response = client.post(USERS_URL, json=NEW_USER, headers=admin_headers)

    assert response.status_code == 201
    assert len(_audit_lines(capsys.readouterr().err)) == 1


def test_audit_record_re_enables_a_logger_disabled_after_start_up(
    saved_audit_logger,
    capsys: pytest.CaptureFixture[str],
) -> None:
    saved_audit_logger.disabled = True

    _log_sample_action()

    assert not saved_audit_logger.disabled
    assert len(_audit_lines(capsys.readouterr().err)) == 1


# ---------------------------------------------------------------------------
# Operator configuration (in process; dictConfig variants run below)
# ---------------------------------------------------------------------------


def test_operator_handler_and_propagation_are_preserved(
    pristine_audit_logger,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    logger = pristine_audit_logger
    operator = _Collect()
    logger.addHandler(operator)
    logger.propagate = True

    for _repeat in range(3):
        configure_audit_logging()

    _log_sample_action()

    # The operator's handler alone, still propagating; no default handler.
    assert logger.handlers == [operator]
    assert logger.propagate is True
    assert logger.level == logging.INFO
    assert len(operator.records) == 1
    # The root logger received the record (caplog listens on the root).
    assert [r.name for r in caplog.records] == [AUDIT_LOGGER_NAME]
    assert _audit_lines(capsys.readouterr().err) == []


@pytest.mark.parametrize("propagate", [True, False])
def test_operator_propagation_choice_is_kept(
    pristine_audit_logger,
    propagate: bool,
) -> None:
    logger = pristine_audit_logger
    logger.addHandler(_Collect())
    logger.propagate = propagate

    configure_audit_logging()
    _log_sample_action()

    assert logger.propagate is propagate


@pytest.mark.parametrize("with_operator_handler", [True, False])
def test_operator_warning_level_is_kept(
    pristine_audit_logger,
    capsys: pytest.CaptureFixture[str],
    with_operator_handler: bool,
) -> None:
    logger = pristine_audit_logger
    operator = _Collect()

    if with_operator_handler:
        logger.addHandler(operator)

    logger.setLevel(logging.WARNING)

    configure_audit_logging()
    _log_sample_action()

    assert logger.level == logging.WARNING
    assert operator.records == []
    assert _audit_lines(capsys.readouterr().err) == []


def test_replaced_default_handler_is_not_reinstalled(
    pristine_audit_logger,
    capsys: pytest.CaptureFixture[str],
) -> None:
    logger = pristine_audit_logger
    configure_audit_logging()
    [default] = logger.handlers

    # What a later dictConfig naming the audit logger does.
    operator = _Collect()
    logger.removeHandler(default)
    logger.addHandler(operator)

    for _repeat in range(3):
        configure_audit_logging()
        _log_sample_action()

    assert logger.handlers == [operator]
    assert len(operator.records) == 3
    assert _audit_lines(capsys.readouterr().err) == []


# ---------------------------------------------------------------------------
# Router used without app.main
# ---------------------------------------------------------------------------


def test_log_admin_action_configures_an_unconfigured_logger(
    pristine_audit_logger,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """As in an application that never imported app.main."""

    _log_sample_action()

    [line] = _audit_lines(capsys.readouterr().err)

    assert CREATE_LINE.fullmatch(line)
    assert len(_default_handlers()) == 1


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_configuration_installs_one_default_handler(
    pristine_audit_logger,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    threads_count = 16
    original = audit_logging._default_handler

    def slow_default_handler() -> logging.Handler:
        # Widen the check-then-install window: without the lock, every
        # thread would find no handler and install its own.
        time.sleep(0.05)
        return original()

    monkeypatch.setattr(audit_logging, "_default_handler", slow_default_handler)
    barrier = threading.Barrier(threads_count)

    def configure_and_log(index: int) -> None:
        barrier.wait()

        if index % 2:
            configure_audit_logging()
        else:
            _log_sample_action()

    threads = [
        threading.Thread(target=configure_and_log, args=(index,))
        for index in range(threads_count)
    ]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(10)

    assert not any(thread.is_alive() for thread in threads)
    assert len(pristine_audit_logger.handlers) == 1
    assert len(_default_handlers()) == 1
    # One line per logging thread: none lost, none duplicated.
    assert len(_audit_lines(capsys.readouterr().err)) == threads_count // 2


# ---------------------------------------------------------------------------
# UTC ISO-8601 timestamps
# ---------------------------------------------------------------------------


def test_timestamps_are_utc_iso_8601() -> None:
    record = logging.LogRecord(
        AUDIT_LOGGER_NAME,
        logging.INFO,
        __file__,
        1,
        "admin_action action=user.create",
        None,
        None,
    )
    record.created = calendar.timegm((2026, 11, 22, 8, 30, 0)) + 0.045
    record.msecs = 45.0

    formatted = UtcIsoFormatter(AUDIT_LOG_FORMAT).format(record)

    # time.gmtime, never the server's local time zone.
    assert formatted == (
        "2026-11-22T08:30:00.045Z INFO resolveai.audit: "
        "admin_action action=user.create"
    )


def test_emitted_timestamp_is_the_current_utc_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _log_sample_action()

    [line] = _audit_lines(capsys.readouterr().err)
    stamp = datetime.strptime(line.split(" ", 1)[0], "%Y-%m-%dT%H:%M:%S.%fZ")

    assert abs(stamp.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)) < (
        timedelta(minutes=2)
    )


# ---------------------------------------------------------------------------
# Fresh processes: real logging configurations, real output
# ---------------------------------------------------------------------------


# Runs in a new interpreter. Arguments: mode ("app": import app.main;
# "router": mount only the admin router in a new FastAPI app), the logging
# configuration to apply, and when to apply it relative to the import.
# Starts the application three times, performs two administrative actions,
# then prints (to stdout) the audit logger's final state with the token
# and stored hash, so the test can check neither reached any log.
PROCESS_SCRIPT = r'''
import copy
import json
import logging
import logging.config
import sys

from uvicorn.config import LOGGING_CONFIG

mode, config_name, when = sys.argv[1:4]


def operator_config(audit_logger_config):
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "operator": {"format": "OPERATOR %(name)s %(message)s"},
            "root": {"format": "ROOT %(name)s %(message)s"},
        },
        "handlers": {
            "operator": {
                "class": "logging.StreamHandler",
                "formatter": "operator",
                "stream": "ext://sys.stderr",
            },
            "root": {
                "class": "logging.StreamHandler",
                "formatter": "root",
                "stream": "ext://sys.stdout",
            },
            "null": {"class": "logging.NullHandler"},
        },
        "root": {"level": "INFO", "handlers": ["root"]},
        "loggers": {"resolveai.audit": audit_logger_config},
    }


CONFIGS = {
    "uvicorn-default": lambda: copy.deepcopy(LOGGING_CONFIG),
    "disable-existing-loggers": lambda: {
        "version": 1,
        "disable_existing_loggers": True,
        "root": {"level": "WARNING"},
    },
    "operator-handler-propagating": lambda: operator_config(
        {"level": "INFO", "handlers": ["operator"], "propagate": True}
    ),
    "operator-warning-level": lambda: operator_config(
        {"level": "WARNING", "handlers": ["operator"], "propagate": True}
    ),
    "operator-root-only": lambda: operator_config(
        {"handlers": ["null"], "propagate": True}
    ),
}


def apply_configuration():
    if config_name != "none":
        logging.config.dictConfig(CONFIGS[config_name]())


if when == "before-import":
    apply_configuration()

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.security import create_access_token
from app.db.base import Base
from app.db.database import get_db
from app.models import User

if mode == "app":
    from app.main import app
else:
    from app.api.routes.admin import router

    app = FastAPI()
    app.include_router(router, prefix="/api/admin")

if when == "after-import":
    apply_configuration()

engine = create_engine(
    "sqlite://",
    poolclass=StaticPool,
    connect_args={"check_same_thread": False},
)
Base.metadata.create_all(engine)
db = sessionmaker(bind=engine, autoflush=False)()
admin = User(
    name="Audit Admin",
    email="audit.admin@example.test",
    password_hash="unused-hash",
    role="admin",
)
db.add(admin)
db.commit()
admin_id = admin.id


def override_get_db():
    yield db


app.dependency_overrides[get_db] = override_get_db
token = create_access_token(subject=str(admin_id), token_version=0)
headers = {"Authorization": "Bearer " + token}

for _restart in range(2):
    with TestClient(app):
        pass

with TestClient(app) as client:
    created = client.post(
        "/api/admin/users",
        json={
            "name": "Audit Target",
            "email": "audit.target@example.test",
            "password": "Audit-Secret-Passphrase-1",
            "role": "agent",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.status_code
    user_id = created.json()["id"]

    changed = client.patch(
        "/api/admin/users/%d/role" % user_id,
        json={"role": "customer"},
        headers=headers,
    )
    assert changed.status_code == 200, changed.status_code

audit = logging.getLogger("resolveai.audit")
result = {
    "app_main_imported": "app.main" in sys.modules,
    "level": logging.getLevelName(audit.level),
    "propagate": audit.propagate,
    "disabled": audit.disabled,
    "handlers": [
        "default" if getattr(h, "resolveai_audit_handler", False) else type(h).__name__
        for h in audit.handlers
    ],
    "token": token,
    "hash": db.get(User, user_id).password_hash,
}
sys.stdout.write("RESULT=" + json.dumps(result) + "\n")
'''


@dataclass(frozen=True)
class Expected:
    default_lines: int  # default handler, on stderr
    operator_lines: int  # operator's handler, on stderr
    root_lines: int  # operator's root handler, on stdout
    handlers: tuple[str, ...]
    level: str
    propagate: bool
    app_main_imported: bool = True


DEFAULT_OUTPUT = Expected(2, 0, 0, ("default",), "INFO", False)
OPERATOR_OUTPUT = Expected(0, 2, 2, ("StreamHandler",), "INFO", True)
OPERATOR_WARNING = Expected(0, 0, 0, ("StreamHandler",), "WARNING", True)

PROCESS_SCENARIOS = {
    "app, no configuration": (("app", "none", "before-import"), DEFAULT_OUTPUT),
    "app, uvicorn default before import": (
        ("app", "uvicorn-default", "before-import"),
        DEFAULT_OUTPUT,
    ),
    "app, uvicorn default after import": (
        ("app", "uvicorn-default", "after-import"),
        DEFAULT_OUTPUT,
    ),
    "app, disable_existing_loggers after import": (
        ("app", "disable-existing-loggers", "after-import"),
        DEFAULT_OUTPUT,
    ),
    "app, operator handler before import": (
        ("app", "operator-handler-propagating", "before-import"),
        OPERATOR_OUTPUT,
    ),
    "app, operator handler after import": (
        ("app", "operator-handler-propagating", "after-import"),
        OPERATOR_OUTPUT,
    ),
    "app, operator WARNING before import": (
        ("app", "operator-warning-level", "before-import"),
        OPERATOR_WARNING,
    ),
    "app, operator WARNING after import": (
        ("app", "operator-warning-level", "after-import"),
        OPERATOR_WARNING,
    ),
    "app, operator root pipeline only": (
        ("app", "operator-root-only", "before-import"),
        Expected(0, 0, 2, ("NullHandler",), "INFO", True),
    ),
    "router only, no configuration": (
        ("router", "none", "before-import"),
        Expected(2, 0, 0, ("default",), "INFO", False, app_main_imported=False),
    ),
    "router only, operator handler": (
        ("router", "operator-handler-propagating", "before-import"),
        Expected(0, 2, 2, ("StreamHandler",), "INFO", True, app_main_imported=False),
    ),
}


@pytest.mark.parametrize(
    ("arguments", "expected"),
    list(PROCESS_SCENARIOS.values()),
    ids=list(PROCESS_SCENARIOS),
)
def test_audit_output_of_a_fresh_process(
    arguments: tuple[str, str, str],
    expected: Expected,
) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", PROCESS_SCRIPT, *arguments],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert completed.returncode == 0, completed.stderr[-3000:]

    [result_line] = [
        line for line in completed.stdout.splitlines() if line.startswith("RESULT=")
    ]
    result = json.loads(result_line.removeprefix("RESULT="))
    stdout_logs = completed.stdout.replace(result_line, "")

    stderr_audit = _audit_lines(completed.stderr)
    default_lines = [line for line in stderr_audit if DEFAULT_LINE.fullmatch(line)]
    operator_lines = [
        line
        for line in stderr_audit
        if line.startswith("OPERATOR resolveai.audit admin_action ")
    ]
    root_lines = [
        line
        for line in _audit_lines(stdout_logs)
        if line.startswith("ROOT resolveai.audit admin_action ")
    ]

    # Exactly one line per action per destination, nothing else, although
    # the application was started three times.
    assert len(default_lines) == expected.default_lines, completed.stderr[-3000:]
    assert len(operator_lines) == expected.operator_lines, completed.stderr[-3000:]
    assert len(root_lines) == expected.root_lines, stdout_logs[-3000:]
    assert len(stderr_audit) == len(default_lines) + len(operator_lines)
    assert len(_audit_lines(stdout_logs)) == len(root_lines)

    if expected.default_lines:
        assert CREATE_LINE.fullmatch(default_lines[0])
        assert ROLE_CHANGE_LINE.fullmatch(default_lines[1])

    assert tuple(result["handlers"]) == expected.handlers
    assert result["level"] == expected.level
    assert result["propagate"] is expected.propagate
    assert result["disabled"] is False
    assert result["app_main_imported"] is expected.app_main_imported

    for secret in (
        PASSWORD,
        result["token"],
        result["hash"],
        "$argon2",
        "audit.target@example.test",
        "audit.admin@example.test",
        "Audit Target",
        "Audit Admin",
    ):
        assert secret not in completed.stderr
        assert secret not in stdout_logs


# ---------------------------------------------------------------------------
# Ticket audit records (Phase 7.6)
# ---------------------------------------------------------------------------

TICKET_LINE = re.compile(UTC_TIMESTAMP + r" INFO resolveai\.audit: ticket_action .*")


def test_log_admin_action_is_shared_and_its_format_is_unchanged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.api.routes import admin as admin_routes

    assert admin_routes.log_admin_action is audit_logging.log_admin_action

    _log_sample_action()

    [line] = _audit_lines(capsys.readouterr().err)

    assert CREATE_LINE.fullmatch(line)


def test_ticket_records_use_the_default_handler_and_utc_timestamps(
    capsys: pytest.CaptureFixture[str],
) -> None:
    audit_logging.log_ticket_status_change(
        actor_id=4, ticket_id=9, status_before="In Progress", status_after="Resolved"
    )

    [line] = [
        line for line in capsys.readouterr().err.splitlines() if "ticket_action" in line
    ]

    assert TICKET_LINE.fullmatch(line)
    assert line.endswith(
        "ticket_action action=ticket.status_change actor_id=4 ticket_id=9 "
        'status_before="In Progress" status_after=Resolved'
    )


def _capture_ticket_record(emit) -> logging.LogRecord:
    collected = _Collect()
    logger = get_audit_logger()
    logger.addHandler(collected)

    try:
        emit()
    finally:
        logger.removeHandler(collected)

    [record] = collected.records

    return record


@pytest.mark.parametrize(
    ("emit", "expected"),
    [
        (
            lambda: audit_logging.log_ticket_assignment(
                actor_id=1, ticket_id=2, event_type="assigned",
                previous_assignee_id=None, new_assignee_id=3,
            ),
            {"action": "ticket.assign", "actor_id": 1, "ticket_id": 2,
             "event_type": "assigned", "previous_assignee_id": None, "new_assignee_id": 3},
        ),
        (
            lambda: audit_logging.log_ticket_claim(actor_id=3, ticket_id=2, assignee_id=3),
            {"action": "ticket.claim", "actor_id": 3, "ticket_id": 2, "assignee_id": 3},
        ),
        (
            lambda: audit_logging.log_ticket_status_change(
                actor_id=3, ticket_id=2, status_before="Open", status_after="In Progress"
            ),
            {"action": "ticket.status_change", "actor_id": 3, "ticket_id": 2,
             "status_before": "Open", "status_after": "In Progress"},
        ),
        (
            lambda: audit_logging.log_ticket_deletion(actor_id=1, ticket_id=2),
            {"action": "ticket.delete", "actor_id": 1, "ticket_id": 2},
        ),
    ],
    ids=["assign", "claim", "status", "delete"],
)
def test_each_ticket_helper_logs_exactly_its_fields(emit, expected: dict) -> None:
    record = _capture_ticket_record(emit)

    assert record.ticket_action == expected
    assert record.levelno == logging.INFO
    assert not hasattr(record, "admin_action")


def test_ticket_helpers_never_echo_unexpected_values() -> None:
    """Values outside the allowlists are replaced, never written out."""

    hostile = "Ticket title\nforged=entry jane@example.test"

    status = _capture_ticket_record(
        lambda: audit_logging.log_ticket_status_change(
            actor_id=1, ticket_id=2, status_before=hostile, status_after="Closed"
        )
    )
    assignment = _capture_ticket_record(
        lambda: audit_logging.log_ticket_assignment(
            actor_id="1; DROP", ticket_id=True, event_type=hostile,
            previous_assignee_id="jane@example.test", new_assignee_id=2.5,
        )
    )

    assert status.ticket_action["status_before"] == "unrecognised"
    assert status.ticket_action["status_after"] == "unrecognised"
    assert assignment.ticket_action == {
        "action": "ticket.assign",
        "actor_id": None,
        "ticket_id": None,
        "event_type": "unrecognised",
        "previous_assignee_id": None,
        "new_assignee_id": None,
    }

    for record in (status, assignment):
        message = record.getMessage()

        assert "\n" not in message
        assert "forged" not in message
        assert "example.test" not in message
        assert "DROP" not in message
