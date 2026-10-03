# Intended use

## Use

DocForge reads business documents (in this release: pharmaceutical distribution invoices,
purchase orders and certificates of analysis), turns them into structured records with the
place on the page of every value, checks them against their source, rules and related
documents, and routes them either to acceptance or to a person for review. A reviewer can
correct values with a reason and approve or reject the record under an electronic signature.
An approved invoice produces a payment approval draft for the organisation's own systems.

## Users

- Integrators: the organisation's systems, with API keys, upload documents and read results.
- Reviewers: trained people who check flagged records, correct them and sign.
- Administrators: manage reviewers, keys and webhooks.

## Not intended for

- Release of a batch, or any GMP decision, without a qualified person's own review. A
  certificate check flags results outside their limits; it does not release anything.
- Documents other than the registered types. Others are refused, not guessed.
- Real personal or patient data in the public demo, which holds synthetic documents only.
- Use as the system of record for payments: the payment approval draft is input to the
  organisation's own approval and payment systems.

## Assumptions the validating organisation confirms

- Reviewers are identified, trained and authorised under the organisation's procedures before
  an account is made for them.
- The organisation decides which document types and which checks need a person, and sets the
  review thresholds from its own measured results.
- Model provider, hosting and backups are qualified as suppliers by the organisation.
