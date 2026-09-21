# Atlas Recovery Handbook

## Snapshot Restoration

The atlas service restores a damaged journal from a signed snapshot.
Restore command: atlasctl restore --snapshot SNAP-42.
Archive checksum: blake3:7bcf4e.

## Network Retries

Retry policy: maximum 4 attempts, exponential backoff 250 ms.
Literal recovery marker: ATLAS_RECOVERY_OK.

### Operator Verification

Verify journal integrity before admitting new writes.

