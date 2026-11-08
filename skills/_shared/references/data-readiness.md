# Data that needs to become state

Use this reference after the [copy-in rule](copy-in.md) establishes why the
instance needs a retained typed value. Preparation happens outside Cruxible;
its acceptance laws validate what arrives, not the source's real-world truth.

Resolve the input's grain: what one row represents and which stable key names
it across reloads. For a relationship with its own facts, distinguish the two
ends from the relationship's identity. A mutable label or row number does not
usually identify the same thing over time.

Inspect identifier normalization, null/sentinel values, duplicate keys and
join coverage where they affect the intended write. Check value types and
cardinality against existing ClaimTypes. A missing match is unknown unless the
source establishes a complete enumeration; partial inputs do not justify
retiring absent facts.

Keep a source-to-value mapping and evidence for transformations that affect
meaning. Identify the source bytes, transform and any records excluded or
unresolved. A focused sample can answer a small question; broader profiling
is justified by ambiguity or the import's scope, not by a required checklist.

Report the specific ambiguity that blocks an import, or the understood key,
grain, mappings and evidence that make it ready. Do not invent a domain model
or copy extra columns merely because they are present.
