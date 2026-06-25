# MinCifry CA certificates (vendored)

These PEM files are the root and subordinate CA certificates of the
**Russian Trusted CA** (MinCifry / Минцифры / Russian Ministry of Digital
Development and Communications). They are required to verify TLS against
the new MAX Bot API domain `platform-api2.max.ru`, the migration deadline
of 2026-07-19 forces us to switch to. Without these, `python -m apply_pilot`
fails the first SSL handshake with:

```
[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate
```

## Files

| File | Subject | Issuer | Validity |
|---|---|---|---|
| `russian_trusted_root_ca_pem.crt` | Russian Trusted Root CA | (self-signed) | 2022-03-01 → 2032-02-27 |
| `russian_trusted_sub_ca_pem.crt`  | Russian Trusted Sub CA   | Russian Trusted Root CA | 2022-03-02 → 2027-03-06 |

## SHA-256 fingerprints (build-time echo)

The Dockerfile prints these on every image build (see
`./Dockerfile` runtime stage); if you ever see a different value, an
upstream CA rotation has happened and you should re-vendor these files.

```
Root CA: D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31
Sub  CA: BB:BD:E2:10:3E:79:0B:99:9E:C6:2B:D0:3C:F6:25:A5:A2:E7:C3:16:E1:0A:FE:6A:49:0E:ED:EA:D8:B3:FD:9B
```

## Why we vendor instead of `curl gu-st.ru` at build time

- The LANBilling guide points the download URLs at `gu-st.ru`, but the
  build host and `gu-st.ru` are not equally reachable from every CI
  runner or sandbox. A transient `CURLE_SSL_CONNECT_ERROR` (curl 35)
  has already cost a real rebuild retry in this repo.
- Vendoring makes the build deterministic: the bytes we trust are
  committed alongside the code that consumes them, so any rotation has
  to land as a reviewed change.
- The official Gosuslugi portal (`https://www.gosuslugi.ru/crt`) is the
  authoritative alternative, but it blocks non-browser user agents so it
  is not viable for automated downloads.

## When to update these files

- The **sub CA** expires **2027-03-06**. Replace before then.
- The **root CA** expires **2032-02-27**. Long-lived, but replace on
  any out-of-band rotation announced by MinCifry / LANBilling /
  Gosuslugi.
- If rotation happens, fetch the replacement from one of:
  - `https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt`
  - `https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt`
  or via a browser from `https://www.gosuslugi.ru/crt`, then
  `cp` the bytes over the file in this directory and commit. The
  Dockerfile does not need to change.

## Related

- `../Dockerfile` runtime stage block uses
  `COPY certs/*.crt /usr/local/share/ca-certificates/ && update-ca-certificates`.
- `../scripts/install-mincifry-ca.sh` is the equivalent for the host
  machine (auto-detects Debian/Ubuntu vs RHEL family, sudo-aware,
  idempotent) — useful for local dev tooling that does not run via
  Docker.
- GitHub issue #233; PR that vendored this: see `git log -- certs/`.

## Security note

These certificate files are PUBLIC infra CAs whose job is to be
widely known — there is no confidentiality risk in committing them.
A short SHA-256 fingerprint in this README + an `openssl ... -fingerprint`
echo on every build provides the rotation detection we need without
restating the full bytes inline.
