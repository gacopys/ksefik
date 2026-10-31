"""ksefik - przeglądanie i wystawianie faktur w KSeF z terminala."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ENVIRONMENTS, __version__, default_profile_path
from .profile import Profile, ProfileError, load_profile

__all__ = ["__version__", "main"]


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ksefik",
        description="Przeglądanie i wystawianie faktur w Krajowym Systemie e-Faktur.",
        epilog=f"Profil: {default_profile_path()} (inny wskażesz przez --profile albo KSEFIK_PROFILE).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--env", choices=list(ENVIRONMENTS), help="środowisko KSeF (nadpisuje profil)")
    parser.add_argument("--profile", type=Path, metavar="PLIK", help="plik profilu firmy (profile.toml)")
    args = parser.parse_args()

    path = (args.profile or default_profile_path()).expanduser()
    profile: Profile | None = None
    try:
        if path.exists():
            profile = load_profile(path, args.env)
    except ProfileError as exc:
        sys.exit(str(exc))

    from .ui.app import KsefApp

    KsefApp(profile, profile_path=path, env_override=args.env).run()
