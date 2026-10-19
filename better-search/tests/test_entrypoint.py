import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "entrypoint.sh"


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run(
    tmp_path: Path,
    uid: str,
    *,
    owner: str = "0",
    data_path: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    chown_log = tmp_path / "chown.log"
    setpriv_log = tmp_path / "setpriv.log"
    _stub(
        bin_dir,
        "id",
        "#!/bin/sh\n"
        'if [ "$1" = "-u" ] && [ "${2:-}" = "app" ]; then printf "1000\\n"; exit 0; fi\n'
        'if [ "$1" = "-u" ]; then printf "%s\\n" "$STUB_UID"; exit 0; fi\n'
        "exit 1\n",
    )
    _stub(
        bin_dir,
        "stat",
        "#!/bin/sh\n"
        'printf "%s\\n" "$STUB_OWNER"\n',
    )
    _stub(
        bin_dir,
        "chown",
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$CHOWN_LOG"\n',
    )
    _stub(
        bin_dir,
        "setpriv",
        "#!/bin/sh\n"
        'while [ "$1" != "--" ]; do shift; done\n'
        "shift\n"
        'printf "%s\\n" "$*" >> "$SETPRIV_LOG"\n'
        'exec "$@"\n',
    )
    data = data_path if data_path is not None else str(tmp_path / "data")
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
            "DATA_PATH": data,
            "STUB_UID": uid,
            "STUB_OWNER": owner,
            "CHOWN_LOG": str(chown_log),
            "SETPRIV_LOG": str(setpriv_log),
        }
    )
    marker = tmp_path / "ran"
    completed = subprocess.run(
        ["sh", str(ENTRYPOINT), "sh", "-c", f"printf started > {marker}"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    chown = chown_log.read_text(encoding="utf-8") if chown_log.exists() else ""
    dropped = setpriv_log.read_text(encoding="utf-8") if setpriv_log.exists() else ""
    return completed, chown, dropped


def test_root_gives_the_data_directory_to_the_app_user_before_listening(tmp_path: Path) -> None:
    completed, chown, dropped = _run(tmp_path, "0")
    assert completed.returncode == 0, completed.stderr
    assert f"app:app {tmp_path / 'data'}" in chown
    assert "-R " not in chown
    assert "sh -c" in dropped
    assert (tmp_path / "data").is_dir()
    assert (tmp_path / "ran").read_text(encoding="utf-8") == "started"


def test_root_skips_chown_when_the_data_directory_is_already_the_app_user(tmp_path: Path) -> None:
    completed, chown, dropped = _run(tmp_path, "0", owner="1000")
    assert completed.returncode == 0, completed.stderr
    assert chown == ""
    assert "sh -c" in dropped


def test_non_root_starts_without_taking_ownership(tmp_path: Path) -> None:
    completed, chown, dropped = _run(tmp_path, "1000")
    assert completed.returncode == 0, completed.stderr
    assert chown == ""
    assert dropped == ""
    assert (tmp_path / "ran").read_text(encoding="utf-8") == "started"


def test_root_refuses_to_chown_the_filesystem_root(tmp_path: Path) -> None:
    completed, chown, _dropped = _run(tmp_path, "0", data_path="/")
    assert completed.returncode != 0
    assert chown == ""
    assert not (tmp_path / "ran").exists()
