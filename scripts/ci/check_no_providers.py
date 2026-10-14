"""Routine jobs must run without either paid provider SDK installed."""

from importlib.metadata import PackageNotFoundError, version


def main() -> None:
    for name in ("anthropic", "google-genai"):
        try:
            version(name)
        except PackageNotFoundError:
            continue
        raise SystemExit(f"routine CI environment unexpectedly includes {name}")
    print("No paid provider SDKs installed")


if __name__ == "__main__":
    main()
