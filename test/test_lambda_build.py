"""Exercise the packaging contract, and the Git and container workflow when Docker is available."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import tracemalloc
import unittest
from unittest import mock
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "lambda_build.py"
sys.path.insert(0, str(ROOT))
import lambda_build  # noqa: E402

COMMIT = "a" * 40
# Small multi-platform image, pinned like a real build image.
BUSYBOX = "busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
# A single handler packaged by the original release scripts; its digest pins the ZIP format.
FIXTURE_HANDLER = b"export const handler = async () => ({statusCode: 200});\n"
FIXTURE_SHA256 = "5defbcbbbeedd07df0c6258fa4a4bdc9347e418a6ad4d95a51a6a7aeed2662a6"


def config(**settings):
    """Return a valid parsed config, overridden by `settings`."""
    base = {"image": BUSYBOX, "build": "true", "runtime": "nodejs24.x", "architecture": "arm64",
            "assets": [{"name": "api", "directory": "build/api"}]}
    return lambda_build.parse_config(toml({**base, **settings}))


def toml(settings):
    """Serialize the flat settings and [[assets]] tables these tests use."""
    lines = [f"{key} = {json.dumps(value)}" for key, value in settings.items() if key != "assets"]
    for asset in settings.get("assets", []):
        lines.append("[[assets]]")
        lines += [f"{key} = {json.dumps(value)}" for key, value in asset.items()]
    return "\n".join(lines) + "\n"


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def files(self, contents):
        for relative, data in contents.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def package(self, settings=None, output="release"):
        return lambda_build.package(self.root, settings or config(), COMMIT, self.root / output)

    def assert_rejected(self, message, settings=None, output="release"):
        """Expect packaging with these config overrides to fail, at config validation or later."""
        with self.assertRaisesRegex(lambda_build.ReleaseError, message):
            self.package(config(**settings) if settings is not None else None, output)
        self.assertFalse((self.root / output).exists(), "a failed packaging must not leave release files")

    def test_matches_the_original_release_format_byte_for_byte(self):
        self.files({"build/api/index.mjs": FIXTURE_HANDLER})
        self.package()
        self.assertEqual(hashlib.sha256((self.root / "release/api.zip").read_bytes()).hexdigest(), FIXTURE_SHA256)

    def test_writes_reproducible_zips_checksums_and_manifest(self):
        self.files({"build/api/index.mjs": b"export const handler = () => 1;",
                    "build/api/assets/logo.png": b"logo", "build/api/node_modules/a/index.js": b""})
        manifest = self.package()
        release = self.root / "release"
        archive = (release / "api.zip").read_bytes()
        digest = hashlib.sha256(archive).hexdigest()
        self.assertEqual((release / "SHA256SUMS").read_text(), f"{digest}  api.zip\n")
        # Runtime and architecture belong to each asset, so a repository can later build assets for
        # several runtimes without a new manifest format.
        expected = {"format_version": 3, "source_commit": COMMIT,
                    "assets": [{"name": "api", "asset": "api.zip", "sha256": digest, "size": len(archive),
                                "runtime": "nodejs24.x", "architecture": "arm64"}]}
        self.assertEqual(manifest, expected)
        self.assertEqual(json.loads((release / "manifest.json").read_text()), expected)
        with ZipFile(release / "api.zip") as bundle:
            entries = bundle.infolist()
        self.assertEqual([e.filename for e in entries], ["assets/logo.png", "index.mjs", "node_modules/a/index.js"])
        for entry in entries:
            self.assertEqual((entry.compress_type, entry.date_time, entry.external_attr >> 16),
                             (ZIP_STORED, (1980, 1, 1, 0, 0, 0), 0o100644))

        # Machine-specific metadata must not reach the ZIP bytes.
        (self.root / "build/api/index.mjs").chmod(0o755)
        os.utime(self.root / "build/api/assets/logo.png", (0, 0))
        self.package(output="again")
        self.assertEqual((self.root / "again/api.zip").read_bytes(), archive)

    def test_package_memory_does_not_grow_with_the_number_of_assets(self):
        # package once held every ZIP in memory until it staged them all.
        def peak(count, output):
            for index in range(count):
                self.files({f"build/functions/f{index}/index.mjs": bytes([index]) * (2 * 1024 * 1024)})
            tracemalloc.start()
            try:
                lambda_build.package(self.root, config(assets=[], assets_from="build/functions"), COMMIT, self.root / output)
                return tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
        one = peak(1, "one")
        eight = peak(8, "eight")
        self.assertLess(eight, one + 3 * 1024 * 1024, f"one asset peaked at {one} bytes, eight at {eight}")

    def test_changed_files_change_the_digest(self):
        self.files({"build/api/index.mjs": b"one"})
        self.package()
        self.files({"build/api/index.mjs": b"two"})
        self.package(output="again")
        self.assertNotEqual((self.root / "release/SHA256SUMS").read_text(), (self.root / "again/SHA256SUMS").read_text())

    def test_marks_only_named_files_executable_and_requires_bootstrap_for_os_only_runtimes(self):
        self.files({"build/functions/web/bootstrap": b"\x7fELF web", "build/functions/worker/bootstrap": b"\x7fELF worker"})
        functions = {"assets": [], "assets_from": "build/functions", "runtime": "provided.al2023"}
        self.assert_rejected("executable bootstrap", dict(**functions))
        manifest = self.package(config(**functions, executable=["bootstrap"]))
        self.assertEqual([a["asset"] for a in manifest["assets"]], ["web.zip", "worker.zip"])
        with ZipFile(self.root / "release/web.zip") as bundle:
            self.assertEqual(bundle.getinfo("bootstrap").external_attr >> 16, 0o100755)
        self.assert_rejected("matches no packaged file", dict(
            assets=[{"name": "api", "directory": "build/functions/web"}], executable=["missing"]), output="other")

    def test_expected_files_must_match_exactly(self):
        self.files({"build/api/index.mjs": b"x", "build/api/logo.png": b"y"})
        listed = config(assets=[{"name": "api", "directory": "build/api", "files": ["index.mjs", "logo.png"]}])
        self.package(listed)
        self.files({"build/api/.env": b"SECRET=example"})
        self.assert_rejected(r"unexpected \['.env'\]", listed, output="again")

    def test_rejects_unsafe_or_ambiguous_inputs(self):
        self.files({"build/api/index.mjs": b"x"})
        (self.root / "build/api/link.mjs").symlink_to(self.root / "build/api/index.mjs")
        self.assert_rejected("symlink")
        (self.root / "build/api/link.mjs").unlink()
        self.assert_rejected("Asset name", dict(assets=[{"name": "-api", "directory": "build/api"}]))
        self.assert_rejected("only once", dict(assets=[{"name": "api", "directory": "build/api"}] * 2))
        self.assert_rejected("inside the repository", dict(assets=[{"name": "api", "directory": "../api"}]))
        self.assert_rejected("expected a directory", dict(assets=[{"name": "api", "directory": "missing"}]))
        (self.root / "empty").mkdir()
        self.assert_rejected("no files", dict(assets=[{"name": "api", "directory": "empty"}]))
        with self.assertRaisesRegex(lambda_build.ReleaseError, "full, lowercase Git commit SHA"):
            lambda_build.package(self.root, config(), "main", self.root / "release")

    def test_rejects_asset_names_that_differ_only_in_case(self):
        # On a case-insensitive filesystem API.zip and api.zip are one file, so one ZIP would
        # silently replace the other while the manifest lists both.
        self.files({"build/upper/index.mjs": b"upper", "build/lower/index.mjs": b"lower"})
        self.assert_rejected("differ only in case", dict(assets=[{"name": "API", "directory": "build/upper"},
                                                                   {"name": "api", "directory": "build/lower"}]))
        # A second safeguard: an existing ZIP is never overwritten.
        (self.root / "taken.zip").write_bytes(b"first")
        with self.assertRaises(FileExistsError):
            lambda_build.write_zip([("index.mjs", b"second", False)], self.root / "taken.zip")
        self.assertEqual((self.root / "taken.zip").read_bytes(), b"first")

    def test_rejects_asset_directories_reached_through_symlinks(self):
        outside = self.root / "outside/api"
        outside.mkdir(parents=True)
        (outside / "secret.txt").write_text("host secret")
        (self.root / "build").mkdir()
        (self.root / "build/link").symlink_to(self.root / "outside")
        self.assert_rejected("build/link is a symlink", dict(assets=[{"name": "api", "directory": "build/link/api"}]))
        self.assert_rejected("build/link is a symlink", dict(assets=[], assets_from="build/link"))

    def test_refuses_to_mix_with_existing_release_files(self):
        self.files({"build/api/index.mjs": b"x", "release/old.zip": b"stale"})
        with self.assertRaisesRegex(lambda_build.ReleaseError, "new or empty directory"):
            self.package()
        self.assertEqual([p.name for p in (self.root / "release").iterdir()], ["old.zip"])

    def test_enforces_lambda_direct_upload_limits(self):
        self.files({"build/api/index.mjs": b"x" * 64})
        with mock.patch.object(lambda_build, "MAX_UNPACKED_BYTES", 63):
            self.assert_rejected("250 MiB unzipped")
        with mock.patch.object(lambda_build, "MAX_ZIP_BYTES", 100):
            self.assert_rejected("50 MiB direct-upload")


class StagingTests(unittest.TestCase):
    def test_a_failed_second_build_leaves_no_release_files_or_builds(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo, output = Path(temporary) / "repo", Path(temporary) / "release"
            repo.mkdir()
            (repo / "lambda-build.toml").write_text(toml({"image": BUSYBOX, "build": "true", "runtime": "nodejs24.x",
                                                             "architecture": "arm64",
                                                             "assets": [{"name": "api", "directory": "build/api"}]}))
            git = ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com"]
            subprocess.run([*git, "init", "-q"], check=True)
            subprocess.run([*git, "add", "-A"], check=True)
            subprocess.run([*git, "commit", "-q", "-m", "release"], check=True)

            calls = []

            def build_tree(repo, commit, config, root):
                calls.append(root)
                if len(calls) == 2:
                    raise lambda_build.ReleaseError("second build failed")
                (root / "build/api").mkdir(parents=True)
                (root / "build/api/index.mjs").write_text("first")

            with mock.patch.object(lambda_build, "build_tree", build_tree):
                with self.assertRaisesRegex(lambda_build.ReleaseError, "second build failed"):
                    lambda_build.package_commit(repo, "HEAD", "lambda-build.toml", output)
                builds = Path(temporary) / "builds"
                calls.clear()
                with self.assertRaisesRegex(lambda_build.ReleaseError, "second build failed"):
                    lambda_build.build_twice(repo, "HEAD", "lambda-build.toml", builds)
            self.assertEqual(len(calls), 2)
            self.assertFalse(output.exists())
            self.assertFalse(builds.exists(), "a failed build must not leave a builds directory")
            self.assertEqual(list(output.parent.glob(".release-*")) + list(output.parent.glob(".builds-*")), [])


class ExportTests(unittest.TestCase):
    """The build input is the committed tree, whatever the local clone's settings say."""

    def test_local_attributes_and_config_cannot_change_the_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            git = ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com"]
            subprocess.run([*git, "init", "-q"], check=True)
            contents = {"src/ignored.txt": b"kept\n", "src/subst.txt": b"$Format:%H$\n", "src/crlf.txt": b"one\ntwo\n"}
            for relative, data in contents.items():
                (repo / relative).parent.mkdir(parents=True, exist_ok=True)
                (repo / relative).write_bytes(data)
            (repo / "run.sh").write_bytes(b"#!/bin/sh\n")
            (repo / "run.sh").chmod(0o755)
            subprocess.run([*git, "add", "-A"], check=True)
            subprocess.run([*git, "commit", "-q", "-m", "source"], check=True)
            commit = lambda_build.resolve_commit(repo, "HEAD")
            # None of these are committed, so none may change what is built.
            (repo / ".git/info/attributes").write_text(
                "src/ignored.txt export-ignore\nsrc/subst.txt export-subst\nsrc/crlf.txt text eol=crlf\n")
            subprocess.run([*git, "config", "core.autocrlf", "true"], check=True)

            destination = Path(temporary) / "export"
            destination.mkdir()
            lambda_build.export(repo, commit, destination)
            exported = {p.relative_to(destination).as_posix(): p.read_bytes()
                        for p in destination.rglob("*") if p.is_file()}
            self.assertEqual(exported, {**contents, "run.sh": b"#!/bin/sh\n"})
            self.assertTrue(os.access(destination / "run.sh", os.X_OK))

    def test_recreates_links_inside_the_tree_and_rejects_links_outside(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            git = ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com"]
            subprocess.run([*git, "init", "-q"], check=True)
            (repo / "run.sh").write_text("#!/bin/sh\n")
            (repo / "link.sh").symlink_to("run.sh")
            subprocess.run([*git, "add", "-A"], check=True)
            subprocess.run([*git, "commit", "-q", "-m", "inside"], check=True)
            inside = Path(temporary) / "inside"
            inside.mkdir()
            lambda_build.export(repo, lambda_build.resolve_commit(repo, "HEAD"), inside)
            self.assertEqual(os.readlink(inside / "link.sh"), "run.sh")

            (repo / "escape").symlink_to("/etc")
            subprocess.run([*git, "add", "-A"], check=True)
            subprocess.run([*git, "commit", "-q", "-m", "outside"], check=True)
            outside = Path(temporary) / "outside"
            outside.mkdir()
            with self.assertRaisesRegex(lambda_build.ReleaseError, "links outside the tree"):
                lambda_build.export(repo, lambda_build.resolve_commit(repo, "HEAD"), outside)


class CheckTests(unittest.TestCase):
    """`check` accepts a release directory only if it is exactly what `package` writes for its ZIPs and commit."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.settings = {"image": BUSYBOX, "build": "true", "runtime": "provided.al2023", "architecture": "arm64",
                         "executable": ["bootstrap"],
                         "assets": [{"name": "api", "directory": "build/api", "files": ["bootstrap"]},
                                    {"name": "worker", "directory": "build/worker"}]}
        (self.repo / "lambda-build.toml").write_text(toml(self.settings))
        git = ["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.com"]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "config"], check=True)
        self.commit = lambda_build.resolve_commit(self.repo, "HEAD")
        tree = self.root / "tree"
        for name in ("api", "worker"):
            (tree / "build" / name).mkdir(parents=True)
            (tree / "build" / name / "bootstrap").write_bytes(f"#!/bin/sh\necho {name}\n".encode())
        self.release = self.root / "release"
        lambda_build.package(tree, lambda_build.parse_config(toml(self.settings)), self.commit, self.release)
        self.manifest_text = (self.release / "manifest.json").read_text()

    def check(self, commit=None):
        return subprocess.run([sys.executable, str(SCRIPT), "check", "--release-dir", str(self.release),
                               "--commit", commit or self.commit, "--repo", str(self.repo)],
                              text=True, capture_output=True)

    def assert_blocked(self, message):
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(message, result.stderr)

    def manifest(self, change):
        manifest = json.loads(self.manifest_text)
        change(manifest)
        (self.release / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    def test_accepts_exactly_what_package_writes(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, (self.release / "SHA256SUMS").read_text())

    def test_blocks_a_manifest_for_another_commit(self):
        result = self.check(commit="b" * 40)
        self.assertEqual(result.returncode, 1)

    def test_blocks_an_extra_json_document(self):
        (self.release / "manifest.json").write_text("{}\n" + self.manifest_text)
        self.assert_blocked("manifest.json does not match")

    def test_blocks_whitespace_in_a_digest(self):
        self.manifest(lambda m: m["assets"][0].update(sha256=m["assets"][0]["sha256"] + "\n"))
        self.assert_blocked("manifest.json does not match")

    def test_blocks_stray_bytes_in_sha256sums(self):
        sums = (self.release / "SHA256SUMS").read_bytes()
        for stray in (b"\0", b"\n", b"\n\n"):
            with self.subTest(stray=stray):
                (self.release / "SHA256SUMS").write_bytes(sums + stray)
                self.assert_blocked("SHA256SUMS does not match")

    def test_blocks_wrong_or_missing_names_and_sizes(self):
        changes = {"wrong name": lambda m: m["assets"][0].update(name="other"),
                   "missing name": lambda m: m["assets"][0].pop("name"),
                   "wrong size": lambda m: m["assets"][0].update(size=m["assets"][0]["size"] + 1),
                   "missing size": lambda m: m["assets"][0].pop("size"),
                   "format 2": lambda m: m.update(format_version=2)}
        for label, change in changes.items():
            with self.subTest(label):
                self.manifest(change)
                self.assert_blocked("manifest.json does not match")

    def test_blocks_extra_and_missing_files(self):
        (self.release / "notes.txt").write_text("extra")
        self.assert_blocked("notes.txt")
        (self.release / "notes.txt").unlink()
        (self.release / "worker.zip").unlink()
        self.assert_blocked("worker.zip")

    def test_blocks_changed_or_noncanonical_zips(self):
        original = (self.release / "api.zip").read_bytes()
        (self.release / "api.zip").write_bytes(original + b"x")
        self.assert_blocked("api.zip")
        # Matching metadata cannot make a ZIP canonical: here the bootstrap lost its executable bit.
        with ZipFile(self.release / "api.zip", "w") as bundle:
            bundle.writestr(ZipInfo("bootstrap", (1980, 1, 1, 0, 0, 0)), b"#!/bin/sh\necho api\n")
        self.assert_blocked("api.zip is not the canonical ZIP")


class HostileReleaseTests(unittest.TestCase):
    """`check` must fail cleanly and cheaply on crafted releases whose checksums and manifest match their ZIPs."""

    NODE = {"image": BUSYBOX, "build": "true", "runtime": "nodejs24.x", "architecture": "arm64"}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)
        self.release = self.root / "release"
        self.release.mkdir()

    def commit_config(self, settings):
        (self.repo / "lambda-build.toml").write_text(toml(settings))
        git = ["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.com"]
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "config"], check=True)
        return subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()

    def write_release(self, commit, zips):
        """Write the ZIPs with a SHA256SUMS and manifest.json that describe them exactly."""
        entries = []
        for name, data in sorted(zips.items()):
            (self.release / f"{name}.zip").write_bytes(data)
            entries.append({"name": name, "asset": f"{name}.zip", "sha256": hashlib.sha256(data).hexdigest(),
                            "size": len(data), "runtime": "nodejs24.x", "architecture": "arm64"})
        (self.release / "SHA256SUMS").write_text("".join(f"{e['sha256']}  {e['asset']}\n" for e in entries))
        manifest = {"format_version": 3, "source_commit": commit, "assets": entries}
        (self.release / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    @staticmethod
    def stored_zip(entries):
        """Return stored ZIP bytes with canonical metadata for (path, bytes) entries, in the given order."""
        buffer = io.BytesIO()
        with ZipFile(buffer, "w", compression=ZIP_STORED) as bundle:
            for path, data in entries:
                info = ZipInfo(path, (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                bundle.writestr(info, data)
        return buffer.getvalue()

    def cli(self, command, commit, release=None):
        args = ["--release-dir", str(release or self.release), "--commit", commit] if command == "check" else \
            ["--output", str(self.root / "out")]
        return subprocess.run([sys.executable, str(SCRIPT), command, *args, "--repo", str(self.repo)],
                              text=True, capture_output=True)

    def assert_check_blocks(self, commit, message, release=None):
        result = self.cli("check", commit, release)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(message, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_rejects_compressed_entries_before_expanding_them(self):
        commit = self.commit_config({**self.NODE, "assets_from": "build"})
        buffer = io.BytesIO()
        with ZipFile(buffer, "w") as bundle:
            info = ZipInfo("index.mjs", (1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED  # 8 MiB of zeros in a few KiB
            bundle.writestr(info, b"\0" * (8 * 1024 * 1024))
        self.write_release(commit, {"api": buffer.getvalue()})
        self.assert_check_blocks(commit, "api.zip: entry index.mjs is not stored")

    def test_rejects_too_many_entries_cleanly(self):
        commit = self.commit_config({**self.NODE, "assets_from": "build"})
        buffer = io.BytesIO()
        with ZipFile(buffer, "w", compression=ZIP_STORED) as bundle:
            for index in range(65536):
                bundle.writestr(ZipInfo(f"f{index:05d}", (1980, 1, 1, 0, 0, 0)), b"")
        self.write_release(commit, {"api": buffer.getvalue()})
        self.assert_check_blocks(commit, "api.zip uses Zip64")

    def test_rejects_oversized_files_before_reading_them(self):
        commit = self.commit_config({**self.NODE, "assets_from": "build"})
        self.write_release(commit, {"api": self.stored_zip([("index.mjs", b"x")])})
        with open(self.release / "api.zip", "r+b") as archive:
            archive.truncate(50 * 1024 * 1024 + 1)  # sparse, so the test stays cheap
        self.assert_check_blocks(commit, "api.zip exceeds Lambda's 50 MiB direct-upload limit")
        self.write_release(commit, {"api": self.stored_zip([("index.mjs", b"x")])})
        with open(self.release / "manifest.json", "r+b") as manifest:
            manifest.truncate(64 * 1024 * 1024)
        self.assert_check_blocks(commit, "manifest.json does not match")

    def test_rejects_duplicate_noncanonical_and_conflicting_paths(self):
        commit = self.commit_config({**self.NODE, "assets": [{"name": "api", "directory": "build/api"}]})
        cases = {"duplicate": ([("index.mjs", b"one"), ("index.mjs", b"two")], "duplicate path index.mjs"),
                 "dot segment": ([("./index.mjs", b"x")], "'./index.mjs' is not a canonical relative path"),
                 "file and directory": ([("a", b"x"), ("a/b", b"y")], "a is both a file and a directory")}
        for label, (entries, message) in cases.items():
            with self.subTest(label):
                for path in self.release.iterdir():
                    path.unlink()
                self.write_release(commit, {"api": self.stored_zip(entries)})
                self.assert_check_blocks(commit, message)

    def test_rejects_a_deep_path_quickly(self):
        # 32,000 directory levels in about 128 KiB once made the prefix check take seconds.
        commit = self.commit_config({**self.NODE, "assets_from": "build"})
        self.write_release(commit, {"api": self.stored_zip([("a/" * 32000 + "f", b"x")])})
        started = time.monotonic()
        # The end record already shows a central directory too large for one entry's 1024-byte path.
        self.assert_check_blocks(commit, "central directory that does not match its end record")
        self.assertLess(time.monotonic() - started, 5)

    def check_peak(self, commit):
        """Run check in this process and return its peak traced allocation in bytes."""
        tracemalloc.start()
        try:
            lambda_build.check_release(self.repo, self.release, commit, "lambda-build.toml")
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    def test_memory_does_not_grow_with_the_number_of_zips(self):
        # Each ZIP's 10,000 paths once stayed in memory until every ZIP had been read.
        commit = self.commit_config({**self.NODE, "assets_from": "build"})
        zip_data = self.stored_zip([(f"{index:05d}-" + "n" * 100, b"") for index in range(10000)])
        self.write_release(commit, {"f0": zip_data})
        one = self.check_peak(commit)
        for path in self.release.iterdir():
            path.unlink()
        self.write_release(commit, {f"f{index}": zip_data for index in range(8)})
        eight = self.check_peak(commit)
        self.assertLess(eight, one + 3 * 1024 * 1024, f"one ZIP peaked at {one} bytes, eight at {eight}")

    def test_caps_the_number_of_assets(self):
        commit = self.commit_config({**self.NODE, "assets_from": "build"})
        self.write_release(commit, {f"f{index:03d}": self.stored_zip([("index.mjs", b"x")]) for index in range(101)})
        self.assert_check_blocks(commit, "more than 100 assets")
        with self.assertRaisesRegex(lambda_build.ReleaseError, "more than 100 assets"):
            lambda_build.check_names([f"f{index}" for index in range(101)])
        lambda_build.check_names([f"f{index}" for index in range(100)])

    def test_refuses_oversized_central_directories_before_parsing_them(self):
        # 100,000 records once allocated about 37 MiB in ZipFile before the count was checked.
        buffer = io.BytesIO()
        with ZipFile(buffer, "w") as bundle:
            for index in range(100000):
                bundle.writestr(ZipInfo(f"f{index:06d}", (1980, 1, 1, 0, 0, 0)), b"")
        data = buffer.getvalue()
        config = lambda_build.parse_config(toml({**self.NODE, "assets_from": "build"}))
        tracemalloc.start()
        try:
            with self.assertRaisesRegex(lambda_build.ReleaseError, "api.zip uses Zip64"):
                lambda_build.read_zip_entries("api", data, None, config)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 1024 * 1024)

    def test_accepts_a_file_name_that_looks_like_a_zip64_locator(self):
        # The last 20 bytes before the end record are this name, which once read as a Zip64 locator.
        commit = self.commit_config({**self.NODE, "assets_from": "build"})
        self.write_release(commit, {"api": self.stored_zip([("PK\x06\x07" + "a" * 16, b"x")])})
        result = self.cli("check", commit)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_verify_refuses_an_oversized_manifest_before_reading_it(self):
        commit = self.commit_config({**self.NODE, "assets_from": "build"})
        self.write_release(commit, {"api": self.stored_zip([("index.mjs", b"x")])})
        with open(self.release / "manifest.json", "r+b") as manifest:
            manifest.truncate(64 * 1024 * 1024)
        tracemalloc.start()
        try:
            with self.assertRaisesRegex(lambda_build.ReleaseError, "manifest.json is larger than"):
                lambda_build.verify(self.repo, self.release, "lambda-build.toml")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_verify_reports_a_deeply_nested_manifest_cleanly(self):
        # Deep nesting once raised an uncaught RecursionError inside json; the depth that does so varies by Python version.
        commit = self.commit_config({**self.NODE, "assets_from": "build"})
        self.write_release(commit, {"api": self.stored_zip([("index.mjs", b"x")])})
        (self.release / "manifest.json").write_text("[" * 400000 + "]" * 400000)  # 800 KB, under the 1 MiB cap
        result = subprocess.run([sys.executable, str(SCRIPT), "verify", "--release-dir", str(self.release),
                                 "--repo", str(self.repo)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("is not a lambda-build manifest", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_refuses_archives_that_are_not_one_plain_central_directory(self):
        commit = self.commit_config({**self.NODE, "assets_from": "build"})
        valid = self.stored_zip([("index.mjs", b"x")])
        cases = {"a trailing comment": (valid[:-2] + b"\x02\x00hi", "end record"),
                 "bytes after the end record": (valid + b"x", "end record")}
        for label, (data, message) in cases.items():
            with self.subTest(label):
                for path in self.release.iterdir():
                    path.unlink()
                self.write_release(commit, {"api": data})
                self.assert_check_blocks(commit, message)

    def test_rejects_empty_releases_and_empty_zips(self):
        commit = self.commit_config({**self.NODE, "assets_from": "build"})
        self.write_release(commit, {})
        self.assert_check_blocks(commit, "no assets")
        self.write_release(commit, {"api": self.stored_zip([])})
        self.assert_check_blocks(commit, "api: no files to package")

    def test_rejects_invalid_configs_through_both_commands(self):
        invalid = {"duplicate asset names": ({"assets": [{"name": "api", "directory": "build/a"},
                                                         {"name": "api", "directory": "build/b"}]}, "only once"),
                   "a directory outside the repository": ({"assets": [{"name": "api", "directory": "../outside"}]},
                                                          "must be a path inside the repository"),
                   # assets_from would also package build/api as api, so package saw two assets named api.
                   "an asset inside assets_from": ({"assets_from": "build",
                                                    "assets": [{"name": "api", "directory": "build/api"}]},
                                                   "asset sources overlap"),
                   "two assets on one directory": ({"assets": [{"name": "api", "directory": "build/api",
                                                                "files": ["index.mjs"]},
                                                               {"name": "web", "directory": "build/api",
                                                                "files": ["other.mjs"]}]},
                                                   "asset sources overlap"),
                   # No build could create these directories, so no release can come from them.
                   "a NUL in a directory": ({"assets": [{"name": "api", "directory": "build/a\0b"}]},
                                            "contains a NUL"),
                   "a 256-byte name in assets_from": ({"assets_from": "build/" + "a" * 256},
                                                      "a name longer than 255 bytes"),
                   "an asset name whose ZIP name exceeds 255 bytes": ({"assets": [{"name": "a" * 252,
                                                                                    "directory": "build/a"}]},
                                                                      "longer than 255 bytes"),
                   "a directory longer than 1024 bytes": ({"assets": [{"name": "api",
                                                                       "directory": "/".join(["a" * 200] * 6)}]},
                                                          "longer than 1024 bytes")}
        for label, (settings, message) in invalid.items():
            commit = self.commit_config({**self.NODE, **settings})
            for command in ("check", "package"):
                with self.subTest(label, command=command):
                    result = self.cli(command, commit)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn(message, result.stderr)

    def test_rejects_a_symlinked_release_directory(self):
        commit = self.commit_config({**self.NODE, "assets_from": "build"})
        self.write_release(commit, {"api": self.stored_zip([("index.mjs", b"x")])})
        self.assertEqual(self.cli("check", commit).returncode, 0, "the real directory itself passes")
        link = self.root / "link"
        link.symlink_to(self.release)
        self.assert_check_blocks(commit, "is a symlink", release=link)


class BuildRecordTests(unittest.TestCase):
    """package --builds trusts only its --commit and --config, and refuses builds recorded for anything else."""

    SETTINGS = {"image": BUSYBOX, "build": "true", "runtime": "nodejs24.x", "architecture": "arm64",
                "assets": [{"name": "api", "directory": "build/api"}]}
    OTHER = {**SETTINGS, "assets": [{"name": "other", "directory": "build/other"}]}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.first = self.commit({"lambda-build.toml": toml(self.SETTINGS), "other.toml": toml(self.OTHER)})
        self.second = self.commit({"lambda-build.toml": toml(self.OTHER)})
        # Two identical build trees, as lambda_build.py build would leave them for the first commit.
        self.builds = self.root / "builds"
        for tree in ("first", "second"):
            for name in ("api", "other"):
                (self.builds / tree / "build" / name).mkdir(parents=True)
                (self.builds / tree / "build" / name / "index.mjs").write_text(name)
        self.record(source_commit=self.first, config="lambda-build.toml",
                    config_blob=self.git("rev-parse", f"{self.first}:lambda-build.toml"))

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                               *args], check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, files):
        for name, text in files.items():
            (self.repo / name).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "change")
        return self.git("rev-parse", "HEAD")

    def record(self, **fields):
        (self.builds / "build.json").write_text(json.dumps(fields))

    def package(self, *args, output="release"):
        return subprocess.run([sys.executable, str(SCRIPT), "package", "--builds", str(self.builds), *args,
                               "--repo", str(self.repo), "--output", str(self.root / output)],
                              text=True, capture_output=True)

    def assert_refused(self, result, message):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stderr)
        self.assertFalse((self.root / "release").exists())

    def test_packages_builds_recorded_for_the_trusted_commit_and_config(self):
        result = self.package("--commit", self.first, "--config", "lambda-build.toml")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.root / "release/manifest.json").read_text())["source_commit"], self.first)

    def test_requires_the_trusted_commit_and_config(self):
        self.assert_refused(self.package("--config", "lambda-build.toml"), "--builds needs --commit and --config")
        self.assert_refused(self.package("--commit", self.first), "--builds needs --commit and --config")

    def test_refuses_a_record_relabeled_with_another_commit(self):
        # Outputs built from the first commit, relabeled as the second.
        self.record(source_commit=self.second, config="lambda-build.toml",
                    config_blob=self.git("rev-parse", f"{self.second}:lambda-build.toml"))
        self.assert_refused(self.package("--commit", self.first, "--config", "lambda-build.toml"),
                            "build.json records commit")

    def test_refuses_a_record_pointing_at_another_config(self):
        self.record(source_commit=self.first, config="other.toml",
                    config_blob=self.git("rev-parse", f"{self.first}:other.toml"))
        self.assert_refused(self.package("--commit", self.first, "--config", "lambda-build.toml"),
                            "build.json records config other.toml")

    def test_refuses_a_different_config_under_the_same_path(self):
        self.record(source_commit=self.first, config="lambda-build.toml",
                    config_blob=self.git("rev-parse", f"{self.first}:other.toml"))
        self.assert_refused(self.package("--commit", self.first, "--config", "lambda-build.toml"),
                            "was built with different lambda-build.toml contents")

    def test_rejects_empty_commit_and_config_values(self):
        for command in (["package", "--output", str(self.root / "out")], ["build", "--output", str(self.root / "out")],
                        ["check", "--release-dir", str(self.root)], ["verify", "--release-dir", str(self.root)]):
            for flag in ("--commit", "--config"):
                if command[0] == "verify" and flag == "--commit":
                    continue
                extra = ["--commit", self.first] if command[0] == "check" and flag == "--config" else []
                with self.subTest(command=command[0], flag=flag):
                    result = subprocess.run([sys.executable, str(SCRIPT), *command, *extra, flag, "",
                                             "--repo", str(self.repo)], text=True, capture_output=True)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("must not be empty", result.stderr)


class CaseCollisionTests(unittest.TestCase):
    """Committed paths that differ only in case would overwrite each other on macOS and Windows."""

    def commit_tree(self, repo, layout):
        """Commit `layout` ({path: bytes or nested dict}) with plumbing, which works on any filesystem."""
        def tree(entries):
            lines = []
            for name, value in entries.items():
                if isinstance(value, dict):
                    lines.append(f"040000 tree {tree(value)}\t{name}")
                else:
                    blob = self.git(repo, "hash-object", "-w", "--stdin", input=value)
                    lines.append(f"100644 blob {blob}\t{name}")
            return self.git(repo, "mktree", input="\n".join(lines).encode() + b"\n")
        return self.git(repo, "commit-tree", tree(layout), "-m", "collide", input=b"")

    def git(self, repo, *args, input):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                               *args], input=input, check=True, capture_output=True).stdout.decode().strip()

    def assert_rejected(self, layout, message):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            commit = self.commit_tree(repo, layout)
            destination = Path(temporary) / "export"
            destination.mkdir()
            with self.assertRaisesRegex(lambda_build.ReleaseError, message):
                lambda_build.export(repo, commit, destination)
            self.assertEqual(list(destination.iterdir()), [], "nothing may be written before the check")

    def test_rejects_files_that_differ_only_in_case(self):
        self.assert_rejected({"A.txt": b"upper", "a.txt": b"lower"}, "A.txt and a.txt differ only in case")

    def test_rejects_directories_that_differ_only_in_case(self):
        self.assert_rejected({"Src": {"x": b"x"}, "src": {"y": b"y"}}, "Src and src differ only in case")

    def test_files_are_created_exclusively(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            commit = self.commit_tree(repo, {"a.txt": b"committed"})
            destination = Path(temporary) / "export"
            destination.mkdir()
            (destination / "a.txt").write_bytes(b"already here")
            with self.assertRaises(FileExistsError):
                lambda_build.export(repo, commit, destination)
            self.assertEqual((destination / "a.txt").read_bytes(), b"already here")


class PathLimitTests(unittest.TestCase):
    """package and check share filesystem limits on ZIP paths, tested at each limit and one past it."""

    def assert_paths(self, files, message=None):
        if message is None:
            lambda_build.check_paths("api", files)
        else:
            with self.assertRaisesRegex(lambda_build.ReleaseError, message):
                lambda_build.check_paths("api", files)

    def test_limits_each_component_to_255_bytes(self):
        self.assert_paths(["a" * 255])
        self.assert_paths(["dir/" + "é" * 127 + "a"])  # 255 bytes in UTF-8
        self.assert_paths(["a" * 256], "longer than 255 bytes")
        self.assert_paths(["dir/" + "é" * 128], "longer than 255 bytes")

    def test_limits_each_path_to_1024_bytes(self):
        at_limit = "/".join(["a" * 200] * 5) + "/" + "b" * 19  # 1024 bytes
        self.assertEqual(len(at_limit.encode()), 1024)
        self.assert_paths([at_limit])
        self.assert_paths([at_limit + "c"], "longer than 1024 bytes")

    def test_memory_stays_bounded_for_many_deep_paths(self):
        # A tree of directory prefixes once peaked at about 91 MiB for these 1000 empty files.
        files = [f"{index:05d}/" + "a/" * 500 + "f" for index in range(1000)]
        tracemalloc.start()
        try:
            lambda_build.check_paths("api", files)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 8 * 1024 * 1024)

    def test_limits_a_zip_to_the_entries_the_writer_supports(self):
        # Python's zipfile switches to Zip64 at 65535 entries, which the canonical writer refuses.
        files = [f"f{index:05d}" for index in range(65534)]
        self.assert_paths(files)
        lambda_build.check_end_record("api", lambda_build.zip_bytes([(path, b"", False) for path in files]))
        self.assert_paths(files + ["g"], "more than 65534 files")

    def test_limits_asset_names_so_the_zip_name_fits_in_255_bytes(self):
        lambda_build.check_name("a" * 251)  # a...a.zip is 255 bytes
        with self.assertRaisesRegex(lambda_build.ReleaseError, "longer than 255 bytes"):
            lambda_build.check_name("a" * 252)

    def test_rejects_nul_in_paths(self):
        self.assert_paths(["a\0b"], "contains a NUL")

    def test_rejects_files_that_are_also_directories_in_either_order(self):
        self.assert_paths(["a", "a/b"], "a is both a file and a directory")
        self.assert_paths(["a/b/c", "a/b"], "a/b is both a file and a directory")
        self.assert_paths(["a/b", "a/c", "d"])
        # "a!" sorts between "a" and "a/b" as a string, but not by path components.
        self.assert_paths(["a/b", "a!", "a"], "a is both a file and a directory")
        self.assert_paths(["a!", "a/b", "a", "a"], "duplicate path a")


class ConfigTests(unittest.TestCase):
    def assert_invalid(self, message, **settings):
        with self.assertRaisesRegex(lambda_build.ReleaseError, message):
            config(**settings)

    def test_rejects_unpinned_images_typos_and_missing_settings(self):
        self.assert_invalid("pin image by digest", image="busybox:1.37.0")
        self.assert_invalid(r"unknown settings \['exectuable'\]", exectuable=["bootstrap"])
        self.assert_invalid("set build", build="")
        self.assert_invalid("architecture must be one of", architecture="aarch64")
        self.assert_invalid("set assets or assets_from", assets=[])
        self.assert_invalid("needs name and directory", assets=[{"name": "api"}])
        with self.assertRaisesRegex(lambda_build.ReleaseError, "lambda-build.toml"):
            lambda_build.parse_config("image = ")

    def test_fills_in_optional_settings(self):
        parsed = config()
        self.assertEqual((parsed["executable"], parsed["assets"][0]["directory"]), ([], "build/api"))


def docker_available():
    return shutil.which("docker") is not None and subprocess.run(
        ["docker", "info"], capture_output=True).returncode == 0


@unittest.skipUnless(docker_available() or os.environ.get("LAMBDA_BUILD_REQUIRE_DOCKER"),
                     "Docker is not available; set LAMBDA_BUILD_REQUIRE_DOCKER=1 to require these tests")
class ContainerTests(unittest.TestCase):
    """Run the command line against a real Git repository and container builds."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                               *args], check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, build, files=None):
        (self.repo / "lambda-build.toml").write_text(toml({
            "image": BUSYBOX, "build": build, "runtime": "nodejs24.x", "architecture": "arm64",
            "assets": [{"name": "api", "directory": "build/api", "files": ["index.mjs"]}]}))
        for relative, data in (files or {"src/index.mjs": FIXTURE_HANDLER}).items():
            (self.repo / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / relative).write_bytes(data)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "release")
        return self.git("rev-parse", "HEAD")

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args, "--repo", str(self.repo)],
                              cwd=self.root, text=True, capture_output=True)

    def test_packages_the_committed_tree_twice_in_the_container(self):
        commit = self.commit("mkdir -p build/api && cp src/index.mjs build/api/")
        # Uncommitted edits and untracked files must not reach the release.
        (self.repo / "src/index.mjs").write_text("changed")
        (self.repo / "build/api").mkdir(parents=True)
        (self.repo / "build/api/stray.txt").write_text("stray")
        result = self.cli("package", "--output", str(self.root / "release"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{FIXTURE_SHA256}  api.zip\n")
        self.assertEqual(result.stderr.count("$ mkdir -p build/api"), 2)
        self.assertEqual(json.loads((self.root / "release/manifest.json").read_text())["source_commit"], commit)

        verified = self.cli("verify", "--release-dir", str(self.root / "release"))
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertIn("Rebuilt release matches.", verified.stderr)

        # A release that the source cannot reproduce fails verification, whether the metadata
        # or a published ZIP changed, or a file is missing or extra.
        release = self.root / "release"
        pristine = {p.name: p.read_bytes() for p in release.iterdir()}

        def assert_differs(change, name):
            for path in release.iterdir():
                path.unlink()
            for file, data in pristine.items():
                (release / file).write_bytes(data)
            change()
            result = self.cli("verify", "--release-dir", str(release))
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(f"differs from the release in {name}", result.stderr)

        assert_differs(lambda: (release / "SHA256SUMS").write_text(f"{'0' * 64}  api.zip\n"), "SHA256SUMS")
        assert_differs(lambda: (release / "api.zip").write_bytes(pristine["api.zip"] + b"x"), "api.zip")
        assert_differs(lambda: (release / "api.zip").unlink(), "api.zip")
        assert_differs(lambda: (release / "extra.zip").write_bytes(b"extra"), "extra.zip")

    def test_verify_downloads_every_release_file(self):
        with mock.patch.object(lambda_build.subprocess, "run") as run:
            lambda_build.download_release("fabricahq/example", "v1.2.3", self.root)
        self.assertEqual(run.call_args.args[0], ["gh", "release", "download", "v1.2.3", "--repo", "fabricahq/example",
                                                 "--dir", str(self.root)])

    def test_verify_compares_an_oversized_zip_without_reading_it(self):
        self.commit("mkdir -p build/api && cp src/index.mjs build/api/")
        release = self.root / "release"
        self.assertEqual(self.cli("package", "--output", str(release)).returncode, 0)
        with open(release / "api.zip", "r+b") as archive:
            archive.truncate(60 * 1024 * 1024)  # sparse
        tracemalloc.start()
        try:
            changed = lambda_build.verify(self.repo, release, "lambda-build.toml")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(changed, ["api.zip"])
        self.assertLess(peak, 16 * 1024 * 1024)

    def test_builds_and_packages_as_separate_steps(self):
        commit = self.commit("mkdir -p build/api && cp src/index.mjs build/api/")
        builds = self.root / "builds"
        built = self.cli("build", "--output", str(builds))
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertEqual(built.stderr.count("$ mkdir -p build/api"), 2)
        self.assertEqual(sorted(p.name for p in builds.iterdir()), ["build.json", "first", "second"])
        self.assertTrue((builds / "first/build/api/index.mjs").is_file())
        config_blob = self.git("rev-parse", f"{commit}:lambda-build.toml")
        self.assertEqual(json.loads((builds / "build.json").read_text()),
                         {"source_commit": commit, "config": "lambda-build.toml", "config_blob": config_blob})

        trusted = ["--commit", commit, "--config", "lambda-build.toml"]
        packaged = self.cli("package", "--builds", str(builds), *trusted, "--output", str(self.root / "split"))
        self.assertEqual(packaged.returncode, 0, packaged.stderr)
        self.assertEqual(packaged.stdout, f"{FIXTURE_SHA256}  api.zip\n")
        self.assertNotIn("$ mkdir", packaged.stderr, "packaging two builds must not build again")
        whole = self.cli("package", "--output", str(self.root / "whole"))
        self.assertEqual(whole.returncode, 0, whole.stderr)
        self.assertEqual(lambda_build.differences(self.root / "split", self.root / "whole"), [])

        # Packaging still requires the two builds to produce identical files.
        (builds / "second/build/api/index.mjs").write_text("changed")
        differ = self.cli("package", "--builds", str(builds), *trusted, "--output", str(self.root / "differ"))
        self.assertEqual(differ.returncode, 1)
        self.assertIn("produced different", differ.stderr)
        self.assertFalse((self.root / "differ").exists())

    def test_rejects_irreproducible_and_failing_builds(self):
        self.commit("mkdir -p build/api && echo $RANDOM$RANDOM > build/api/index.mjs")
        result = self.cli("package", "--output", str(self.root / "release"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("produced different SHA256SUMS, api.zip, manifest.json", result.stderr)
        self.assertFalse((self.root / "release").exists())

        self.commit("exit 3")
        result = self.cli("package", "--output", str(self.root / "release"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("returned non-zero exit status 3", result.stderr)
        self.assertFalse((self.root / "release").exists())

    def test_a_build_cannot_package_host_files_through_a_symlink(self):
        (self.repo / "lambda-build.toml").write_text(toml({
            "image": BUSYBOX, "build": "mkdir -p build && ln -s / build/link", "runtime": "nodejs24.x",
            "architecture": "arm64", "assets": [{"name": "api", "directory": "build/link/etc"}]}))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "release")
        result = self.cli("package", "--output", str(self.root / "release"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("build/link is a symlink", result.stderr)
        self.assertFalse((self.root / "release").exists())

    def test_the_image_entrypoint_cannot_swallow_the_build(self):
        # An entrypoint that ignores its arguments and succeeds would skip the build silently.
        image = "lambda-build-test-entrypoint:local"
        dockerfile = f"FROM {BUSYBOX}\nENTRYPOINT [\"/bin/echo\", \"entrypoint ran instead\"]\n"
        subprocess.run(["docker", "build", "--quiet", "--platform", "linux/arm64", "--tag", image, "-"],
                       input=dockerfile, text=True, check=True, capture_output=True)
        self.addCleanup(subprocess.run, ["docker", "image", "rm", "--force", image], capture_output=True)
        root = self.root / "tree"
        root.mkdir()
        lambda_build.run_build(image, "arm64", "echo built > built.txt", root)
        self.assertEqual((root / "built.txt").read_text(), "built\n")

    def test_verify_needs_the_release_source_commit(self):
        release = self.root / "release"
        release.mkdir()
        (release / "manifest.json").write_text(json.dumps({"format_version": 3, "source_commit": "b" * 40}))
        self.commit("true")
        result = self.cli("verify", "--release-dir", str(release))
        self.assertEqual(result.returncode, 1)
        self.assertIn("fetch it first", result.stderr)

        # A manifest from another format version is not this tool's to rebuild.
        (release / "manifest.json").write_text(json.dumps({"format_version": 2, "source_commit": "b" * 40}))
        result = self.cli("verify", "--release-dir", str(release))
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not a lambda-build manifest of format 3", result.stderr)


if __name__ == "__main__":
    unittest.main()
