# Release policy

Release Planner's agent reads this file before every release. Treat the public contract below as what users depend on, and flag uncertainty instead of inventing compatibility guarantees.

## Breaking changes

A change is breaking when it changes any of these in a way that forces a calling repository to change its workflows, configuration, or deployment pins:

- `build.yml`'s inputs, the artifact it uploads, and the permissions and events it accepts
- `lambda-build.toml` keys and their meaning
- The artifact layout and file formats: ZIP names and bytes for the same input, `SHA256SUMS`, and `manifest.json`
- `lambda_build.py` commands, flags, and exit codes

A change to the ZIP bytes for the same build output is always breaking, because deployments pin digests and `verify` must keep reproducing older releases. Tests, CI, the Go fixture, and documentation are not part of the contract.

## Choosing a version

Versions follow [SemVer 2.0.0](https://semver.org/), with Git tags `vMAJOR.MINOR.PATCH`. The first release is v0.1.0.

Before 1.0.0:

- Minor: any breaking change or new feature
- Patch: compatible fixes, documentation, and internal changes

From 1.0.0:

- Major: any breaking change
- Minor: compatible new features
- Patch: compatible fixes, documentation, and internal changes

## Who reads the release notes

Maintainers of Fabrica repositories who pin `build.yml` by commit SHA and decide whether to move the pin.

## Order of the release notes

1. Breaking changes, with the workflow or config edits callers need
2. New features
3. Bug fixes
4. Improvements

## Always and never

- Always say whether a release changes the ZIP bytes for the same input.
- Never present CI, test, or fixture changes as user-facing features.
