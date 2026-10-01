"""Exercise the packaging contract, and the Git and container workflow when Docker is available."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from zipfile import ZIP_STORED, ZipFile

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
        with self.assertRaisesRegex(lambda_build.ReleaseError, message):
            self.package(settings, output)
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
        expected = {"format_version": 2, "source_commit": COMMIT, "runtime": "nodejs24.x", "architecture": "arm64",
                    "assets": [{"name": "api", "asset": "api.zip", "sha256": digest, "size": len(archive)}]}
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

    def test_changed_files_change_the_digest(self):
        self.files({"build/api/index.mjs": b"one"})
        self.package()
        self.files({"build/api/index.mjs": b"two"})
        self.package(output="again")
        self.assertNotEqual((self.root / "release/SHA256SUMS").read_text(), (self.root / "again/SHA256SUMS").read_text())

    def test_marks_only_named_files_executable_and_requires_bootstrap_for_os_only_runtimes(self):
        self.files({"build/functions/web/bootstrap": b"\x7fELF web", "build/functions/worker/bootstrap": b"\x7fELF worker"})
        functions = {"assets": [], "assets_from": "build/functions", "runtime": "provided.al2023"}
        self.assert_rejected("executable bootstrap", config(**functions))
        manifest = self.package(config(**functions, executable=["bootstrap"]))
        self.assertEqual([a["asset"] for a in manifest["assets"]], ["web.zip", "worker.zip"])
        with ZipFile(self.root / "release/web.zip") as bundle:
            self.assertEqual(bundle.getinfo("bootstrap").external_attr >> 16, 0o100755)
        self.assert_rejected("matches no packaged file", config(
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
        self.assert_rejected("Asset name", config(assets=[{"name": "-api", "directory": "build/api"}]))
        self.assert_rejected("only once", config(assets=[{"name": "api", "directory": "build/api"}] * 2))
        self.assert_rejected("inside the repository", config(assets=[{"name": "api", "directory": "../api"}]))
        self.assert_rejected("expected a directory", config(assets=[{"name": "api", "directory": "missing"}]))
        (self.root / "empty").mkdir()
        self.assert_rejected("no files", config(assets=[{"name": "api", "directory": "empty"}]))
        with self.assertRaisesRegex(lambda_build.ReleaseError, "full, lowercase Git commit SHA"):
            lambda_build.package(self.root, config(), "main", self.root / "release")

    def test_rejects_asset_names_that_differ_only_in_case(self):
        # On a case-insensitive filesystem API.zip and api.zip are one file, so one ZIP would
        # silently replace the other while the manifest lists both.
        self.files({"build/upper/index.mjs": b"upper", "build/lower/index.mjs": b"lower"})
        self.assert_rejected("differ only in case", config(assets=[{"name": "API", "directory": "build/upper"},
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
        self.assert_rejected("build/link is a symlink", config(assets=[{"name": "api", "directory": "build/link/api"}]))
        self.assert_rejected("build/link is a symlink", config(assets=[], assets_from="build/link"))

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
    def test_a_failed_second_build_leaves_no_release_files(self):
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

            def build(repo, commit, config, destination):
                calls.append(destination)
                if len(calls) == 2:
                    raise lambda_build.ReleaseError("second build failed")
                destination.mkdir()
                (destination / "api.zip").write_text("first")

            with mock.patch.object(lambda_build, "build", build):
                with self.assertRaisesRegex(lambda_build.ReleaseError, "second build failed"):
                    lambda_build.package_commit(repo, "HEAD", "lambda-build.toml", output)
            self.assertEqual(len(calls), 2)
            self.assertFalse(output.exists())
            self.assertEqual(list(output.parent.glob(".release-*")), [])


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

        # A release that the source cannot reproduce fails verification.
        (self.root / "release/SHA256SUMS").write_text(f"{'0' * 64}  api.zip\n")
        tampered = self.cli("verify", "--release-dir", str(self.root / "release"))
        self.assertEqual(tampered.returncode, 1)
        self.assertIn("differs from the release in SHA256SUMS", tampered.stderr)

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

    def test_verify_needs_the_release_source_commit(self):
        release = self.root / "release"
        release.mkdir()
        (release / "manifest.json").write_text(json.dumps({"format_version": 2, "source_commit": "b" * 40}))
        self.commit("true")
        result = self.cli("verify", "--release-dir", str(release))
        self.assertEqual(result.returncode, 1)
        self.assertIn("fetch it first", result.stderr)


if __name__ == "__main__":
    unittest.main()
