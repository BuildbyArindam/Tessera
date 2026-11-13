"""How an Airwallex transfer status or webhook event name maps onto Tessera's TransferStatus.

Only `payout.transfer.*` event names that the transfers list page mentions are matched. The page does not say what
each one means, so the mapping is conservative: only `paid` is SETTLED, only `failed` and `cancelled` are FAILED,
and every other name (processing, sent, scheduled, funding.*) says "still in flight" and changes nothing. A name
or status that is not recognised is never read as success.
"""

from tessera_airwallex.models import TransferStatus

_FROM_STATUS = {
    "PAID": TransferStatus.SETTLED,
    "FAILED": TransferStatus.FAILED,
    "CANCELLED": TransferStatus.FAILED,
    "CANCELED": TransferStatus.FAILED,
}
_FROM_EVENT = {
    "payout.transfer.paid": TransferStatus.SETTLED,
    "payout.transfer.failed": TransferStatus.FAILED,
    "payout.transfer.cancelled": TransferStatus.FAILED,
}


def transfer_status_from_vendor(status: object) -> TransferStatus:
    """The port status for a vendor `status` string. Anything unrecognised is still SUBMITTED, never SETTLED."""
    return (
        _FROM_STATUS.get(status.upper(), TransferStatus.SUBMITTED)
        if isinstance(status, str)
        else (TransferStatus.SUBMITTED)
    )


def transfer_status_for_event(event_name: str) -> TransferStatus | None:
    """SETTLED or FAILED if the event ends a transfer, None if it does not (or is not a transfer event)."""
    return _FROM_EVENT.get(event_name)
