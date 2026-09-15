"""CLI: python -m video_retrieval api [args...]"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: python -m video_retrieval api [args...]")
        print("  Start unified video retrieval API (CLIP + hybrid BGE search)")
        raise SystemExit(0 if argv and argv[0] in ("-h", "--help") else 1)

    command = argv[0].lower()
    rest = argv[1:]
    sys.argv = [f"video_retrieval.{command}"] + rest

    if command in ("api", "serve"):
        from video_retrieval.api import main as entry

        entry()
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        print("Only 'api' is supported. Video vectors are ingested via embedding_service.", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
