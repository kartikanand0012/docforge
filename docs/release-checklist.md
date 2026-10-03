# Before the repository goes public

Nothing here is done yet. Each step needs the owner's go-ahead: rewriting history cannot be
undone for anyone who has already cloned the repository.

1. **Rotate the Gemini API key.** The key used during development has been on a developer
   machine for weeks. Make a new one, update `.env`, and delete the old one in Google AI
   Studio.
2. **Remove internal notes from history.** These hold research, plans and interview
   preparation that are not for clients:
   - `docs/research/`;
   - `docs/build-plan.md`.

   With [git-filter-repo](https://github.com/newren/git-filter-repo), on a fresh mirror clone:
   ```bash
   git clone --mirror git@github.com:kartikanand0012/docforge.git docforge-mirror
   cd docforge-mirror
   git filter-repo --invert-paths --path docs/research/ --path docs/build-plan.md
   git push --force --mirror
   ```
   Then remove any links to these files from the remaining docs.

   This rewrites every commit hash, so:
   - open pull requests must be merged or closed first;
   - each must then be rebuilt on the new history;
   - the commit hashes cited in `docs/tdd/` change and need updating.
3. **Scan the rewritten history for secrets** with gitleaks:
   ```bash
   docker run --rm -v "$PWD":/repo zricethezav/gitleaks:latest git /repo
   ```
4. **Read the public pages once more** (README, docs/progress.md, docs/validation/, the
   portfolio card). The wording rule: "designed to support", never "compliant",
   "validated", "certified" or "tamper-proof".
5. **Make the repository public.** Then point the portfolio card's links at the demo and the
   source.
