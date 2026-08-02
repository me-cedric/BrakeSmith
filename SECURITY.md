# Security policy

## Reporting

Please report vulnerabilities privately through GitHub Security Advisories for this repository. Do not open a public issue for an unpatched vulnerability.

## Scope

BrakeSmith runs local executables against user-selected media. Paths are passed to subprocesses as argument arrays, never through a shell. Source media must remain untouched on success, failure, and cancellation; violations of that guarantee are security-sensitive.

Only maintained releases receive fixes.
