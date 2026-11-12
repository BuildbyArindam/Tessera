# Airwallex response fixtures

**These files are derived from the field lists in the official reference (see
`docs/airwallex-notes.md`), not recorded from the sandbox.** Values are invented. They exist so
CI can exercise the sandbox client without network.

To replace them with real, redacted recordings once you have sandbox credentials:

    AIRWALLEX_CLIENT_ID=... AIRWALLEX_API_KEY=... python scripts/record_airwallex_fixtures.py

The recorder redacts tokens and IDs. After recording, run `make test` and fix any field the
real response names differently, then update the notes.
