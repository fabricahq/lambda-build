"""Build a commit's Lambda functions in a pinned container and package reproducible release files.

`build` exports the commit from Git twice and runs the build command from its
lambda-build.toml in each export, inside the container image pinned there. `package --builds`
packages both builds into ZIPs, SHA256SUMS, and manifest.json and fails unless the two are
byte-identical; plain `package` does both steps at once. `verify` rebuilds a
published release from its manifest's source commit the same way and compares the files.
`check` confirms, without building, that a release directory is exactly what `package` writes
for its ZIPs and commit, so a publish job can trust it.

Each ZIP stores its entries uncompressed, sorted by path, with a fixed timestamp and fixed
permissions, so the same files always produce the same bytes. Requires Python 3.11+ and Git;
`build`, `package`, and `verify` also need Docker.
"""
import argparse
import hashlib
import json
import os
from io import BytesIO
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import subprocess
import sys
import tempfile
import tomllib
import zlib
from zipfile import ZIP_STORED, ZipFile, ZipInfo

# Format 3 records runtime and architecture on each asset; format 2 recorded them once per release.
FORMAT_VERSION = 3
# Lambda rejects direct uploads above these sizes. Stored entries make each ZIP slightly larger than its files.
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_UNPACKED_BYTES = 250 * 1024 * 1024
# The canonical writer uses no Zip64 extensions; Python's zipfile needs them from 65535 entries.
MAX_FILES = 65534
# A documented cap on ZIPs per release, so check's work and memory stay bounded with assets_from.
MAX_ASSETS = 100
# Far above any real manifest: 100 assets take about 30 KB.
MAX_MANIFEST_BYTES = 1024 * 1024
# The end-of-central-directory record that ends every ZIP lambda-build writes, with no comment,
# and the central directory and local file headers it points to.
END_RECORD = struct.Struct("<4s4H2LH")
CENTRAL_RECORD = struct.Struct("<4s6H3L5HLL")
LOCAL_HEADER = struct.Struct("<4s5H3L2H")
# Paths a Linux filesystem accepts: 255-byte names (NAME_MAX), and a cap on the whole path that
# also bounds its depth.
MAX_NAME_BYTES = 255
MAX_PATH_BYTES = 1024
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
# What `build` writes and `package --builds` reads: two build trees and a record of what was built.
BUILD_TREES = ("first", "second")
BUILD_RECORD = "build.json"
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
    # Static rules every command shares, so package and check accept exactly the same configs.
    names = [asset["name"] for asset in assets]
    if len(set(names)) != len(names):
        raise ReleaseError(f"{CONFIG}: each asset name may appear only once")
    for name in names:
        check_name(name)
    check_names(names, allow_empty=True)
    sources = [(f"asset {asset['name']}", inside_repository(asset["directory"], "asset directory").parts)
               for asset in assets]
    if "assets_from" in config:
        sources.append(("assets_from", inside_repository(config["assets_from"], "assets_from").parts))
    # One directory, or one inside another, would package the same files as two assets.
    for index, (label, parts) in enumerate(sources):
        for other_label, other_parts in sources[index + 1:]:
            shorter = min(len(parts), len(other_parts))
            if parts[:shorter] == other_parts[:shorter]:
                raise ReleaseError(f"{CONFIG}: asset sources overlap: {label} at {'/'.join(parts) or '.'} "
                                   f"and {other_label} at {'/'.join(other_parts) or '.'}")
    return config


def inside_repository(value, setting):
    """Return a config path as a relative path, rejecting absolute paths, `..` escapes, and any
    path a Linux filesystem could not hold, which no build could produce."""
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ReleaseError(f"{CONFIG}: {setting} {value!r} must be a path inside the repository")
    problem = path_problem("/".join(path.parts)) if path.parts else None
    if problem:
        raise ReleaseError(f"{CONFIG}: {setting} {value[:60]!r} {problem}")
    return path


def relative_path(root, value, setting):
    """Resolve a config path inside the source tree."""
    return root.joinpath(*inside_repository(value, setting).parts)


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


def check_name(name):
    """Fail unless `name` is a valid asset name, which also makes NAME.zip a plain file name that
    fits a filesystem's 255-byte limit."""
    if not NAME.fullmatch(name):
        raise ReleaseError(f"Asset name {name!r} must start with a letter or digit and use only letters, digits, '.', '_', and '-'")
    if len(f"{name}.zip".encode()) > MAX_NAME_BYTES:
        raise ReleaseError(f"Asset name {name[:40]}... makes a ZIP name longer than {MAX_NAME_BYTES} bytes")


def check_names(names, allow_empty=False):
    """Fail on an empty or oversized asset set, or on names that differ only in case, which a
    case-insensitive filesystem stores as one ZIP."""
    if not names and not allow_empty:
        raise ReleaseError("There are no assets to package")
    if len(names) > MAX_ASSETS:
        raise ReleaseError(f"There are more than {MAX_ASSETS} assets to package")
    if len({name.casefold() for name in names}) != len(set(names)):
        raise ReleaseError(f"Asset names {sorted(names)} must not differ only in case")


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
    check_names(resolved)
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


def path_problem(path):
    """Return why `path` cannot be a relative path on a Linux filesystem, or None if it can."""
    if "\0" in path:
        return "contains a NUL"
    if len(path.encode()) > MAX_PATH_BYTES:
        return f"is longer than {MAX_PATH_BYTES} bytes"
    parts = path.split("/")
    if path.startswith("/") or any(part in ("", ".", "..") for part in parts):
        return "is not a canonical relative path"
    if any(len(part.encode()) > MAX_NAME_BYTES for part in parts):
        return f"has a name longer than {MAX_NAME_BYTES} bytes"
    return None


def check_paths(name, files):
    """Fail unless `files` could be a real directory tree on a Linux filesystem: nonempty, unique,
    canonically spelled, within the name and path length limits, and with no path that is both a
    file and another path's directory. Uses memory proportional to the paths themselves."""
    if not files:
        raise ReleaseError(f"{name}: no files to package")
    if len(files) > MAX_FILES:
        raise ReleaseError(f"{name}: more than {MAX_FILES} files, the most a ZIP without Zip64 holds")
    for path in files:
        problem = path_problem(path)
        if problem:
            raise ReleaseError(f"{name}: path {path[:60]!r} {problem}")
    # Sorting with "/" as the lowest character orders paths by their components, so a duplicate is
    # next to its twin and a file is directly followed by any path inside it.
    ordered = sorted(files, key=lambda path: path.replace("/", "\0"))
    for current, following in zip(ordered, ordered[1:]):
        if following == current:
            raise ReleaseError(f"{name}: duplicate path {current}")
        if following.startswith(current + "/"):
            raise ReleaseError(f"{name}: {current} is both a file and a directory")


def check_asset(name, files, unpacked_size, expected, config):
    """Apply the per-asset rules shared by `package` and `check` to one asset's file list."""
    check_name(name)
    check_paths(name, files)
    if expected is not None and set(files) != set(expected):
        missing, unexpected = sorted(set(expected) - set(files)), sorted(set(files) - set(expected))
        raise ReleaseError(f"{name}: must contain exactly the expected files; "
                           f"missing {missing or 'none'}, unexpected {unexpected or 'none'}")
    if unpacked_size > MAX_UNPACKED_BYTES:
        raise ReleaseError(f"{name}: files exceed Lambda's 250 MiB unzipped limit")
    if config["runtime"].startswith("provided") and ("bootstrap" not in files or "bootstrap" not in config["executable"]):
        raise ReleaseError(f"{name}: an OS-only runtime needs an executable bootstrap at the ZIP root; "
                           "add bootstrap to executable")


def check_executables(config, found):
    """Fail when an `executable` path matches no file in any asset, which usually means a typo.

    `found` is the set of `executable` paths seen in any asset.
    """
    for path in sorted(set(config["executable"]) - set(found)):
        raise ReleaseError(f"executable {path} matches no packaged file")


def zip_bytes(entries):
    """Return the canonical ZIP bytes for (path, bytes, executable) entries, in the given order."""
    buffer = BytesIO()
    write_zip(entries, buffer)
    return buffer.getvalue()


def release_metadata(commit, config, archives):
    """Return the SHA256SUMS text and manifest for (name, sha256, size) triples sorted by name.

    `package` writes exactly these files and `check` requires them, byte for byte.
    """
    if not COMMIT.fullmatch(commit):
        raise ReleaseError("Source commit must be a full, lowercase Git commit SHA")
    entries = []
    for name, digest, size in archives:
        if size > MAX_ZIP_BYTES:
            raise ReleaseError(f"{name}.zip exceeds Lambda's 50 MiB direct-upload limit")
        entries.append({"name": name, "asset": f"{name}.zip", "sha256": digest,
                        "size": size, "runtime": config["runtime"], "architecture": config["architecture"]})
    sums = "".join(f"{e['sha256']}  {e['asset']}\n" for e in entries)
    # Runtime and architecture describe each asset, so the format can hold assets built for
    # different runtimes without changing.
    manifest = {"format_version": FORMAT_VERSION, "source_commit": commit, "assets": entries}
    return sums, manifest, json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def package(root, config, commit, output):
    """Package the built tree at `root` into ZIPs, SHA256SUMS, and manifest.json in a new or empty `output`.

    Nothing is left in `output` on failure. Returns the manifest.
    """
    if not COMMIT.fullmatch(commit):
        raise ReleaseError("Source commit must be a full, lowercase Git commit SHA")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ReleaseError(f"{output}: output must be a new or empty directory, so no stale asset is published")
    executables = set(config["executable"])
    contents = {}
    for name, (directory, expected) in sorted(resolve_assets(root, config).items()):
        files = collect(directory)
        check_asset(name, files, sum((directory / f).stat().st_size for f in files), expected, config)
        contents[name] = (directory, files)
    check_executables(config, {path for _, files in contents.values() for path in files if path in executables})

    output.parent.mkdir(parents=True, exist_ok=True)
    # Stage beside the output and move it into place last, so a failure never leaves partial assets.
    with tempfile.TemporaryDirectory(prefix=f".{output.name}-", dir=output.parent) as temporary:
        staging = Path(temporary) / "release"
        staging.mkdir()
        archives = []
        # One ZIP at a time, reading one file at a time, so memory does not grow with the release.
        for name, (directory, files) in contents.items():
            archive = staging / f"{name}.zip"
            write_zip(((f, (directory / f).read_bytes(), f in executables) for f in files), archive)
            archives.append((name, file_digest(archive), archive.stat().st_size))
        sums, manifest, manifest_text = release_metadata(commit, config, archives)
        (staging / "SHA256SUMS").write_text(sums)
        (staging / "manifest.json").write_text(manifest_text)
        if output.exists():
            output.rmdir()
        staging.rename(output)
    return manifest


def read_member(directory, name, limit, too_large):
    """Read a regular file inside the open directory `directory` without following symlinks,
    reading at most `limit` bytes and raising `too_large` beyond that."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    except OSError:
        raise ReleaseError(f"{name} is not a regular file") from None
    with os.fdopen(descriptor, "rb") as member:
        if not stat.S_ISREG(os.fstat(member.fileno()).st_mode):
            raise ReleaseError(f"{name} is not a regular file")
        data = member.read(limit + 1)
    if len(data) > limit:
        raise ReleaseError(too_large)
    return data


def check_end_record(name, data):
    """Fail unless `data` ends with the plain end record lambda-build writes, before the central
    directory is parsed: no comment or trailing bytes, no Zip64, one disk, at most MAX_FILES
    entries, and a central directory directly before the record and no larger than its entries need.
    Returns the entry count and the central directory's offset.
    """
    if len(data) < END_RECORD.size:
        raise ReleaseError(f"{name}.zip is too short to be a ZIP")
    end = len(data) - END_RECORD.size
    signature, disk, directory_disk, disk_entries, entries, size, offset, comment = END_RECORD.unpack_from(data, end)
    if signature != b"PK\x05\x06" or comment:
        raise ReleaseError(f"{name}.zip does not end with a plain end record (lambda-build writes no comment or trailing bytes)")
    # Zip64 saturates these fields; any Zip64 record or locator would also sit between the central
    # directory and this record, which the offset check below refuses.
    if 0xFFFF in (disk_entries, entries) or 0xFFFFFFFF in (size, offset):
        raise ReleaseError(f"{name}.zip uses Zip64, which lambda-build never writes")
    if disk or directory_disk or disk_entries != entries:
        raise ReleaseError(f"{name}.zip spans several disks")
    if entries > MAX_FILES:
        raise ReleaseError(f"{name}: more than {MAX_FILES} files, the most a ZIP without Zip64 holds")
    # A central directory record is 46 bytes plus its file name, and lambda-build adds no extras.
    if offset + size != end or size > entries * (CENTRAL_RECORD.size + MAX_PATH_BYTES):
        raise ReleaseError(f"{name}.zip has a central directory that does not match its end record")
    return entries, offset


def read_zip_entries(name, data, expected, config):
    """Return the (path, bytes) entries of an untrusted ZIP after the checks that need no file contents.

    Parses the central directory directly rather than with zipfile, which searches for Zip64 records
    and can misread a file name as one. Rejects anything but stored entries and applies the shared
    asset rules before reading entry data, so nothing is decompressed and memory stays within the
    archive's own size. The caller still requires the canonical bytes, which covers every field
    this does not check.
    """
    count, position = check_end_record(name, data)
    directory_end = len(data) - END_RECORD.size
    records = []
    try:
        for _ in range(count):
            if position + CENTRAL_RECORD.size > directory_end:
                raise ReleaseError(f"{name}.zip has a truncated central directory")
            (signature, _, _, flags, method, _, _, crc, compressed, size, name_length, extra_length,
             comment_length, _, _, _, local) = CENTRAL_RECORD.unpack_from(data, position)
            if signature != b"PK\x01\x02":
                raise ReleaseError(f"{name}.zip has a malformed central directory")
            raw = data[position + CENTRAL_RECORD.size:position + CENTRAL_RECORD.size + name_length]
            path = raw.decode("utf-8" if flags & 0x800 else "cp437")
            if method != ZIP_STORED or compressed != size:
                raise ReleaseError(f"{name}.zip: entry {path} is not stored")
            records.append((path, local, size, crc))
            position += CENTRAL_RECORD.size + name_length + extra_length + comment_length
        if position != directory_end:
            raise ReleaseError(f"{name}.zip has a central directory that does not match its end record")
        check_asset(name, [path for path, _, _, _ in records], sum(size for _, _, size, _ in records), expected, config)
        entries = []
        for path, local, size, crc in records:
            if local + LOCAL_HEADER.size > len(data):
                raise ReleaseError(f"{name}.zip: entry {path} has no local header")
            signature, *_, name_length, extra_length = LOCAL_HEADER.unpack_from(data, local)
            start = local + LOCAL_HEADER.size + name_length + extra_length
            if signature != b"PK\x03\x04" or start + size > len(data):
                raise ReleaseError(f"{name}.zip: entry {path} has a malformed local header")
            content = data[start:start + size]
            if zlib.crc32(content) != crc:
                raise ReleaseError(f"{name}.zip: entry {path} fails its CRC check")
            entries.append((path, content))
        return entries
    except (struct.error, UnicodeDecodeError) as error:
        raise ReleaseError(f"{name}.zip is not a readable ZIP ({type(error).__name__}: {error})") from None


def check_release(repo, release, revision, config_path):
    """Fail unless `release` holds exactly the files `package` writes for its ZIPs and the commit.

    Builds nothing and runs no code from the repository: it reads the config as committed,
    requires each ZIP to be canonical and to follow the packaging rules, and recomputes
    SHA256SUMS and manifest.json from the ZIPs, requiring them byte for byte. Every read is
    bounded, files are opened relative to the directory without following symlinks, and one ZIP
    is held in memory at a time. Returns the SHA256SUMS text.
    """
    commit = resolve_commit(repo, revision)
    config = read_config(repo, commit, config_path)
    if release.is_symlink():
        raise ReleaseError(f"{release} is a symlink; pass the release directory itself")
    try:
        directory = os.open(release, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise ReleaseError(f"{release} is not a directory ({error.strerror})") from None
    try:
        present = sorted(os.listdir(directory))
        for name in present:
            if not stat.S_ISREG(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode):
                raise ReleaseError(f"{name} is not a regular file")
        names = [name[:-len(".zip")] for name in present if name.endswith(".zip")]
        for name in names:
            check_name(name)
        check_names(names)
        configured = {asset["name"]: asset.get("files") for asset in config["assets"]}
        allowed = set(names) if "assets_from" in config else set(configured)
        expected_files = sorted({f"{name}.zip" for name in allowed | set(configured)} | {"SHA256SUMS", "manifest.json"})
        if present != expected_files:
            missing, extra = sorted(set(expected_files) - set(present)), sorted(set(present) - set(expected_files))
            raise ReleaseError(f"{release} must hold exactly lambda-build's files; "
                               f"missing {missing or 'none'}, unexpected {extra or 'none'}")
        archives, found = [], set()
        executables = set(config["executable"])
        for name in sorted(names):
            data = read_member(directory, f"{name}.zip", MAX_ZIP_BYTES,
                               f"{name}.zip exceeds Lambda's 50 MiB direct-upload limit")
            entries = read_zip_entries(name, data, configured.get(name), config)
            canonical = zip_bytes([(path, content, path in executables) for path, content in sorted(entries)])
            if canonical != data:
                raise ReleaseError(f"{name}.zip is not the canonical ZIP lambda-build writes for its files "
                                   "(stored, sorted, fixed timestamps and permissions)")
            archives.append((name, hashlib.sha256(data).hexdigest(), len(data)))
            found.update(executables.intersection(path for path, _ in entries))
            del data, entries, canonical
        check_executables(config, found)
        sums, _, manifest_text = release_metadata(commit, config, archives)
        for name, text in (("SHA256SUMS", sums), ("manifest.json", manifest_text)):
            expected = text.encode()
            mismatch = (f"{name} does not match what lambda-build writes for these ZIPs"
                        + (f" and {commit}" if name == "manifest.json" else ""))
            if read_member(directory, name, len(expected), mismatch) != expected:
                raise ReleaseError(mismatch)
    finally:
        os.close(directory)
    return sums


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
    Rejects paths that differ only in case before writing anything. Submodules become empty
    directories.
    """
    entries, seen = [], {}
    for record in git(repo, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if record:
            meta, path = record.split(b"\t", 1)
            mode, kind, oid = meta.decode().split(" ")
            relative = PurePosixPath(os.fsdecode(path))
            if relative.is_absolute() or ".." in relative.parts:
                raise ReleaseError(f"{commit}: unsafe path {relative} in the tree")
            entries.append((mode, kind, oid, destination.joinpath(*relative.parts)))
            # Paths, including their directories, that differ only in case are one path on a
            # case-insensitive filesystem; both builds would silently lose the same file.
            for depth in range(1, len(relative.parts) + 1):
                prefix = "/".join(relative.parts[:depth])
                other = seen.setdefault(prefix.casefold(), prefix)
                if other != prefix:
                    first, second = sorted((other, prefix))
                    raise ReleaseError(f"{commit}: {first} and {second} differ only in case, "
                                       "so one would overwrite the other on a case-insensitive filesystem")
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
                with open(target, "xb") as file:  # exclusive: never replace a file already written
                    file.write(data)
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


def build_tree(repo, commit, config, root):
    """Export the commit into the empty directory `root` and run its build there, in the container."""
    export(repo, commit, root)
    run_build(config["image"], config["architecture"], config["build"], root)


def build(repo, commit, config, output):
    """Export the commit, build it in the container, and package the result into `output`."""
    with tempfile.TemporaryDirectory(prefix="lambda-build-src-") as temporary:
        root = Path(temporary)
        build_tree(repo, commit, config, root)
        return package(root, config, commit, output)


def require_new_or_empty(path, what):
    """Fail unless `path` is missing or an empty directory, so nothing stale mixes with new output."""
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ReleaseError(f"{path}: {what} must be a new or empty directory")


def build_twice(repo, revision, config_path, builds):
    """Build the commit twice, from two clean exports, into `builds`/first and `builds`/second.

    Records the commit, the config path, and the config's blob in `builds`/build.json, so
    `package_builds` can confirm the builds came from the commit and config it is given. Leaves
    `builds` empty if either build fails.
    """
    commit = resolve_commit(repo, revision)
    config = read_config(repo, commit, config_path)
    config_blob = config_blob_of(repo, commit, config_path)
    require_new_or_empty(builds, "the builds directory")
    builds.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{builds.name}-", dir=builds.parent) as temporary:
        staging = Path(temporary) / "builds"
        for tree in BUILD_TREES:
            (staging / tree).mkdir(parents=True)
            build_tree(repo, commit, config, staging / tree)
        record = {"source_commit": commit, "config": config_path, "config_blob": config_blob}
        (staging / BUILD_RECORD).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        if builds.exists():
            builds.rmdir()
        staging.rename(builds)
    return commit


def config_blob_of(repo, commit, config_path):
    """Return the Git blob ID of the config as committed, which identifies its exact contents."""
    return git(repo, "rev-parse", "--verify", "--quiet", f"{commit}:{config_path}", text=True).strip()


def package_builds(repo, builds, output, revision, config_path):
    """Package the two builds that `build_twice` wrote, keeping the first only if both match.

    `revision` and `config_path` are the trusted inputs. `builds`/build.json must record the same
    commit, the same config path, and the blob of that config as committed, so a build record
    cannot relabel builds as another commit or select another config.
    """
    commit = resolve_commit(repo, revision)
    config = read_config(repo, commit, config_path)
    try:
        record = json.loads((builds / BUILD_RECORD).read_text())
    except (OSError, ValueError) as error:
        raise ReleaseError(f"{builds}: no readable {BUILD_RECORD} from lambda_build.py build ({error})") from None
    if not isinstance(record, dict):
        raise ReleaseError(f"{builds}/{BUILD_RECORD} is not a build record")
    if record.get("source_commit") != commit:
        raise ReleaseError(f"{BUILD_RECORD} records commit {record.get('source_commit')!r}, not {commit}")
    if record.get("config") != config_path:
        raise ReleaseError(f"{BUILD_RECORD} records config {record.get('config')}, not {config_path}")
    if record.get("config_blob") != config_blob_of(repo, commit, config_path):
        raise ReleaseError(f"{builds} was built with different {config_path} contents than {commit} holds")
    require_new_or_empty(output, "output")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Stage both packagings beside the output, so nothing appears there unless both match.
    with tempfile.TemporaryDirectory(prefix=f".{output.name}-", dir=output.parent) as temporary:
        first, second = (Path(temporary) / tree for tree in BUILD_TREES)
        manifest = package(builds / BUILD_TREES[0], config, commit, first)
        package(builds / BUILD_TREES[1], config, commit, second)
        changed = differences(first, second)
        if changed:
            raise ReleaseError(f"Two builds of {commit} produced different {', '.join(changed)}; "
                               "make the build reproducible")
        if output.exists():
            output.rmdir()
        first.rename(output)
    return manifest


def file_digest(path):
    """Return a file's SHA-256, reading it in chunks."""
    with open(path, "rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def same_file(first, second):
    """Compare two regular files by size, then in chunks, so neither is read whole."""
    if first.is_symlink() or second.is_symlink() or not first.is_file() or not second.is_file():
        return False
    if first.stat().st_size != second.stat().st_size:
        return False
    with open(first, "rb") as one, open(second, "rb") as other:
        while True:
            chunk = one.read(1024 * 1024)
            if chunk != other.read(1024 * 1024):
                return False
            if not chunk:
                return True


def differences(first, second):
    """List the release files that are missing from one directory or differ in bytes, comparing
    sizes first and contents in chunks, so an oversized file is never read whole."""
    names = sorted({p.name for p in first.iterdir()} | {p.name for p in second.iterdir()})
    return [name for name in names if not same_file(first / name, second / name)]


def package_commit(repo, revision, config_path, output):
    """Build the commit twice from clean exports and keep the first packaging only if both match:
    `build_twice` and `package_builds` in one step, with the builds in a temporary directory."""
    require_new_or_empty(output, "output")
    with tempfile.TemporaryDirectory(prefix="lambda-build-builds-") as temporary:
        builds = Path(temporary) / "builds"
        commit = build_twice(repo, revision, config_path, builds)
        return package_builds(repo, builds, output, commit, config_path)


def download_release(repository, tag, destination):
    """Download every file of a GitHub release with the gh CLI's credentials."""
    subprocess.run(["gh", "release", "download", tag, "--repo", repository, "--dir", str(destination)], check=True)


def verify(repo, release, config_path):
    """Rebuild the release in `release` from its source commit and return the files that differ.

    Every file counts: each ZIP, SHA256SUMS, and manifest.json must match the rebuild byte for
    byte, and a file missing from either side, or present in only one, is a difference.
    """
    path = release / "manifest.json"
    try:
        if path.is_symlink() or not path.is_file():
            raise ReleaseError(f"{release}: manifest.json is not a regular file")
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ReleaseError(f"{release}: manifest.json is larger than {MAX_MANIFEST_BYTES} bytes, "
                               "more than any lambda-build manifest")
        with open(path, "rb") as file:
            text = file.read(MAX_MANIFEST_BYTES + 1)[:MAX_MANIFEST_BYTES]
    except OSError as error:
        raise ReleaseError(f"{release}: unreadable manifest.json ({error.strerror})") from None
    try:
        manifest = json.loads(text)
    except (ValueError, RecursionError) as error:  # deep nesting overflows json's recursive decoder
        raise ReleaseError(f"{release}: manifest.json is not a lambda-build manifest ({type(error).__name__})") from None
    commit = manifest.get("source_commit") if isinstance(manifest, dict) else None
    if (not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION
            or not isinstance(commit, str) or not COMMIT.fullmatch(commit)):
        raise ReleaseError(f"{release}: manifest.json is not a lambda-build manifest of format {FORMAT_VERSION}")
    try:
        resolve_commit(repo, commit)
    except ReleaseError:
        raise ReleaseError(f"The release's source commit {commit} is not in {repo}; fetch it first") from None
    config = read_config(repo, commit, config_path)
    with tempfile.TemporaryDirectory(prefix="lambda-build-verify-") as temporary:
        rebuilt = Path(temporary) / "rebuilt"
        build(repo, commit, config, rebuilt)
        return differences(release, rebuilt)


def nonempty(value):
    """argparse type that refuses an empty value instead of letting it fall back to a default."""
    if not value:
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build", help="build a commit twice in its pinned container, without packaging")
    build_parser.add_argument("--commit", type=nonempty, default="HEAD", help="commit to build (default: HEAD)")
    build_parser.add_argument("--output", type=Path, required=True,
                              help="new or empty directory for the two build trees and build.json")
    package_parser = commands.add_parser("package", help="package two builds into release files, building them first unless --builds")
    package_parser.add_argument("--builds", type=Path,
                                help="directory that lambda_build.py build wrote; package it without building. "
                                     "Needs the same --commit and --config that build was given")
    package_parser.add_argument("--commit", type=nonempty, help="commit to build (default: HEAD); required with --builds")
    package_parser.add_argument("--output", type=Path, required=True, help="new or empty directory for the release files")
    verify_parser = commands.add_parser("verify", help="rebuild a published release and compare its files")
    source = verify_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--tag", help="GitHub release tag to download with gh; needs --repository")
    source.add_argument("--release-dir", type=Path, help="directory holding exactly the release's ZIPs, SHA256SUMS, and manifest.json")
    verify_parser.add_argument("--repository", help="OWNER/NAME of the GitHub repository that published --tag")
    check_parser = commands.add_parser("check", help="confirm a release directory is exactly what lambda-build writes, without building")
    check_parser.add_argument("--release-dir", type=Path, required=True, help="directory holding the release files")
    check_parser.add_argument("--commit", type=nonempty, required=True, help="commit the release was built from")
    for command in (build_parser, package_parser, verify_parser, check_parser):
        command.add_argument("--repo", type=Path, default=Path("."), help="Git repository holding the source (default: .)")
        command.add_argument("--config", type=nonempty,
                             help=f"config path inside the commit (default: {CONFIG}); required with --builds")
    args = parser.parse_args(argv)
    if args.command == "package" and args.builds is not None and (args.commit is None or args.config is None):
        parser.error("--builds needs --commit and --config, the trusted inputs build was given, to check build.json against")
    if args.config is None:
        args.config = CONFIG
    if args.command == "package" and args.commit is None:
        args.commit = "HEAD"
    if args.command == "verify" and args.tag and not args.repository:
        parser.error("--tag needs --repository")
    try:
        if args.command == "check":
            print(check_release(args.repo, args.release_dir, args.commit, args.config), end="")
            return 0
        if args.command == "build":
            commit = build_twice(args.repo, args.commit, args.config, args.output)
            print(f"Built {commit} twice into {args.output}", file=sys.stderr)
            return 0
        if args.command == "package":
            if args.builds is not None:
                manifest = package_builds(args.repo, args.builds, args.output, args.commit, args.config)
            else:
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
