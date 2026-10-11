# Datasets

Every outside dataset DocForge is tested on, and what may be done with it. Research and
reasons: `docs/research/09-real-world-test-corpus.md`; plan: `docs/plans/v1-real-world.md`.

## Rules

- **Licence from the primary source**, read on the date given, never from a mirror. Until it
  is read there, a dataset is not downloaded.
- **Committed**: only small subsets of permissive sets (MIT, Apache-2.0, CC BY 4.0,
  CDLA-Permissive-1.0), with attribution here. Everything else lives in `data/datasets/`
  (ignored by git), at most 20 GB in all.
- **Not used**: non-commercial or unclear licences (SROIE, FUNSD, XFUND, RealKIE, RVL-CDIP,
  DocVQA, Kleister).
- **Client samples** (shared under an NDA) go in `data/private/` only: never committed,
  recorded, uploaded anywhere or quoted in a report; only totals are reported. Deleted when
  the owner says.
- Each download is checked against the checksum below (sha256).

## Register

| Dataset | Source | Licence (as read at the source) | Read on | Used for | Where | Checksum |
| --- | --- | --- | --- | --- | --- | --- |
| katanaml invoices-donut-data-v1 | https://huggingface.co/datasets/katanaml-org/invoices-donut-data-v1 | MIT (to confirm) | - | invoice fields | `data/datasets/` | - |
| CORD | https://github.com/clovaai/cord | CC BY 4.0 (to confirm) | - | receipt fields | `data/datasets/` | - |
| AgamiAI Indian-Bank-Statements | https://huggingface.co/datasets/AgamiAI/Indian-Bank-Statements | Apache-2.0 (to confirm) | - | Indian tables | `data/datasets/` | - |
| AgamiAI Indian-Income-Tax-Returns | https://huggingface.co/datasets/AgamiAI/Indian-Income-Tax-Returns | Apache-2.0 (to confirm) | - | Hindi and English | `data/datasets/` | - |
| CUAD | https://www.atticusprojectai.org/cuad | CC BY 4.0 (to confirm) | - | contract questions | `data/datasets/` | - |
| DocLayNet | https://huggingface.co/datasets/docling-project/DocLayNet | CDLA-Permissive-1.0 (to confirm) | - | long layouts | `data/datasets/` | - |

Generated in the repo, owned by us: the Indian corpus (`src/docforge/synth/india/`) and the
adversarial files (`src/docforge/synth/adversarial.py`).
