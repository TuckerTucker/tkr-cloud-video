# Conditional Rules

Rules that bind only in a named situation, keyed by `trigger:` frontmatter — kept out of the
always-loaded tier so every task does not pay for rules that rarely apply.

- **Authoring bar:** a `trigger:` naming the situation mechanically (file glob, command, event).
- **Written by:** human.
- **Retention:** bounded — replace-on-write.
