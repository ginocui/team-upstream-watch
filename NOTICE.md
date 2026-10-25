# Provenance and scope of this source package

The original public-reference collector and source providers were built and integrated by **Hugo in the WE project**, owned by Gino. They were originally used as an internal shared-reference tracker. This independent release preserves that engineering origin; it is not a claim that Chase authored the original collector.

This release adds a standalone team configuration and CLI, shared source observation with member-specific review receipts, configuration validation, public examples and operations documentation. Chase coordinated this release work. Anonymous assistants used for extraction or review are not additional named WE members and do not inherit the original author's history.

The public extraction keeps the generic collector/provider behavior and makes these narrower changes:
- The optional internal managed-GitHub bridge is unsupported in this package, rather than importing an absent internal module.
- The internal two-PR derived-metadata evidence route is not a public provider API. Normal public GET observations remain supported.
- Tests do not include the production scheduler wrapper or read its installed configuration. Public test fixtures are synthetic.

No upstream project source code, model weights or Git executable is bundled. Public repository URLs and synthetic fixture fields describe things the tool observes; their license metadata does not grant a license to this package or to another upstream project. Standard-library modules are used from your Python installation.

Production member identities, follow-up messages, private acceptance records, managed credentials, databases and Runner-specific transport are not part of this distribution. The repository does not claim those integrations have been ported.

The Owner selected MIT for this package. This notice records project provenance and release changes; it is not a legal opinion or proof that all similar code on the internet has been searched.
