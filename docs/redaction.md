# Redaction

Secrets and personal data are removed **on the developer's machine, before anything is
uploaded**. The code is in [`src/ccp/redaction/`](../src/ccp/redaction/).

```python
from ccp.redaction import redact_line

result = redact_line(raw_line, line_number)  # one transcript line
result.text  # the line to upload (None if it had to be withheld)
result.hits  # [Hit(rule_id, line, path)]  - where and which rule, never the value
result.withheld  # True if the line could not be processed
```

## Guarantees

| Guarantee | How |
| --- | --- |
| The removed value is never recorded | A `Hit` holds the rule id and the location (line number, JSON path), not the text |
| Clean lines are unchanged | A line is re-written only if something was redacted, so offsets and hashes of clean data stay exact |
| Output is still valid JSON | Lines are parsed, strings are cleaned, then the line is written again |
| Fail closed | If a line cannot be processed, it is withheld, never uploaded as-is |
| Idempotent | Redacting redacted text changes nothing |

## Rules

Placeholders look like `[REDACTED:<rule-id>]`. Rules are applied in this order.

**Secrets**

| Rule id | Finds |
| --- | --- |
| `private-key` | PEM private key block (also when the end marker is cut off) |
| `aws-access-key-id` | AWS access key id (`AKIA...`, `ASIA...`) |
| `github-token`, `github-fine-grained-token` | GitHub tokens |
| `slack-token` | Slack tokens |
| `stripe-key` | Stripe live and test keys |
| `anthropic-api-key`, `openai-style-key`, `google-api-key` | AI and Google API keys |
| `jwt` | JSON Web Tokens |
| `url-password` | Password inside `scheme://user:password@host` (user and host are kept) |
| `bearer-token` | `Authorization: Bearer ...` |
| `secret-assignment` | A value of 8+ characters assigned to a name such as `password`, `DB_PASSWORD`, `secret_key`, `api-key`, `auth_token` (the name is kept) |

**Personal data (India)**

| Rule id | Finds |
| --- | --- |
| `aadhaar` | 12 digits, optionally grouped 4-4-4, that pass the Verhoeff check |
| `pan` | 5 letters, 4 digits, 1 letter, with a valid holder-type letter |
| `indian-mobile` | 10-digit mobile starting 6-9, with or without `+91`, `91` or `0` |
| `email` | Email addresses (`git@host` SSH remotes are not treated as people) |

## Design choices to know about

- **Too much is better than too little.** A false positive loses a little context; a false
  negative leaks data. Cheap checks cut false positives where possible (the Aadhaar checksum).
- **Identifiers are protected.** A number inside a longer identifier is not redacted: a UUID's last
  12-digit group, or digits inside a hash. This was a real bug found on real transcripts
  (a UUID passing the Aadhaar check, which would have broken `uuid` / `parentUuid` links).
  A number set off by underscores or punctuation (`user_9876543210`) *is* redacted.
- **Developer email addresses are redacted in the transcript copy.** The developer's identity
  is recorded separately in the session record (`session_env`, task 1.8), not recovered from the
  transcript.

## Known limits

- Only **strings** are scanned. A number stored as a JSON number (`{"phone": 9876543210}`) is
  not inspected.
- Pattern-based rules cannot catch every secret: an unusual format or a secret without a
  telling name (`x = 8f3k...`) passes. Rules are the first line; keep secrets out of
  transcripts where possible.
- Names, addresses and other free-text personal data are not detected.
- Speed is about 1.5 MB/s on a laptop (a typical hook call handles well under 1 MB).

## Adding or changing a rule

1. Add a `_rule(...)` entry to `SECRET_RULES` or `PERSONAL_DATA_RULES` in
   [`rules.py`](../src/ccp/redaction/rules.py) with an id, a description and a pattern.
2. Add a fake value for the rule to the `fakes` fixture in
   [`tests/unit/redaction/conftest.py`](../tests/unit/redaction/conftest.py). Build it from
   pieces so that no complete fake credential appears in the source.
3. Add "must not match" examples to `test_rules.py`. A rule without near-miss tests is not done.
4. Run the rules on a real transcript and check the counts (see below) for false positives.

Check a real transcript without printing any content:

```powershell
python -c "import sys, pathlib; from collections import Counter; from ccp.redaction import redact_line; c = Counter(); [c.update(h.rule_id for h in redact_line(l, n).hits) for f in pathlib.Path(sys.argv[1]).rglob('*.jsonl') for n, l in enumerate(f.read_text(encoding='utf-8').splitlines(), 1)]; print(dict(c))" <folder>
```
