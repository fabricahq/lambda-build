# Lambda build

Build AWS Lambda functions reproducibly in GitHub Actions, as release assets that deployments can pin by SHA-256.

This repository is built for Fabrica's own repositories and conventions. It is public so our public and private repositories can share it, and you're welcome to read or copy it, but we don't promise support, stable interfaces, or answers to issues and pull requests from outside Fabrica.

## What it does

- **Builds in a pinned container.** The build command runs twice, each time in a clean export of the commit, inside a container image pinned by digest, on the Lambda's own Linux platform. A deterministic build command then produces the same bytes on a CI runner or a laptop. Commands that depend on the time, unpinned downloads, or the host still differ, and the two-build comparison is meant to catch them.
- **Packages byte-reproducible ZIPs.** Entries are stored uncompressed, sorted by path, dated 1980-01-01, with 0644 permissions, or 0755 for files you mark executable. Packaging fails unless both builds produce identical ZIPs.
- **Enforces Lambda's direct-upload limits**: 50 MiB per ZIP and 250 MiB unzipped. It also requires an executable `bootstrap` for OS-only runtimes such as `provided.al2023`, and keeps every path a Linux filesystem accepts: names of at most 255 bytes and paths of at most 1024 bytes. A release holds at most 100 ZIPs of at most 65534 files each.
- **Builds with read-only access.** The reusable build workflow owns its job with `contents: read` and refuses `pull_request_target`.
- **Verifies a published release.** `verify` rebuilds a release from its source commit and compares every published file, the ZIPs included, with the rebuild.

lambda-build never tags or publishes. [Release Planner](https://github.com/fabricahq/release-planner) does: it calls your build on the release pull request, attests the files, and publishes them when you merge.

## Files it builds

- `NAME.zip` for each function.
- `SHA256SUMS`: one `<sha256>  NAME.zip` line per ZIP.
- `manifest.json`: `format_version` 3, the full `source_commit`, and each asset's `name`, `asset`, `sha256`, `size`, `runtime`, and `architecture`.

Asset names use only letters, digits, `.`, `_`, and `-`, so every file name is one Release Planner accepts.

## Configure a repository

Commit a `lambda-build.toml` at the repository root. Paths are relative to the root, and the build runs there as `/src` inside the container, with `/bin/sh -e`:

```toml
runtime = "provided.al2023"
architecture = "arm64" # or x86_64; the build runs on linux/arm64 or linux/amd64
image = "golang:1.26.7-bookworm@sha256:<digest>" # pin the image index digest
build = "make build"

# Package each subdirectory as its own ZIP, named after it...
assets_from = "build/functions"
executable = ["bootstrap"]

# ...or name each ZIP's directory, optionally listing the exact files it must contain.
# [[assets]]
# name = "api"
# directory = "build/api"
# files = ["index.mjs"]
```

Unknown settings are errors, and asset sources must not overlap: no asset directory, or `assets_from`, may be the same as or inside another. Those paths follow the same limits as paths inside a ZIP. The build starts from the committed tree only: no `.git`, untracked files, dependencies installed on the host, or credentials. It must install its own dependencies from a lockfile. It runs as your user ID with `HOME=/tmp/home`.

## Build release assets with Release Planner

Release Planner's [release assets](https://release-planner.fabricahq.com/customize/release-assets/) hook calls a workflow in your repository with string inputs `ref`, `tag`, and `version`, and publishes the `release-assets` artifact it uploads. Name your workflow in `.release-planner/config.yml`:

```yaml
release-assets:
  workflow: build-release.yml
```

Then make `.github/workflows/build-release.yml` one job that calls this repository's build workflow, pinned to a full commit SHA:

```yaml
name: Build release
on:
  workflow_call:
    inputs:
      ref:
        type: string
        required: true
      tag:
        type: string
        required: true
      version:
        type: string
        required: true
permissions:
  contents: read
jobs:
  build:
    uses: fabricahq/lambda-build/.github/workflows/build.yml@<full commit SHA> # vX.Y.Z
    with:
      ref: ${{ inputs.ref }}
      # config: lambda-build.toml   # the default
      # runs-on: ubuntu-24.04-arm   # the default; match the Lambda architecture
```

The build workflow checks out `ref` and runs each stage as its own step: build the functions twice in the pinned container, package each build into reproducible ZIPs and require them to match, check the release files, and upload the ZIPs, `SHA256SUMS`, and `manifest.json` as the `release-assets` artifact, with the files at its top level. It checks out `lambda_build.py` from its own commit, so the SHA you pin decides both. Release Planner's release workflow calls `build-release.yml` with read-only access, and this workflow keeps it that way.

Release Planner attests every release asset, and GitHub offers artifact attestations only to public repositories, or to private ones on GitHub Enterprise Cloud.

## Build on pull requests

Run the same build in pull request CI so an irreproducible or oversized build fails before a release:

```yaml
jobs:
  lambda-build:
    uses: fabricahq/lambda-build/.github/workflows/build.yml@<full commit SHA> # vX.Y.Z
    with:
      ref: ${{ github.sha }}
```

Download the `release-assets` artifact in a later job to smoke-test what you ship.

## Check release files before publishing

A repository that publishes the `release-assets` artifact with its own job, rather than through Release Planner, should check the downloaded files first:

```sh
python3 lambda_build.py check --release-dir release --commit "$SOURCE_COMMIT"
```

`check` builds nothing and runs no code from the repository. Its input is the artifact the build job uploaded in the same workflow run, so it guards against a build that wrote something other than lambda-build's output, not against an attacker who controls the runner. It reads `lambda-build.toml` as committed at that commit, requires each ZIP to be the canonical ZIP for its files and to follow the packaging rules, and recomputes `SHA256SUMS` and `manifest.json` from the ZIPs. The directory must hold exactly those files, byte for byte. It treats the files as hostile: it rejects a symlinked directory, opens files without following symlinks, reads nothing past the size limits, reads each ZIP's end record before parsing its central directory itself, and rejects compressed entries before reading them, so a crafted ZIP cannot expand in memory. It holds one ZIP at a time. It prints `SHA256SUMS` on success. Run the `lambda_build.py` from the lambda-build commit that built the files, with the repository checked out at that commit or any clone that contains it.

## Build or verify on your machine

You need Python 3.11 or later, Git, and, to build, package, or verify, Docker. From a clone of the application repository:

```sh
# Build HEAD twice and write its release files.
python3 lambda_build.py package --output build/release-assets

# Or the same in two steps, as the build workflow runs them: build twice, then package both
# builds. --builds needs the same --commit and --config that build used.
commit=$(git rev-parse HEAD)
python3 lambda_build.py build --commit "$commit" --config lambda-build.toml --output build/builds
python3 lambda_build.py package --builds build/builds --commit "$commit" --config lambda-build.toml \
  --output build/release-assets-from-builds

# Rebuild a published release from its source commit and compare.
python3 lambda_build.py verify --repository OWNER/NAME --tag v1.2.3
```

`verify` downloads every file of the release with `gh` and requires each ZIP, `SHA256SUMS`, and `manifest.json` to match the rebuild byte for byte, with no file missing or extra. It reads at most 1 MiB of `manifest.json`, and compares files by size and then in chunks, so an oversized release file cannot exhaust its memory. A release holds exactly lambda-build's output, because Release Planner publishes exactly the files in the `release-assets` artifact, so attach nothing else to it. The source commit must be in your clone. It reads `lambda-build.toml` as committed at that commit. Use the `lambda_build.py` from the commit of this repository that the release's workflow pinned. `--help` lists every option.

## Develop

Run the tests from the repository root. The container tests are skipped when Docker isn't running:

```sh
python3 -B -m unittest discover -s test -v
```

CI also builds [`examples/go-function`](examples/go-function) through the build workflow and requires the digest in its `expected-SHA256SUMS`, which was produced on macOS. When you change the fixture or its image, rebuild it and update that file.

## License

[MIT](LICENSE)
