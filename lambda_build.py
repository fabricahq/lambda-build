"""Build a commit's Lambda functions in a pinned container and package reproducible release files.

`package` exports the commit from Git, runs the build command from its lambda-build.toml
twice inside the container image pinned there, and packages each build into ZIPs, SHA256SUMS,
and manifest.json. It fails unless both packagings are byte-identical. `verify` rebuilds a
published release from its manifest's source commit the same way and compares the files.

Each ZIP stores its entries uncompressed, sorted by path, with a fixed timestamp and fixed
permissions, so the same files always produce the same bytes. Requires Python 3.11+, Git,
and Docker.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import tomllib
from zipfile import ZIP_STORED, ZipFile, ZipInfo

FORMAT_VERSION = 2
# Lambda rejects direct uploads above these sizes. Stored entries make each ZIP slightly larger than its files.
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_UNPACKED_BYTES = 250 * 1024 * 1024
TIMESTAMP = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o100644
EXECUTABLE_MODE = 0o100755
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
RUNTIME = re.compile(r"[a-z][a-z0-9.]*")
IMAGE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
# Builds run on the Lambda's own platform, so any tests the build runs exercise real binaries.
PLATFORMS = {"arm64": "linux/arm64", "x86_64": "linux/amd64"}
CONFIG = "lambda-build.toml"
CONFIG_KEYS = {"image", "build", "runtime", "architecture", "assets", "assets_from", "executable"}
ASSET_KEYS = {"name", "directory", "files"}


class ReleaseError(Exception):
    """A release input or output that must not be published."""


def parse_config(text):
    """Validate lambda-build.toml text and return it as a dict with every optional key filled in.

    Unknown keys are errors, so a misspelled setting cannot silently change what is packaged.
    """
    try:
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ReleaseError(f"{CONFIG}: {error}") from None
    unknown = set(config) - CONFIG_KEYS
    if unknown:
        raise ReleaseError(f"{CONFIG}: unknown settings {sorted(unknown)}")
    for key in ("image", "build", "runtime", "architecture"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ReleaseError(f"{CONFIG}: set {key} to a nonempty string")
    if not IMAGE.fullmatch(config["image"]):
        raise ReleaseError(f"{CONFIG}: pin image by digest, such as golang:1.26.7@sha256:<64 hex digits>")
    if not RUNTIME.fullmatch(config["runtime"]):
        raise ReleaseError(f"{CONFIG}: unexpected Lambda runtime identifier {config['runtime']!r}")
    if config["architecture"] not in PLATFORMS:
        raise ReleaseError(f"{CONFIG}: architecture must be one of {', '.join(PLATFORMS)}")
    assets = config.setdefault("assets", [])
    if not isinstance(assets, list):
        raise ReleaseError(f"{CONFIG}: assets must be an array of tables")
    for asset in assets:
        if not isinstance(asset, dict) or set(asset) - ASSET_KEYS or not {"name", "directory"} <= set(asset):
            raise ReleaseError(f"{CONFIG}: each [[assets]] entry needs name and directory, and optionally files")
        if not all(isinstance(asset[k], str) for k in ("name", "directory")):
            raise ReleaseError(f"{CONFIG}: asset name and directory must be strings")
        files = asset.get("files")
        if files is not None and (not isinstance(files, list) or not all(isinstance(f, str) for f in files)):
            raise ReleaseError(f"{CONFIG}: asset files must be an array of strings")
    if "assets_from" in config and not isinstance(config["assets_from"], str):
        raise ReleaseError(f"{CONFIG}: assets_from must be a string")
    executable = config.setdefault("executable", [])
    if not isinstance(executable, list) or not all(isinstance(p, str) for p in executable):
        raise ReleaseError(f"{CONFIG}: executable must be an array of ZIP paths")
    if not assets and "assets_from" not in config:
        raise ReleaseError(f"{CONFIG}: set assets or assets_from")
    return config


def relative_path(root, value, setting):
    """Resolve a config path inside the source tree, rejecting absolute paths and `..` escapes."""
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ReleaseError(f"{CONFIG}: {setting} {value!r} must be a path inside the repository")
    return root / path


def contained(root, directory):
    """Return `directory` if no part of it below `root` is a symlink, so the build cannot point it at host files."""
    current = root
    for part in directory.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise ReleaseError(f"{current.relative_to(root)} is a symlink; build into real directories")
    if not directory.resolve().is_relative_to(root.resolve()):
        raise ReleaseError(f"{directory.relative_to(root)} resolves outside the source tree")
    return directory


def collect(directory):
    """Return the directory's regular files as sorted POSIX paths, rejecting anything else."""
    if directory.is_symlink() or not directory.is_dir():
        raise ReleaseError(f"{directory}: expected a directory of built files")
    files = []
    for path in directory.rglob("*"):
        relative = path.relative_to(directory).as_posix()
        if path.is_symlink():
            raise ReleaseError(f"{directory}: {relative} is a symlink; copy the file instead")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ReleaseError(f"{directory}: {relative} is not a regular file")
        files.append(relative)
    if not files:
        raise ReleaseError(f"{directory}: no files to package")
    return sorted(files)


def resolve_assets(root, config):
    """Map each asset name to (directory, expected files or None), reading assets_from as the build left it."""
    specs = [(a["name"], contained(root, relative_path(root, a["directory"], "asset directory")), a.get("files"))
             for a in config["assets"]]
    if "assets_from" in config:
        parent = contained(root, relative_path(root, config["assets_from"], "assets_from"))
        if parent.is_symlink() or not parent.is_dir():
            raise ReleaseError(f"{config['assets_from']}: the build did not create this directory of function directories")
        for path in sorted(parent.iterdir()):
            if path.is_symlink() or not path.is_dir():
                raise ReleaseError(f"{config['assets_from']}: {path.name} is not a function directory")
            specs.append((path.name, path, None))
    resolved = {name: (directory, expected) for name, directory, expected in specs}
    if len(resolved) != len(specs):
        raise ReleaseError("Each asset name may appear only once")
    # A case-insensitive filesystem would store both ZIPs as one file.
    if len({name.casefold() for name in resolved}) != len(resolved):
        raise ReleaseError(f"Asset names {sorted(resolved)} must not differ only in case")
    if not resolved:
        raise ReleaseError(f"{config['assets_from']}: no function directories to package")
    return resolved


def write_zip(entries, archive):
    """Write (path, bytes, executable) entries as stored entries with fixed metadata, in the given order.

    Creates `archive` exclusively, so an existing file is never overwritten.
    """
    with ZipFile(archive, "x", compression=ZIP_STORED, allowZip64=False) as bundle:
        for relative, data, executable in entries:
            entry = ZipInfo(relative, TIMESTAMP)
            entry.create_system = 3  # Unix, so the permission bits below are honored.
            entry.external_attr = (EXECUTABLE_MODE if executable else FILE_MODE) << 16
            bundle.writestr(entry, data)


def package(root, config, commit, output):
    """Package the built tree at `root` into ZIPs, SHA256SUMS, and manifest.json in a new or empty `output`.

    Nothing is left in `output` on failure. Returns the manifest.
    """
    if not COMMIT.fullmatch(commit):
        raise ReleaseError("Source commit must be a full, lowercase Git commit SHA")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ReleaseError(f"{output}: output must be a new or empty directory, so no stale asset is published")
    runtime, executables = config["runtime"], set(config["executable"])

    contents = {}
    for name, (directory, expected) in sorted(resolve_assets(root, config).items()):
        if not NAME.fullmatch(name):
            raise ReleaseError(f"Asset name {name!r} must start with a letter or digit and use only letters, digits, '.', '_', and '-'")
        files = collect(directory)
        if expected is not None and set(files) != set(expected):
            missing, unexpected = sorted(set(expected) - set(files)), sorted(set(files) - set(expected))
            raise ReleaseError(f"{name}: {directory.relative_to(root)} must contain exactly the expected files; "
                               f"missing {missing or 'none'}, unexpected {unexpected or 'none'}")
        if sum((directory / f).stat().st_size for f in files) > MAX_UNPACKED_BYTES:
            raise ReleaseError(f"{name}: files exceed Lambda's 250 MiB unzipped limit")
        if runtime.startswith("provided") and ("bootstrap" not in files or "bootstrap" not in executables):
            raise ReleaseError(f"{name}: an OS-only runtime needs an executable bootstrap at the ZIP root; "
                               "add bootstrap to executable")
        contents[name] = (directory, files)
    for path in sorted(executables):
        if not any(path in files for _, files in contents.values()):
            raise ReleaseError(f"executable {path} matches no packaged file")

    output.parent.mkdir(parents=True, exist_ok=True)
    # Stage beside the output and move it into place last, so a failure never leaves partial assets.
    with tempfile.TemporaryDirectory(prefix=f".{output.name}-", dir=output.parent) as temporary:
        staging = Path(temporary) / "release"
        staging.mkdir()
        entries = []
        for name, (directory, files) in contents.items():
            archive = staging / f"{name}.zip"
            write_zip([(f, (directory / f).read_bytes(), f in executables) for f in files], archive)
            size = archive.stat().st_size
            if size > MAX_ZIP_BYTES:
                raise ReleaseError(f"{name}: {archive.name} exceeds Lambda's 50 MiB direct-upload limit")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            entries.append({"name": name, "asset": archive.name, "sha256": digest, "size": size})
        (staging / "SHA256SUMS").write_text("".join(f"{e['sha256']}  {e['asset']}\n" for e in entries))
        manifest = {"format_version": FORMAT_VERSION, "source_commit": commit, "runtime": runtime,
                    "architecture": config["architecture"], "assets": entries}
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        if output.exists():
            output.rmdir()
        staging.rename(output)
    return manifest


# Git reads objects only: no system or global config, attributes, or replace refs, so the
# local clone's settings cannot change what a commit means.
GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ATTR_NOSYSTEM": "1",
           "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0"}


def git(repo, *args, **kwargs):
    """Run Git in `repo`, returning stdout, and turn its failure into a ReleaseError."""
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            env={**os.environ, **GIT_ENV}, **kwargs)
    if result.returncode != 0:
        message = result.stderr.decode(errors="replace").strip() if isinstance(result.stderr, bytes) else result.stderr.strip()
        raise ReleaseError(f"git {' '.join(args)}: {message}")
    return result.stdout


def resolve_commit(repo, revision):
    """Return the full SHA of a commit that exists in `repo`."""
    commit = git(repo, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}", text=True).strip()
    if not COMMIT.fullmatch(commit):
        raise ReleaseError(f"{revision}: not a commit in {repo}")
    return commit


def read_config(repo, commit, path):
    """Read and validate the release config as committed, ignoring uncommitted edits."""
    return parse_config(git(repo, "cat-file", "blob", f"{commit}:{path}", text=True))


def export(repo, commit, destination):
    """Write the committed tree to `destination`, without .git, untracked files, or credentials.

    Reads the tree and its blobs directly rather than through `git archive`, so no attribute
    (export-ignore, export-subst, eol) or local config such as core.autocrlf can change a file.
    Submodules become empty directories.
    """
    entries = []
    for record in git(repo, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if record:
            meta, path = record.split(b"\t", 1)
            mode, kind, oid = meta.decode().split(" ")
            relative = PurePosixPath(os.fsdecode(path))
            if relative.is_absolute() or ".." in relative.parts:
                raise ReleaseError(f"{commit}: unsafe path {relative} in the tree")
            entries.append((mode, kind, oid, destination.joinpath(*relative.parts)))
    root = destination.resolve()
    with subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, env={**os.environ, **GIT_ENV}) as reader:
        for mode, kind, oid, target in entries:
            target.parent.mkdir(parents=True, exist_ok=True)
            if kind == "commit":
                target.mkdir(exist_ok=True)
                continue
            reader.stdin.write(f"{oid}\n".encode())
            reader.stdin.flush()
            header = reader.stdout.readline().split()
            data = reader.stdout.read(int(header[2]))
            reader.stdout.read(1)  # the newline after each object
            if mode == "120000":
                link = os.fsdecode(data)
                if os.path.isabs(link) or not (target.parent / link).resolve().is_relative_to(root):
                    raise ReleaseError(f"{target.relative_to(destination)} links outside the tree")
                target.symlink_to(link)
            else:
                target.write_bytes(data)
                target.chmod(0o755 if mode == "100755" else 0o644)
        reader.stdin.close()
        if reader.wait() != 0:
            raise ReleaseError(f"git cat-file could not read the tree of {commit}")


def run_build(image, architecture, command, root):
    """Run the build command in the pinned image, with the source tree at /src, as the current user."""
    print(f"$ {command}", file=sys.stderr, flush=True)
    # A fixed mount path keeps absolute paths out of the output; HOME gives tools a writable cache.
    subprocess.run(["docker", "run", "--rm", "--platform", PLATFORMS[architecture],
                    "--user", f"{os.getuid()}:{os.getgid()}", "--env", "HOME=/tmp/home",
                    "--volume", f"{root.resolve()}:/src", "--workdir", "/src",
                    # Replace the image's entrypoint, which could otherwise ignore the command.
                    "--entrypoint", "/bin/sh", image, "-ec", command], check=True, stdout=sys.stderr)


def build(repo, commit, config, output):
    """Export the commit, build it in the container, and package the result into `output`."""
    with tempfile.TemporaryDirectory(prefix="lambda-build-src-") as temporary:
        root = Path(temporary)
        export(repo, commit, root)
        run_build(config["image"], config["architecture"], config["build"], root)
        return package(root, config, commit, output)


def differences(first, second):
    """List the release files that are missing from one directory or differ in bytes."""
    names = sorted({p.name for p in first.iterdir()} | {p.name for p in second.iterdir()})
    return [name for name in names
            if not ((first / name).is_file() and (second / name).is_file()
                    and (first / name).read_bytes() == (second / name).read_bytes())]


def package_commit(repo, revision, config_path, output):
    """Build the commit twice from clean exports and keep the first packaging only if both match."""
    commit = resolve_commit(repo, revision)
    config = read_config(repo, commit, config_path)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ReleaseError(f"{output}: output must be a new or empty directory, so no stale asset is published")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Stage both packagings beside the output, so nothing appears there unless both builds finish and match.
    with tempfile.TemporaryDirectory(prefix=f".{output.name}-", dir=output.parent) as temporary:
        first, repeat = Path(temporary) / "first", Path(temporary) / "repeat"
        manifest = build(repo, commit, config, first)
        build(repo, commit, config, repeat)
        changed = differences(first, repeat)
        if changed:
            raise ReleaseError(f"Two builds of {commit} produced different {', '.join(changed)}; "
                               "make the build reproducible")
        if output.exists():
            output.rmdir()
        first.rename(output)
    return manifest


def download_release(repository, tag, destination):
    """Download every file of a GitHub release with the gh CLI's credentials."""
    subprocess.run(["gh", "release", "download", tag, "--repo", repository, "--dir", str(destination)], check=True)


def verify(repo, release, config_path):
    """Rebuild the release in `release` from its source commit and return the files that differ.

    Every file counts: each ZIP, SHA256SUMS, and manifest.json must match the rebuild byte for
    byte, and a file missing from either side, or present in only one, is a difference.
    """
    try:
        manifest = json.loads((release / "manifest.json").read_text())
        commit = manifest["source_commit"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ReleaseError(f"{release}: unreadable manifest.json ({error})") from None
    if manifest.get("format_version") != FORMAT_VERSION or not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        raise ReleaseError(f"{release}: manifest.json is not a format {FORMAT_VERSION} Lambda release manifest")
    try:
        resolve_commit(repo, commit)
    except ReleaseError:
        raise ReleaseError(f"The release's source commit {commit} is not in {repo}; fetch it first") from None
    config = read_config(repo, commit, config_path)
    with tempfile.TemporaryDirectory(prefix="lambda-build-verify-") as temporary:
        rebuilt = Path(temporary) / "rebuilt"
        build(repo, commit, config, rebuilt)
        return differences(release, rebuilt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    package_parser = commands.add_parser("package", help="build a commit twice and write its release files")
    package_parser.add_argument("--commit", default="HEAD", help="commit to build (default: HEAD)")
    package_parser.add_argument("--output", type=Path, required=True, help="new or empty directory for the release files")
    verify_parser = commands.add_parser("verify", help="rebuild a published release and compare its files")
    source = verify_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--tag", help="GitHub release tag to download with gh; needs --repository")
    source.add_argument("--release-dir", type=Path, help="directory holding exactly the release's ZIPs, SHA256SUMS, and manifest.json")
    verify_parser.add_argument("--repository", help="OWNER/NAME of the GitHub repository that published --tag")
    for command in (package_parser, verify_parser):
        command.add_argument("--repo", type=Path, default=Path("."), help="Git repository holding the source (default: .)")
        command.add_argument("--config", default=CONFIG, help=f"config path inside the commit (default: {CONFIG})")
    args = parser.parse_args(argv)
    if args.command == "verify" and args.tag and not args.repository:
        parser.error("--tag needs --repository")
    try:
        if args.command == "package":
            manifest = package_commit(args.repo, args.commit, args.config, args.output)
            for entry in manifest["assets"]:
                print(f"{entry['sha256']}  {entry['asset']}")
            return 0
        with tempfile.TemporaryDirectory(prefix="lambda-build-download-") as temporary:
            release = args.release_dir
            if release is None:
                release = Path(temporary)
                download_release(args.repository, args.tag, release)
            changed = verify(args.repo, release, args.config)
            if changed:
                print(f"error: the rebuild differs from the release in {', '.join(changed)}", file=sys.stderr)
                return 1
            print((release / "SHA256SUMS").read_text(), end="")
            print("Rebuilt release matches.", file=sys.stderr)
            return 0
    except (ReleaseError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
