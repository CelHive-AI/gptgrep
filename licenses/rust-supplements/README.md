# Version-bound source license supplements

These files fill missing license payloads in published crate archives. They are
original upstream bytes, not regenerated text from an SPDX identifier. Each
package directory contains `provenance.json` with its version, packaged Git
revision, actual license revision, primary URL, Git blob ID and SHA-256.
`scripts/collect_licenses.py` verifies that the installed crate's
`.cargo_vcs_info.json` revision and the retained license digests match before
including a supplement in a release inventory.

| Published package | Packaged revision | Retained license source |
|---|---|---|
| profiling 1.0.18 | `8271551172eb6fa4cba47369aedd93790c623df9` | Same revision, Apache-2.0 and MIT texts |
| profiling-procmacros 1.0.18 | `8271551172eb6fa4cba47369aedd93790c623df9` | Same revision, Apache-2.0 and MIT texts |
| pulp-wasm-simd-flag 0.1.1 | `5eb07fd7b68edf0a5e19f71737d315f72a510295` | Same revision, upstream LICENSE |
| simd_helpers 0.1.0 | `ca1a2f84aa386d758e98f8a609d990263932fb85` | Next upstream commit `82040194cd05affb060bf94d6f19f82a771d07fb`, only adds LICENSE; compiled source unchanged |
| uuid-simd 0.8.0 | `d74c030d9dc4f3cae02146d1f497ff62726ef09a` | Same revision, upstream MIT LICENSE |
| vsimd 0.8.0 | `d74c030d9dc4f3cae02146d1f497ff62726ef09a` | Same revision, upstream MIT LICENSE |
| zune-core 0.4.12 | `f8fbb123d5ed04441e8324a555bfcda0cb1bd28f` | Same revision, full Zlib text and upstream alternative-license notice |
| zune-inflate 0.2.54 | `69502ce83fdfecdd0beefd677e2abb3781b29d98` | Same revision, full Zlib text and upstream alternative-license notice |
| zune-jpeg 0.4.21 | `fa2c767a01d7d9373911d0bf63e0588553d67e0e` | Same revision, full Zlib text and upstream alternative-license notice |

The simd_helpers exception is supported by the author's
[one-file comparison](https://github.com/lu-zero/simd_helpers/compare/ca1a2f84aa386d758e98f8a609d990263932fb85...82040194cd05affb060bf94d6f19f82a771d07fb).
The source blob is verified against the installed package when collecting it.
Zune's short alternative-license notice is retained verbatim and is not labelled
as the full MIT or Apache-2.0 text; its full Zlib terms are the retained complete
license alternative.
