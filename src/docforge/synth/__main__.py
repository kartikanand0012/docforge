"""`python -m docforge.synth --out DIR`: write the synthetic pairs to DIR."""

import argparse
from collections.abc import Sequence
from pathlib import Path

from docforge.synth import DEFAULT_COUNT, DEFAULT_SEED
from docforge.synth.dataset import generate_dataset


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m docforge.synth",
        description="Generate synthetic pharma invoice / purchase-order pairs with ground truth.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="output directory; existing pair_NNN folders in it are replaced",
    )
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)

    labels = generate_dataset(args.out, count=args.count, seed=args.seed)
    print(f"Wrote {len(labels)} invoice/PO pairs to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
