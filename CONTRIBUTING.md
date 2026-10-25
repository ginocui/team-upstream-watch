# Contributing

Small, testable fixes are welcome. This first release is a local Python CLI, not a hosted service or an agent platform.

1. Describe the concrete observation/review problem and the supported source involved.
2. Use synthetic fixtures; never attach credentials, a real team database, private configuration or internal member messages.
3. For a behavior change or bug fix, add a failing regression, then a minimal implementation and run the relevant offline tests.
4. Keep network acquisition separate from deterministic state/review logic. No upstream installation, automatic adoption, hidden daemon or chat posting in a routine fix.
5. Explain compatibility and local-data implications. Source observations, member review and actual adoption remain separate.

The original collector's shared global ACK is not the team API. Team receipts are keyed by member and event. Do not replace them with a single global boolean.

Opening a pull request does not authorize a production deployment or grant access to any team state. Contributions must be yours to submit and compatible with the repository's MIT license; preserve third-party notices where applicable.
