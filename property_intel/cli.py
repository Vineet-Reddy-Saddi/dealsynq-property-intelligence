import argparse
import json

from .municipality import activate_property, precompute, status_from_config, validate_sample
from .stage_engine import stage_catalog


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="property-intel",
        description="Batch-first, evidence-backed property-intelligence pipeline",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    batch_p = sub.add_parser(
        "precompute", help="Run jurisdiction batch engines before property search")
    batch_p.add_argument("config")
    batch_p.add_argument("--force", action="store_true")

    activate_p = sub.add_parser(
        "activate-property",
        help="Resolve a precomputed property and run its on-demand engines",
    )
    activate_p.add_argument("config")
    activate_p.add_argument("--address")
    activate_p.add_argument("--parcel-id")
    activate_p.add_argument("--name")
    activate_p.add_argument("--output-root")
    activate_p.add_argument("--force", action="store_true")
    activate_p.add_argument("--skip-live", action="store_true")

    scope_p = sub.add_parser(
        "scope-status", help="Show batch-engine coverage for a collection scope")
    scope_p.add_argument("config")

    sample_p = sub.add_parser(
        "validate-sample",
        help="Activate a reproducible random property from a non-hardcoded scope",
    )
    sample_p.add_argument("config")
    sample_p.add_argument("--seed", type=int)
    sample_p.add_argument("--allow-missing-geometry", action="store_true")
    sample_p.add_argument("--force", action="store_true")
    sample_p.add_argument("--skip-live", action="store_true")

    sub.add_parser(
        "stage-status", help="Show the reusable batch, on-demand, and materialization contracts")

    args = parser.parse_args()
    if args.command == "precompute":
        result = precompute(args.config, force=args.force)
    elif args.command == "activate-property":
        if not (args.address or args.parcel_id or args.name):
            parser.error("activate-property requires --address, --parcel-id, or --name")
        result = activate_property(
            args.config, address=args.address, parcel_id=args.parcel_id,
            name=args.name, output_root=args.output_root,
            force=args.force, skip_live=args.skip_live,
        )
    elif args.command == "scope-status":
        result = status_from_config(args.config)
    elif args.command == "validate-sample":
        result = validate_sample(
            args.config, seed=args.seed,
            require_geometry=not args.allow_missing_geometry,
            force=args.force, skip_live=args.skip_live,
        )
    else:
        result = {"contract": stage_catalog()}
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
