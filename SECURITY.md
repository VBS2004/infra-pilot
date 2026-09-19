# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Use GitHub's private reporting:
**Security → Report a vulnerability** on <https://github.com/VBS2004/terra-pilot>. Include the version
or commit, what you found, and how to reproduce it. You should get a first reply within a week.

## Scope worth reporting

- Code that could write outside the target repo, or overwrite files without `--force`/`--apply`.
- Secrets (API keys, credentials) being logged, cached or sent to an unintended endpoint.
- Command injection through file names, prompts or config values passed to `terragrunt`, `terraform`,
  `docker`, `tflint`, `tfsec` or `checkov`.

## Things to know

- terra-pilot sends repository snippets to the LLM endpoint you configure. Use a local model
  (`LOCAL_MODEL`) or a private gateway for code you cannot share.
- Generated Terraform is **never applied** by this tool; `--apply` only writes files. Review them, and run
  the validation gate (`python -m terra_pilot.utils.validate`) before `terraform apply`.
