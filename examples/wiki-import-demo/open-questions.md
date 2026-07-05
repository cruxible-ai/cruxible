# Open Questions

Things we have not decided. If you (human or agent) resolve one, move it to
the decisions page and delete it here. Historically we do the first half.

## Should dispatch workers be region-pinned?

EU customers are asking about data residency for in-flight events. Pinning
workers per region doubles our minimum footprint. Nobody has costed this.
Blocks: the enterprise tier pricing work.

## What do we do about the events table growth?

`events` is 1.4 TB and grows ~40 GB/week. Options discussed: native Postgres
partitioning + drop old partitions, move cold events to S3/Parquet, or just
buy bigger disks for another year. The 2025-11 "one database" decision makes
the S3 option awkward for the analytics replica. No owner.

## Event bus: do we still need to revisit Kafka vs Redis Streams?

The March decision said keep Kafka. But the managed-Kafka bill went up 60% in
May, and the decision said revisit "only if the bill doubles." Is a 60% jump
close enough to reopen it, or do we wait? Unclear who gets to call this.

## Do we officially support customer endpoints behind mTLS?

Two enterprise prospects asked. Dispatch can technically do it (there's an
undocumented flag), but supporting it means cert rotation UX in the console.
Sales thinks we already said yes to one prospect. Nobody can find where.
