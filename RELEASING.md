# Release checklist

Create a release only from a commit whose **Pre-release safety gate** workflow is green.

Before tagging or publishing:

- [ ] The working tree is clean.
- [ ] Local `main` is synchronized with `origin/main`; record the exact release commit.
- [ ] The full unittest suite passes locally and the Python 3.10, 3.12, and 3.13 matrix is green.
- [ ] Python compile checks pass.
- [ ] `git diff --check` passes.
- [ ] The Gitleaks repository and history scan passes.
- [ ] Semgrep's local Python rules pass.
- [ ] CLI and example-policy smoke checks pass.
- [ ] The complete Pre-release safety gate is green for the exact commit to be released.
- [ ] No secrets, local credentials, generated artifacts, or unrelated files are included.

Only after every check passes should you create and push the intended tag and publish the release. Afterward, verify that the tag points to the recorded commit and that the public release and CI status are correct.

Equivalent local scanner commands, when the tools are installed:

```sh
gitleaks git . --redact=100 --no-banner
semgrep scan --config .semgrep.yml --error --metrics=off .
```

Gitleaks scans the checked-out Git history as well as repository content; a shallow clone cannot provide a complete history scan. `--redact=100` keeps detected secret values out of Gitleaks output. Semgrep uses the checked-in local ruleset, does not require cloud login, and disables metrics. The CI scanners run from pinned container versions with the repository mounted read-only and container networking disabled.
