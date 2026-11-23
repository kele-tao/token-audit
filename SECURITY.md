# Security policy

## Supported versions

| version | supported |
| --- | --- |
| 1.x | yes |

## Reporting a vulnerability

Please do not open a public issue for a security problem. Email the maintainer
at `guoshiyin0609@gmail.com` with a description, a reproduction and the impact
you believe it has. You can expect an acknowledgement within three business
days.

## Threat model

`token-audit` is an offline command line tool. It reads files you point it at
and writes reports where you ask it to. It does not open sockets, does not read
environment variables for credentials, and does not execute anything it parses.

Things worth knowing when you run it on untrusted input:

- It parses JSON and JSONL with the standard library, so extremely deep nesting
  is bounded by Python's own recursion limit; malformed JSON is reported and the
  file is treated as text rather than crashing the scan.
- It decompresses the two merge tables that ship inside the package. It never
  decompresses user input.
- The HTML report escapes every string it prints. It does not load remote
  resources, fonts or scripts, so it renders identically offline.
- Paths are printed as given; it does not follow symlinks out of its way beyond
  what `pathlib` does by default.

If you find a case where a malicious prompt file can make the tool execute code,
exfiltrate data or crash with a traceback, that is a security bug and we want to
hear about it.
