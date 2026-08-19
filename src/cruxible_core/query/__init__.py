"""Residual query vocabulary reached by deferred config-schema validation.

PC-F deleted the query engine, evaluation, filter, projection, continuation,
layout, and read-surface donors. What survives is exactly the chain
``cruxible_core.config.schema`` reaches when it validates a named query:
``enums`` (imported eagerly), ``predicates`` (imported inside
``_validate_top_level_query_predicate_scopes``), and the ``types``,
``profiles``, and ``relationship_state`` modules those two pull in. The residue
leaves with the config donor in PC-H.

Nothing is re-exported here: the old ``execute_query``/``QueryResult``
accessors pointed at the deleted engine.
"""
