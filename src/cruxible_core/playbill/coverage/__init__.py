"""Coverage delivery: what an ordinary source read has to do with accepted state.

Coverage is delivered, not fetched. Every module here answers one question --
"how does this working occurrence relate to accepted Playbill state?" -- and
none of them may answer it by writing anything down. The package is deliberately
import-poor: it reads the frozen source/evidence grammar and the accepted
projection coordinate, and it never reaches the proposal, settlement,
activation, compiler, or ledger-write paths. An architecture test holds that
line, because "coverage adds no authority" is the whole contract.

* :mod:`.contracts` -- the frozen §11.5-addendum grammar: the two closed enums,
  the logical-source and occurrence identities, the request/result cards.
* :mod:`.indexes` -- the two disposable indexes, both rebuilt from accepted
  state and the working snapshot and both worthless if deleted.
* :mod:`.manifest` -- the local atomic manifest and its monotonic epoch, which
  is what lets freshness fail closed without a live socket.
* :mod:`.resolver` -- one side-effect-free entry point over all three.
"""

from __future__ import annotations
