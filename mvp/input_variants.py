"""Structured input-variant signals shared by scanners and run boundaries."""

from __future__ import annotations


class Record61StatusInventoryError(ValueError):
    """The Record status-box inventory is outside the validated template.

    This is a structured input-variant signal.  It is intentionally distinct
    from generic parsing failures so the run boundary can publish a manual or
    unsupported result with the observed inventory instead of reporting a
    worker crash.
    """

    code = "RECORD61_STATUS_INVENTORY_UNSUPPORTED"

    def __init__(
        self,
        *,
        total: int,
        native: int,
        alternate: int,
        expected_total: int = 851,
        expected_native: int = 849,
        expected_alternate: int = 2,
    ) -> None:
        self.inventory = {
            "observed": {"total": total, "native": native, "alternate": alternate},
            "expected": {
                "total": expected_total,
                "native": expected_native,
                "alternate": expected_alternate,
            },
        }
        super().__init__(
            "unexpected GB 9706.1 status inventory: "
            f"total={total}, native={native}, alternate={alternate}"
        )

