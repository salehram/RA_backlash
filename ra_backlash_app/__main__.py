"""Desktop entry point (the legacy CLI remains separate)."""
from .gui import main

if __name__ == "__main__":
    raise SystemExit(main())