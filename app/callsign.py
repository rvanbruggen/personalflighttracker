"""Resolving the ADS-B callsign for a flight.

The status provider usually supplies one. When it doesn't, we derive it from
the airline's ICAO code plus the numeric part of the flight number — the
convention most carriers follow (BA117 -> BAW117, KL1705 -> KLM1705).

This is a *fallback*, not a guarantee: some carriers use callsigns unrelated to
the flight number, so a derived callsign is flagged as such and the UI says the
position is unconfirmed rather than presenting a possibly-wrong aircraft.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# IATA -> ICAO for carriers whose ICAO code the status provider may omit.
# Only needed for the fallback path; the provider's own callsign wins.
_IATA_TO_ICAO = {
    "BA": "BAW", "KL": "KLM", "AF": "AFR", "LH": "DLH", "SN": "BEL",
    "IB": "IBE", "AZ": "ITY", "LX": "SWR", "OS": "AUA", "TP": "TAP",
    "EI": "EIN", "SK": "SAS", "AY": "FIN", "TK": "THY", "EK": "UAE",
    "QR": "QTR", "DL": "DAL", "AA": "AAL", "UA": "UAL", "FR": "RYR",
    "U2": "EZY", "VY": "VLG", "W6": "WZZ", "HV": "TRA", "LO": "LOT",
}

_NUMBER_RE = re.compile(r"^([A-Z0-9]{2,3}?)(\d{1,4})([A-Z]?)$")


@dataclass
class ResolvedCallsign:
    value: str
    derived: bool  # True when we guessed rather than read it from the provider

    def __bool__(self) -> bool:
        return bool(self.value)


def derive_callsign(flight_number: str, airline_icao: str = "") -> str:
    """'BA117' + 'BAW' -> 'BAW117'. Returns '' when it can't be derived."""
    match = _NUMBER_RE.match((flight_number or "").upper().replace(" ", ""))
    if not match:
        return ""
    prefix, digits, suffix = match.groups()

    icao = (airline_icao or "").upper().strip()
    if not icao:
        icao = _IATA_TO_ICAO.get(prefix, "")
    if not icao:
        return ""

    # Callsigns drop leading zeros: BA0117 -> BAW117.
    return f"{icao}{int(digits)}{suffix}"


def resolve_callsign(
    provider_callsign: str, flight_number: str, airline_icao: str = ""
) -> Optional[ResolvedCallsign]:
    """Prefer what the provider told us; fall back to derivation."""
    given = (provider_callsign or "").strip().upper()
    if given:
        return ResolvedCallsign(given, derived=False)

    guess = derive_callsign(flight_number, airline_icao)
    if guess:
        return ResolvedCallsign(guess, derived=True)
    return None
