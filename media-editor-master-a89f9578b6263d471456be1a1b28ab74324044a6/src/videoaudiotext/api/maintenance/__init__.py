"""Maintenance CLI: purge stale workspaces."""

from __future__ import annotations

import argparse
import json
import sys

from videoaudiotext.api.purge import PurgePolicy, purge_workspaces


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Purge stale Flow B workspaces under WORKSPACES_ROOT.",
    )
    parser.add_argument(
        "command",
        choices=["purge-workspaces"],
        help="Maintenance command",
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="List candidates without deleting (default: true)",
    )
    parser.add_argument(
        "--max-delete",
        type=int,
        default=100,
        help="Maximum workspaces to delete in one run",
    )
    parser.add_argument(
        "--ttl-empty-days",
        type=float,
        default=None,
        help="Override WORKSPACE_TTL_EMPTY_DAYS",
    )
    parser.add_argument(
        "--ttl-abandoned-days",
        type=float,
        default=None,
        help="Override WORKSPACE_TTL_ABANDONED_DAYS",
    )
    parser.add_argument(
        "--ttl-composed-days",
        type=float,
        default=None,
        help="Override WORKSPACE_TTL_COMPOSED_DAYS",
    )
    parser.add_argument(
        "--grace-hours",
        type=float,
        default=None,
        help="Override WORKSPACE_GRACE_HOURS",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Omit all_evaluated from JSON output",
    )
    args = parser.parse_args(argv)

    policy = PurgePolicy()
    if args.ttl_empty_days is not None:
        policy.ttl_empty_days = args.ttl_empty_days
    if args.ttl_abandoned_days is not None:
        policy.ttl_abandoned_days = args.ttl_abandoned_days
    if args.ttl_composed_days is not None:
        policy.ttl_composed_days = args.ttl_composed_days
    if args.grace_hours is not None:
        policy.grace_hours = args.grace_hours

    if args.command == "purge-workspaces":
        result = purge_workspaces(
            dry_run=bool(args.dry_run),
            max_delete=max(0, args.max_delete),
            policy=policy,
        )
        if args.compact:
            result = {k: v for k, v in result.items() if k != "all_evaluated"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
