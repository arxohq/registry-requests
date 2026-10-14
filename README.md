# Arxo registry requests

Requests to add your package to the public Arxo registry
([arxohq/registry](https://github.com/arxohq/registry), registry id `arxo`).

You do not need this repository to publish a package. Your own registry (a
directory in your git repository or on static HTTPS) plus a `.arxo` file is
the main route, and it needs nothing from Arxo; see
[Publish your package](https://docs.arxo.io/build/packages/publish/). This
repository is for authors who also want their package in the curated `arxo`
registry, where `law add <name>@<version>` finds it without `--registry`.

## How a request works

1. **You pack and release.** `law pack` your package, then put the packed
   files as assets of a GitHub Release in your own public repository.
2. **You open a pull request** that adds exactly one file,
   `requests/<name>-<version>/request.json`. It names the Release, the
   license and the sha256 of every file. You need no token from Arxo.
3. **A machine checks it** (workflow `check-request`, no secrets). The report
   arrives as a comment on the pull request.
4. **A maintainer reviews** what a machine cannot check (checklist below) and
   merges. Merging approves exactly the sha256 named in the request.
5. **The acceptance workflow publishes.** It downloads the bytes again,
   stores them as a Release `accepted/<name>@<version>` of this repository,
   records a candidate in `candidates/` and publishes into `arxohq/registry`.
   From then on the published bytes do not depend on your Release.

### Make the request

With a CLI that has `law publish --request`:

```sh
law pack --project my-package --out artifacts/acme.parcel_tariff-0.1.0
law publish artifacts/acme.parcel_tariff-0.1.0 \
  --request requests/acme.parcel_tariff-0.1.0/request.json --bytes release-assets \
  --source acme/parcel-tariff@parcel-tariff-0.1.0 --commit "$(git rev-parse parcel-tariff-0.1.0^{commit})" \
  --license Apache-2.0
gh release create parcel-tariff-0.1.0 release-assets/* --repo acme/parcel-tariff
```

With `@arxo/cli` 0.2.6, write a candidate and convert it:

```sh
law publish artifacts/acme.parcel_tariff-0.1.0 --candidate cand.json --bytes release-assets
python3 tools/make_request.py cand.json --source acme/parcel-tariff@parcel-tariff-0.1.0 \
  --commit "$(git rev-parse parcel-tariff-0.1.0^{commit})" --license Apache-2.0 \
  --out requests/acme.parcel_tariff-0.1.0/request.json
```

Several packages that depend on each other can travel in one request: run
`law publish --request` once per artifact into the same file, with the same
Release. Name the request directory after the package the others lead to.

### What the machine checks

- the request matches `schema/registry-request.schema.json`; `path` values are
  relative, without `..`, absolute parts or duplicates; `file` values are bare
  file names; each file is at most 64 MiB and a request at most 256 MiB;
- the pull request adds only its own `request.json`;
- the Release is in a public repository outside `arxohq`, its tag points to the
  named commit, and every asset has the named size and sha256 (downloaded
  anonymously);
- the artifacts are restored and published with the pinned `law` into a
  scratch copy of `arxohq/registry`, dependencies before dependents; the
  publication refusals of `law` (a taken version with other bytes, a
  dependency that is not in `arxo`, a damaged artifact) refuse the request;
- the registry's own gate `check_registry.py --base` passes on that copy;
- a scratch consumer installs the packages with `law add … --from arxo` and
  `law install --offline`, which checks the closure, the exact language
  release and the resources;
- the name prefix (the part before the first dot) is not reserved
  (`reserved-prefixes.toml`) and is not held by another owner
  (`prefixes.toml`). The first accepted request with a new prefix claims it for
  the owner of its source repository.

## Maintainer checklist

Merge only when every line holds. The machine report must be green first.

- [ ] **License.** `license` is an SPDX identifier that allows redistribution
      of the published files, and it agrees with the LICENSE of the source
      repository.
- [ ] **Clean sources.** Texts the package carries are public domain, official
      acts, or the author's own text. No copied commercial commentary,
      textbooks or databases under copyright.
- [ ] **Name.** The name says what the package is (`<publisher>.<subject>[.<module>]`
      is recommended), does not imitate an Arxo package or another publisher,
      and a new prefix is one the author can reasonably claim.
- [ ] **Description.** The package's own description and README say what it
      models and where its model ends.
- [ ] **Provenance.** The source repository and the tagged commit exist and
      contain the package sources.
- [ ] **The hashes you approve** are the ones in the request and in the green
      report. Merging approves exactly them.

## What is not promised

- **No deadline.** Requests are reviewed by people when they can.
- **No automatic acceptance.** A green machine check is necessary, not
  sufficient.
- **No signature.** Packages in `arxo` are published without a publisher
  signature today; consumers verify them by hash.
- **No rewriting.** A published version never changes. A fix is a new version
  and a new request.

## Repository layout

```
requests/<name>-<version>/request.json   requests (one per pull request)
candidates/<name>-<version>.json         written by the acceptance workflow
reserved-prefixes.toml                   prefixes of Arxo packages
prefixes.toml                            prefixes held by external publishers
schema/registry-request.schema.json      request format
toolchain.lock.json                      the pinned law binary (@arxo/cli, npm)
tools/intake.py                          both workflows' logic
tools/vendor/                            pinned candidate scripts, see VENDORED.json
```

## For the owner: setup

- Secret `REGISTRY_DEPLOY_KEY`: the private half of a deploy key of
  `arxohq/registry` with write access.
- Branch protection of `main`: require the `check-request / check` status and a
  review; let GitHub Actions push to `main` (the acceptance workflow commits
  the candidate and the prefix claim).
