import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import skill_ctl.self_update as self_update
from skill_ctl.self_update import SelfUpdateError, parse_release, parse_version


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({
            "tag_name": "v0.1.1",
            "assets": [{
                "name": "skill_ctl-0.1.1-py3-none-any.whl",
                "browser_download_url": "https://example.test/release.whl",
            }],
        }).encode()


def test_fetch_latest_release_uses_github_endpoint_and_user_agent(monkeypatch):
    requests = []
    monkeypatch.setattr(self_update, "urlopen", lambda request, timeout: requests.append((request, timeout)) or FakeResponse())

    release = self_update.fetch_latest_release()

    assert release.tag == "v0.1.1"
    assert requests[0][0].full_url == self_update.LATEST_RELEASE_URL
    assert requests[0][0].get_header("User-agent") == "skill-ctl"
    assert requests[0][1] == 10


def test_installer_command_uses_uv_tool_source_install():
    command = self_update.installer_command(
        Path("/home/user/.local/share/uv/tools/skill-ctl"), Path("/python"), "https://example.test/release.whl", "v0.1.1",
    )
    assert command == ["uv", "tool", "install", "git+https://github.com/Hadisd/skill-ctl@v0.1.1"]


def test_installer_command_uses_pipx_force_install():
    command = self_update.installer_command(
        Path("/home/user/.local/pipx/venvs/skill-ctl"), Path("/python"), "https://example.test/release.whl", "v0.1.1",
    )
    assert command == ["pipx", "install", "--force", "https://example.test/release.whl"]


def test_installer_command_falls_back_to_running_python():
    command = self_update.installer_command(
        Path("/venv"), Path("/venv/bin/python"), "https://example.test/release.whl", "v0.1.1",
    )
    assert command == ["/venv/bin/python", "-m", "pip", "install", "--upgrade", "https://example.test/release.whl"]


def test_parse_release_reads_matching_wheel_asset():
    release = parse_release({
        "tag_name": "v0.1.1",
        "assets": [{
            "name": "skill_ctl-0.1.1-py3-none-any.whl",
            "browser_download_url": "https://example.test/skill_ctl-0.1.1-py3-none-any.whl",
        }],
    })

    assert release.version == (0, 1, 1)
    assert release.tag == "v0.1.1"
    assert release.wheel_url.endswith("skill_ctl-0.1.1-py3-none-any.whl")


@pytest.mark.parametrize("value", ["v1.2", "v1.two.3", "v1.2.3-rc.1"])
def test_parse_version_rejects_non_final_release_versions(value):
    with pytest.raises(SelfUpdateError, match="version"):
        parse_version(value)


def test_parse_release_falls_back_to_git_url_when_missing_wheel():
    release = parse_release({"tag_name": "v0.1.1", "assets": []})
    assert release.version == (0, 1, 1)
    assert release.tag == "v0.1.1"
    assert release.wheel_url == "git+https://github.com/Hadisd/skill-ctl@v0.1.1"


def test_parse_release_rejects_tag_without_v_prefix():
    with pytest.raises(SelfUpdateError, match="tag"):
        parse_release({"tag_name": "0.1.1", "assets": []})


def test_self_update_check_reports_versions_without_prompt_or_subprocess(monkeypatch, capsys):
    monkeypatch.setattr(self_update, "fetch_latest_release", lambda: self_update.Release((0, 1, 1), "v0.1.1", "https://example.test/release.whl"))
    monkeypatch.setattr(self_update, "installed_version", lambda: (0, 1, 0))
    monkeypatch.setattr(self_update.Confirm, "ask", lambda *_args, **_kwargs: pytest.fail("must not prompt"))
    monkeypatch.setattr(self_update.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not run installer"))

    self_update.self_update(check=True)

    output = capsys.readouterr().out
    assert "v0.1.0" in output
    assert "v0.1.1" in output


def test_self_update_decline_does_not_run_installer(monkeypatch):
    monkeypatch.setattr(self_update, "fetch_latest_release", lambda: self_update.Release((0, 1, 1), "v0.1.1", "https://example.test/release.whl"))
    monkeypatch.setattr(self_update, "installed_version", lambda: (0, 1, 0))
    monkeypatch.setattr(self_update.Confirm, "ask", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(self_update.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not run installer"))

    self_update.self_update()


def test_self_update_yes_runs_detected_installer(monkeypatch):
    calls = []
    monkeypatch.setattr(self_update, "fetch_latest_release", lambda: self_update.Release((0, 1, 1), "v0.1.1", "https://example.test/release.whl"))
    monkeypatch.setattr(self_update, "installed_version", lambda: (0, 1, 0))
    monkeypatch.setattr(self_update, "installer_command", lambda *_args: ["uv", "tool", "install", "--reinstall", "https://example.test/release.whl"])
    monkeypatch.setattr(self_update.subprocess, "run", lambda command: calls.append(command) or SimpleNamespace(returncode=0))

    self_update.self_update(yes=True)

    assert calls == [["uv", "tool", "install", "--reinstall", "https://example.test/release.whl"]]


def test_self_update_exits_with_error_when_installer_fails(monkeypatch):
    monkeypatch.setattr(self_update, "fetch_latest_release", lambda: self_update.Release((0, 1, 1), "v0.1.1", "https://example.test/release.whl"))
    monkeypatch.setattr(self_update, "installed_version", lambda: (0, 1, 0))
    monkeypatch.setattr(self_update, "installer_command", lambda *_args: ["pip", "install"])
    monkeypatch.setattr(self_update.subprocess, "run", lambda _command: SimpleNamespace(returncode=1))

    with pytest.raises(SystemExit, match="1"):
        self_update.self_update(yes=True)


@pytest.fixture
def forbid_external_effects(monkeypatch):
    monkeypatch.setattr(self_update, "urlopen", lambda *_args, **_kwargs: pytest.fail("must not contact network"))
    monkeypatch.setattr(self_update.Confirm, "ask", lambda *_args, **_kwargs: pytest.fail("must not prompt"))
    monkeypatch.setattr(self_update.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not run installer"))


def test_cli_self_update_check_skips_config_initialization(monkeypatch, capsys, forbid_external_effects):
    import skill_ctl.cli as cli

    monkeypatch.setattr(cli.sys, "argv", ["skctl", "self-update", "--check"])
    monkeypatch.setattr(cli, "ensure_config", lambda: pytest.fail("must not create or migrate config"))
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(self_update, "fetch_latest_release", lambda: self_update.Release((0, 1, 1), "v0.1.1", "https://example.test/release.whl"))
    monkeypatch.setattr(self_update, "installed_version", lambda: (0, 1, 0))

    cli.main()

    output = capsys.readouterr().out
    assert "Current version: v0.1.0" in output
    assert "Latest version: v0.1.1" in output
    assert "Update available." in output


@pytest.mark.parametrize("error", [FileNotFoundError("uv not found"), PermissionError("uv not executable")])
def test_self_update_reports_installer_start_failure(monkeypatch, capsys, forbid_external_effects, error):
    monkeypatch.setattr(self_update, "fetch_latest_release", lambda: self_update.Release((0, 1, 1), "v0.1.1", "https://example.test/release.whl"))
    monkeypatch.setattr(self_update, "installed_version", lambda: (0, 1, 0))
    monkeypatch.setattr(self_update, "installer_command", lambda *_args: ["uv", "tool", "install", "--reinstall", "https://example.test/release.whl"])

    def fail_to_start(_command):
        raise error

    monkeypatch.setattr(self_update.subprocess, "run", fail_to_start)

    with pytest.raises(SystemExit) as caught:
        self_update.self_update(yes=True)

    assert caught.value.code == 1
    output = capsys.readouterr().err
    assert "Could not start installer" in output
    assert str(error) in output
    assert "Traceback" not in output


def test_self_update_reports_invalid_installed_version(monkeypatch, capsys, forbid_external_effects):
    monkeypatch.setattr(self_update, "fetch_latest_release", lambda: self_update.Release((0, 1, 1), "v0.1.1", "https://example.test/release.whl"))
    monkeypatch.setattr(self_update, "__version__", "invalid-version")
    monkeypatch.setattr(self_update, "installed_version", lambda: parse_version(self_update.__version__))

    with pytest.raises(SystemExit) as caught:
        self_update.self_update()

    assert caught.value.code == 1
    output = capsys.readouterr().err
    assert "Invalid release version: invalid-version" in output
    assert "Traceback" not in output


@pytest.mark.parametrize("payload", [
    [],
    None,
    {"tag_name": "v0.1.1", "assets": None},
    {"tag_name": "v0.1.1", "assets": [None]},
], ids=["list-payload", "null-payload", "null-assets", "null-asset"])
@pytest.mark.parametrize("entry_point", ["parser", "fetch", "handler"])
def test_invalid_release_shapes_fail_gracefully(monkeypatch, capsys, forbid_external_effects, payload, entry_point):
    monkeypatch.setattr(self_update, "urlopen", lambda *_args, **_kwargs: io.BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr(self_update, "installed_version", lambda: (0, 1, 0))

    if entry_point == "parser":
        with pytest.raises(SelfUpdateError, match="Invalid release"):
            parse_release(payload)
    elif entry_point == "fetch":
        with pytest.raises(SelfUpdateError, match="Could not check for updates: Invalid release"):
            self_update.fetch_latest_release()
    else:
        with pytest.raises(SystemExit) as caught:
            self_update.self_update()

        assert caught.value.code == 1
        output = capsys.readouterr().err
        assert "Could not check for updates: Invalid release" in output
        assert "Traceback" not in output


def test_windows_self_update_renames_and_cleans_up(tmp_path, monkeypatch):
    scripts_dir = tmp_path / "Scripts"
    scripts_dir.mkdir()
    fake_exe = scripts_dir / "skctl.exe"
    fake_exe.write_text("binary content", encoding="utf-8")

    monkeypatch.setattr(self_update, "is_windows", lambda: True)
    monkeypatch.setattr(self_update.sys, "prefix", str(tmp_path))
    monkeypatch.setattr(self_update.sys, "argv", [str(fake_exe)])
    monkeypatch.setattr(self_update.shutil, "which", lambda _name: str(fake_exe))

    # Mock subprocess run to simulate successful update creating new binary
    def mock_run(command):
        # Simulate new skctl.exe being written
        fake_exe.write_text("new binary content", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(self_update.subprocess, "run", mock_run)

    code = self_update.run_installer_with_windows_handling(["uv", "tool", "install"])
    assert code == 0
    assert fake_exe.exists()
    assert fake_exe.read_text(encoding="utf-8") == "new binary content"
    assert not (scripts_dir / "skctl.old.exe").exists()


def test_windows_self_update_restores_on_failure(tmp_path, monkeypatch):
    scripts_dir = tmp_path / "Scripts"
    scripts_dir.mkdir()
    fake_exe = scripts_dir / "skctl.exe"
    fake_exe.write_text("original binary", encoding="utf-8")

    monkeypatch.setattr(self_update, "is_windows", lambda: True)
    monkeypatch.setattr(self_update.sys, "prefix", str(tmp_path))
    monkeypatch.setattr(self_update.sys, "argv", [str(fake_exe)])
    monkeypatch.setattr(self_update.shutil, "which", lambda _name: str(fake_exe))

    # Mock installer failing
    monkeypatch.setattr(self_update.subprocess, "run", lambda _cmd: SimpleNamespace(returncode=1))

    code = self_update.run_installer_with_windows_handling(["pip", "install"])
    assert code == 1
    # Original executable restored
    assert fake_exe.exists()
    assert fake_exe.read_text(encoding="utf-8") == "original binary"


def test_cleanup_old_executables_removes_leftover_files(tmp_path, monkeypatch):
    scripts_dir = tmp_path / "Scripts"
    scripts_dir.mkdir()
    old_file = scripts_dir / "skctl.old.exe"
    old_file.write_text("old", encoding="utf-8")

    monkeypatch.setattr(self_update, "is_windows", lambda: True)
    monkeypatch.setattr(self_update.sys, "prefix", str(tmp_path))
    monkeypatch.setattr(self_update.sys, "argv", [str(scripts_dir / "skctl.exe")])

    assert old_file.exists()
    self_update.cleanup_old_executables()
    assert not old_file.exists()

