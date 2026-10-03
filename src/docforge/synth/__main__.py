"""`python -m docforge.synth --out DIR`: write the synthetic pairs to DIR."""

import argparse
from collections.abc import Sequence
from pathlib import Path

from docforge.synth import DEFAULT_COUNT, DEFAULT_SEED
from docforge.synth.dataset import generate_dataset, generate_multipage
from docforge.synth.coa import generate_coas
from docforge.synth.scans import generate_scans
from docforge.synth.seeded import generate_seeded


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
    parser.add_argument(
        "--seeded",
        action="store_true",
        help="write the seeded-defect cases (case_NNN) instead of the clean pairs",
    )
    parser.add_argument(
        "--coa",
        action="store_true",
        help="write the certificates of analysis for the pairs' batches",
    )
    parser.add_argument(
        "--multipage",
        action="store_true",
        help="write the long pairs whose tables run over several pages",
    )
    parser.add_argument(
        "--scans-from",
        type=Path,
        default=None,
        help="write scanned variants of the pairs in this directory instead of new pairs",
    )
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)

    if args.scans_from is not None:
        written = generate_scans(args.scans_from, args.out)
        print(f"Wrote {written} scanned invoices to {args.out}")
        return 0
    if args.coa:
        coas = generate_coas(args.out, seed=args.seed)
        print(f"Wrote {len(coas)} certificates of analysis to {args.out}")
        return 0
    if args.multipage:
        labels = generate_multipage(args.out, seed=args.seed)
        print(f"Wrote {len(labels)} multi-page invoice/PO pairs to {args.out}")
        return 0
    if args.seeded:
        cases = generate_seeded(args.out, seed=args.seed)
        print(f"Wrote {len(cases)} seeded cases to {args.out}")
        return 0
    try:
        labels = generate_dataset(args.out, count=args.count, seed=args.seed)
    except ValueError as error:
        parser.error(str(error))
    print(f"Wrote {len(labels)} invoice/PO pairs to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
