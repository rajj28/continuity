"""Age ratings and certification: the dimension with a legal deadline.

A film cannot be released in Germany without an FSK certificate, in Japan
without EIRIN, in India without CBFC. This is not a quality bar that can be
argued down -- it is a permission granted by a body outside the company, on a
date, for a specific cut, and it expires.

Three ways it blocks a release, and they need different responses:

    not submitted     nobody sent it. A human has to, and the lead time is
                      weeks. This is the one worth alerting on early.
    pending           submitted, awaiting a decision. Nothing to do but wait,
                      and the expected date is what an operator plans around.
    certificate stale The cut changed after the certificate was granted. The
                      certificate describes a specific runtime and content;
                      re-cutting invalidates it, and shipping the new cut under
                      the old certificate is a regulatory offence rather than a
                      QC failure.

That third case is why this lives next to the content-addressed store rather
than in a spreadsheet. A certificate records the master hash it was granted
against, so "is this certificate still valid for what we are about to ship" is
the same hash comparison every other staleness question here reduces to.

Like rights, and unlike everything else in this system, **none of this is
repairable by an agent.** No amount of processing produces a certificate. The
correct behaviour is to block, say which body and which state, and escalate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

REGISTER = Path(__file__).resolve().parents[1] / "assets" / "certificates.json"

# States a submission can be in. `EXEMPT` is real: some territories do not
# require certification for some distribution channels, and treating that as
# "missing" would block a release that was never obliged to have one.
SUBMITTED, PENDING, GRANTED, REFUSED, EXEMPT = (
    "not_submitted", "pending", "granted", "refused", "exempt"
)


class CertificateError(RuntimeError):
    pass


@dataclass
class Certificate:
    """One certification decision, for one market, against one cut."""

    market: str
    body: str
    state: str
    rating: str = ""
    granted_on: str = ""
    expires_on: str = ""
    # The master this was granted against. A re-cut invalidates the
    # certificate, and this is what makes that detectable rather than a thing
    # someone has to remember.
    master_sha256: str = ""
    expected_by: str = ""       # for PENDING: when a decision is expected
    note: str = ""

    def valid_on(self, when: date) -> bool:
        if self.state not in (GRANTED, EXEMPT):
            return False
        if self.expires_on and when > date.fromisoformat(self.expires_on):
            return False
        return True


@dataclass
class Certification:
    """Whether this market may ship, as far as certification is concerned."""

    market: str
    body: str
    state: str
    valid: bool
    reason: str
    rating: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def repairable(self) -> bool:
        """Always False, like rights and for the same reason.

        A certificate is a permission granted by a body outside this system.
        An agent that "repaired" a missing one would be forging it.
        """
        return False


@lru_cache(maxsize=1)
def load_register(path: Path | None = None) -> dict[str, Certificate]:
    source = path or REGISTER
    if not source.exists():
        raise CertificateError(
            f"no certificate register at {source}. A release system that "
            f"cannot say whether a title is certified cannot say whether it "
            f"may ship."
        )
    raw = json.loads(source.read_text(encoding="utf-8"))
    return {
        entry["market"]: Certificate(**entry)
        for entry in raw["certificates"]
    }


def certification(
    market: str,
    ratings_body: str,
    master_sha256: str,
    *,
    on: date | None = None,
    register: dict[str, Certificate] | None = None,
) -> Certification:
    """Can this market ship the cut identified by `master_sha256`?

    The hash is the point. A certificate granted against an earlier master is
    not a certificate for this one, however recently it was issued.
    """
    when = on or datetime.utcnow().date()
    certificates = register if register is not None else load_register()
    certificate = certificates.get(market)

    if certificate is None:
        return Certification(
            market=market, body=ratings_body, state=SUBMITTED, valid=False,
            reason=(f"no submission to {ratings_body}. Certification lead "
                    f"times run to weeks; a human has to start this."),
        )

    if certificate.state == REFUSED:
        return Certification(
            market=market, body=certificate.body, state=REFUSED, valid=False,
            reason=(f"{certificate.body} refused certification"
                    + (f": {certificate.note}" if certificate.note else "")),
            detail={"note": certificate.note},
        )

    if certificate.state == PENDING:
        return Certification(
            market=market, body=certificate.body, state=PENDING, valid=False,
            reason=(f"awaiting {certificate.body}"
                    + (f", expected by {certificate.expected_by}"
                       if certificate.expected_by else "")),
            detail={"expected_by": certificate.expected_by},
        )

    if certificate.state == SUBMITTED:
        return Certification(
            market=market, body=certificate.body, state=SUBMITTED, valid=False,
            reason=f"not submitted to {certificate.body}",
        )

    # GRANTED or EXEMPT -- but for which cut, and is it still in date?
    if certificate.state == GRANTED and certificate.master_sha256 and \
            master_sha256 and certificate.master_sha256 != master_sha256:
        return Certification(
            market=market, body=certificate.body, state=GRANTED, valid=False,
            rating=certificate.rating,
            reason=(
                f"{certificate.body} certified a different cut "
                f"({certificate.master_sha256[:12]}); this is "
                f"{master_sha256[:12]}. A re-cut invalidates the certificate, "
                f"and shipping under it would be a regulatory matter rather "
                f"than a QC one."
            ),
            detail={"certified_sha256": certificate.master_sha256,
                    "current_sha256": master_sha256},
        )

    if not certificate.valid_on(when):
        return Certification(
            market=market, body=certificate.body, state=certificate.state,
            valid=False, rating=certificate.rating,
            reason=f"certificate expired {certificate.expires_on}",
            detail={"expires_on": certificate.expires_on},
        )

    return Certification(
        market=market, body=certificate.body, state=certificate.state,
        valid=True, rating=certificate.rating,
        reason=(f"{certificate.body} {certificate.rating}"
                if certificate.rating else f"exempt from {certificate.body}"),
        detail={"granted_on": certificate.granted_on,
                "expires_on": certificate.expires_on},
    )
