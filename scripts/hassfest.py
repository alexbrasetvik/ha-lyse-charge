#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = ["sh"]
# ///
"""Run hassfest locally, the way home-assistant/actions/hassfest does in CI, without Docker.

Uses a shallow checkout of Home Assistant core at the version the dev environment
uses, and a separate venv with core's requirements (hassfest imports many core
components), both cached under .cache/. The script passes extra arguments on to hassfest.
"""

import os
from pathlib import Path
import re
import sys

import sh

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache"
INTEGRATION = ROOT / "custom_components" / "lyse_charge"


def _pinned(requirements: Path, package: str) -> str:
    """Return the `package==version` line pinned in a core requirements file."""
    match = re.search(rf"^{re.escape(package)}==\S+", requirements.read_text(), re.MULTILINE)
    if not match:
        sys.exit(f"{package} is not pinned in {requirements}")
    return match.group(0)


def main() -> None:
    """Check out core and its requirements if needed, then run hassfest on the integration."""
    uv = sh.uv.bake(_cwd=ROOT)
    version = str(
        uv("run", "--quiet", "python", "-c", "from homeassistant.const import __version__; print(__version__)")
    ).strip()
    core = CACHE / f"ha-core-{version}"
    venv = CACHE / f"hassfest-venv-{version}"
    python = venv / "bin" / "python"

    if not core.is_dir():
        sh.git(
            "-c", "advice.detachedHead=false", "clone", "--quiet", "--depth", "1", "--branch", version,
            "https://github.com/home-assistant/core.git", str(core),
        )  # fmt: skip
    if not python.exists():
        uv("venv", "--quiet", "--python", "3.14", str(venv))
        uv(
            "pip", "install", "--quiet",
            "-c", str(core / "homeassistant" / "package_constraints.txt"),
            "-r", str(core / "requirements.txt"),
            _pinned(core / "requirements_test.txt", "pipdeptree"),
            _pinned(core / "requirements_test.txt", "tqdm"),
            _pinned(core / "requirements_test_pre_commit.txt", "ruff"),
            _env={**os.environ, "VIRTUAL_ENV": str(venv)},
        )  # fmt: skip

    env = {**os.environ, "PATH": f"{venv / 'bin'}{os.pathsep}{os.environ['PATH']}"}
    # Hand over the process, as hassfest's output and exit code are the result.
    os.chdir(core)
    os.execve(
        python, [str(python), "-m", "script.hassfest", "--integration-path", str(INTEGRATION), *sys.argv[1:]], env
    )


if __name__ == "__main__":
    main()
