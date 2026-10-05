from __future__ import annotations

from pathlib import Path

import pytest

from prospectpilot.agents.mailer import Mailer, RealSendRefused, build_message, ensure_sandbox

from .helpers import make_settings


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "mailpit", "MAILPIT", "::1"])
def test_sandbox_hosts_allowed(host: str) -> None:
    ensure_sandbox(host, allow_real_send=False, i_understand=False)


@pytest.mark.parametrize(("allow", "flag"), [(False, False), (True, False), (False, True)])
def test_real_host_refused_unless_both_switches(allow: bool, flag: bool) -> None:
    with pytest.raises(RealSendRefused):
        ensure_sandbox("smtp.gmail.com", allow_real_send=allow, i_understand=flag)


def test_real_host_needs_env_and_flag() -> None:
    ensure_sandbox("smtp.example.com", allow_real_send=True, i_understand=True)


def test_mailer_refuses_at_construction(tmp_path: Path) -> None:
    s = make_settings(tmp_path, smtp_host="smtp.sendgrid.net", allow_real_send=True)
    with pytest.raises(RealSendRefused):
        Mailer(s)  # env alone is not enough


def test_mailer_rechecks_at_send_time(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    m = Mailer(s)
    s.smtp_host = "smtp.evil.example"  # config mutated after construction
    msg = build_message(sender="a@b.test", to="c@d.test", subject="s", body="b", headers={})
    with pytest.raises(RealSendRefused):
        m._send_sync(msg)


def test_message_has_id_and_custom_headers() -> None:
    msg = build_message(
        sender="a@b.test",
        to="c@d.test",
        subject="hi",
        body="body",
        headers={"X-ProspectPilot-Step": "1"},
    )
    assert str(msg["Message-ID"]).endswith("@prospectpilot.test>")
    assert msg["X-ProspectPilot-Step"] == "1"
