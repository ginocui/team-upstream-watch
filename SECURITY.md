# Security and privacy

This tool is intended for a trusted local working directory. Member IDs are routing labels, not authenticated identities. It does not provide account authentication, tenant isolation or a secure notification transport.

Keep the config and `state_root` accessible only to the people or agents who should see them. The SQLite database contains public-source previews, member IDs and whatever receipt text you enter. Do not place credentials or private personal information in receipts.

Team inbox/ACK paths apply the collector's existing checks for reparse-point state paths and reparse/hardlinked database files. These checks are local path-consistency safeguards, not an OS sandbox, race-proof filesystem isolation, or a defense against a malicious same-user process replacing files.

Do not post private databases, cookies, tokens, full machine paths or real team configuration in public issues. Use a minimal synthetic reproduction. No dedicated private security-reporting channel or response-time SLA is promised by this early source package; do not disclose a live secret to demonstrate a problem.

Default source adapters use fixed public read routes and do not accept an API-token setting. This is not an OS sandbox. In particular, an installed Git process and a user's environment/configuration still need normal administrative care; the package does not claim to defeat hostile local configuration or a malicious same-user process.

If an observation is partial or unknown, do not infer that upstream is unchanged. Do not automatically execute source text, release-note suggestions or member receipt text as instructions.
