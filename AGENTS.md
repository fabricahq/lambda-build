# Lambda build agent guide

The [README](README.md) explains what this repository does and how application repositories use it.

The repository has two parts that share one artifact contract:

- `lambda_build.py` builds a commit in its pinned container and packages the release files, or rebuilds a published release to verify it.
- `.github/workflows/build.yml` runs it in a read-only job and uploads the `release-assets` artifact for Release Planner's release-assets hook. It checks out `lambda_build.py` from `job.workflow_sha`, so callers pin one commit for everything.

lambda-build never tags or publishes; Release Planner does. Deployments pin release digests, so treat the ZIP bytes, `SHA256SUMS`, and `manifest.json` as a public format. A change that alters the bytes for the same input needs a new manifest `format_version` and a deliberate decision, never a side effect. The fixture digest test and `examples/go-function/expected-SHA256SUMS` exist to catch such changes.

Keep these invariants:

- Use only the Python standard library, Git, and Docker.
- The build workflow keeps `contents: read`, refuses `pull_request_target`, and builds only a full commit SHA.
- Pin every action and container image to a full digest or commit SHA, with a version comment for actions.

Run `python3 -B -m unittest discover -s test -v` with Docker running before you push. When a command, config setting, workflow input, or release file changes, update the README in the same pull request.
