# Atlas Replication

## Leader Election

Replica promotion requires fencing the previous leader.
Failover interval: 12 seconds.
Literal recovery marker: REPLICA_RECOVERY_OK.

## Election Journal

The election log key is vote_epoch.

