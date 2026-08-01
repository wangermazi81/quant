# -*- coding: utf-8 -*-
import errno
import os
import stat
from types import SimpleNamespace

import pytest


def test_secure_regular_uses_nofollow_fd_operations(monkeypatch):
    from deploy import harden_runtime_permissions as hardener

    calls = []
    monkeypatch.setattr(
        hardener.os,
        "open",
        lambda name, flags, *, dir_fd: (
            calls.append(("open", name, flags, dir_fd)) or 41
        ),
    )
    monkeypatch.setattr(
        hardener.os,
        "fstat",
        lambda fd: SimpleNamespace(
            st_mode=stat.S_IFREG | 0o666,
            st_nlink=1,
        ),
    )
    monkeypatch.setattr(
        hardener.os,
        "fchown",
        lambda fd, uid, gid: calls.append(
            ("fchown", fd, uid, gid)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        hardener.os,
        "fchmod",
        lambda fd, mode: calls.append(("fchmod", fd, mode)),
        raising=False,
    )
    monkeypatch.setattr(
        hardener.os,
        "close",
        lambda fd: calls.append(("close", fd)),
    )

    hardener._secure_regular_at(
        9,
        "users.db",
        uid=1001,
        gid=1001,
        mode=0o600,
    )

    open_call = calls[0]
    assert open_call[0:2] == ("open", "users.db")
    assert open_call[2] & hardener.O_NOFOLLOW
    assert open_call[3] == 9
    assert ("fchown", 41, 1001, 1001) in calls
    assert ("fchmod", 41, 0o600) in calls
    assert calls[-1] == ("close", 41)


def test_secure_regular_rejects_symlink_without_path_chown(
    monkeypatch,
):
    from deploy import harden_runtime_permissions as hardener

    changed = []

    def reject_symlink(_name, _flags, *, dir_fd):
        assert dir_fd == 9
        raise OSError(errno.ELOOP, "symbolic link")

    monkeypatch.setattr(hardener.os, "open", reject_symlink)
    monkeypatch.setattr(
        hardener.os,
        "fchown",
        lambda *args: changed.append(args),
        raising=False,
    )
    monkeypatch.setattr(
        hardener.os,
        "fchmod",
        lambda *args: changed.append(args),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="symbolic link"):
        hardener._secure_regular_at(
            9,
            "users.db",
            uid=1001,
            gid=1001,
            mode=0o600,
        )

    assert changed == []


def test_open_directory_rejects_symlink(monkeypatch):
    from deploy import harden_runtime_permissions as hardener

    monkeypatch.setattr(
        hardener.os,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.ELOOP, "symbolic link")
        ),
    )

    with pytest.raises(RuntimeError, match="symbolic link"):
        hardener._open_directory_at(9, "data")


def test_git_config_payload_disables_hooks_and_binds_remote():
    from deploy import harden_runtime_permissions as hardener

    payload = hardener._git_config_payload(
        "https://gitee.com/example/quant.git",
        "main",
    ).decode("utf-8")

    assert "hooksPath = /dev/null" in payload
    assert "url = https://gitee.com/example/quant.git" in payload
    assert "merge = refs/heads/main" in payload
    ssh_payload = hardener._git_config_payload(
        "ssh://git@gitee.com/example/quant.git",
        "main",
    ).decode("utf-8")
    assert "url = ssh://git@gitee.com/example/quant.git" in ssh_payload
    with pytest.raises(ValueError, match="HTTPS"):
        hardener._git_config_payload("file:///tmp/quant.git", "main")


def test_release_venv_symlink_allowlist_is_exact():
    from deploy import harden_runtime_permissions as hardener

    release = (
        ".venv-releases/"
        "release-" + ("a" * 40) + ".ABC123/lib64"
    )

    assert hardener._is_allowed_release_venv_symlink(
        release,
        "lib",
    )
    assert not hardener._is_allowed_release_venv_symlink(
        release,
        "../lib",
    )
    assert not hardener._is_allowed_release_venv_symlink(
        release.replace("/lib64", "/bin/python"),
        "python3",
    )
    assert not hardener._is_allowed_release_venv_symlink(
        release.replace("a" * 40, "not-a-commit"),
        "lib",
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX fd semantics")
def test_release_tree_hardening_accepts_only_standard_lib64_link(
    tmp_path,
):
    from deploy import harden_runtime_permissions as hardener

    releases = tmp_path / ".venv-releases"
    release = releases / (
        "release-" + ("a" * 40) + ".ABC123"
    )
    (release / "lib").mkdir(parents=True)
    (release / "lib64").symlink_to("lib", target_is_directory=True)
    root_fd = os.open(
        tmp_path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        hardener._secure_tree_at(
            root_fd,
            ".venv-releases",
            uid=os.getuid(),
            gid=os.getgid(),
            directory_mode=0o750,
            private=False,
            expected_dev=os.fstat(root_fd).st_dev,
            allowed_symlink=(
                hardener._is_allowed_release_venv_symlink
            ),
        )
    finally:
        os.close(root_fd)

    assert (release / "lib64").is_symlink()
    assert os.readlink(release / "lib64") == "lib"


@pytest.mark.skipif(os.name != "posix", reason="POSIX fd semantics")
def test_runtime_hardening_preserves_non_secret_cache_permissions(
    tmp_path,
):
    from deploy import harden_runtime_permissions as hardener

    (tmp_path / "data" / "daily").mkdir(parents=True)
    (tmp_path / "logs").mkdir()
    cache_file = tmp_path / "data" / "daily" / "000001.csv"
    cache_file.write_text("date,close\n", encoding="utf-8")
    cache_file.chmod(0o644)

    hardener.harden_application(
        tmp_path,
        runtime_uid=os.getuid(),
        runtime_gid=os.getgid(),
        runtime_only=True,
    )

    assert stat.S_IMODE(cache_file.stat().st_mode) == 0o644
