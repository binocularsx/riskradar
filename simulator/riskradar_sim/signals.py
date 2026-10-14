"""The events around the payments: logins, devices, credentials, payees, SIMs (D77).

Plain English
-------------
Real people log in before they pay, add a new payee now and then, change a PIN,
get a new SIM. Attackers do the same things for a different reason, and in a
different order: guess the password or swap the SIM, bind their own phone,
change the PIN so the owner cannot get back in, enrol the mule accounts, then
move the money.

This layer writes both, around the payments the simulator already produces. It
never changes a payment. It draws from its **own** random stream and its own
reference counter, so the payment corpus a seed produced before this file
existed is byte-for-byte the payment corpus it produces now (checked, D77c), and
every measurement taken on it stays comparable.

Nothing here is a perfect marker. Most customers who change a SIM are not being
robbed; a phished takeover needs no failed logins; plenty of legitimate payees
are paid without ever being enrolled. A detector that learns a single one of
these as "fraud" will be wrong on real traffic, and the layer is written so it
would be wrong here too (D57).

The generator/detector wall holds (D10a): these are payloads with labels kept
beside them, exactly like payments. The API never sees a label.
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import datetime, timedelta

from .engine import Event
from .population import Customer

# A session: a login covers payments from the same device for this long.
SESSION_MINUTES = 20

# Background rates per customer per day, for things ordinary people do.
CREDENTIAL_CHANGE_RATE = 0.002
SIM_CHANGE_RATE = 0.0006
DEVICE_REBIND_RATE = 0.002
LIMIT_CHANGE_RATE = 0.001
# A legitimate new payee is enrolled before its first payment this often.
LEGIT_PAYEE_ENROL_RATE = 0.5
# A mistyped password before a successful login.
LEGIT_LOGIN_TYPO_RATE = 0.04

LOGIN_METHOD = {"MOBILE_APP": "BIOMETRIC", "WEB": "PASSWORD", "USSD": "PIN"}


class EventLayer:
    def __init__(self, seed: int, customers: list[Customer]) -> None:
        self.rng = random.Random(f"riskradar-events:{seed}")
        self.customers = customers
        self.by_id = {c.customer_id: c for c in customers}
        # Payees each customer already has, before the window began.
        self.payees: dict[str, set[str]] = {
            c.customer_id: set(c.known_beneficiaries) | set(c.payout_group) for c in customers
        }
        self.last_login: dict[tuple[str, str], datetime] = {}

    # ------------------------------------------------------------------ util

    def _ref(self) -> str:
        return f"EV{self.rng.getrandbits(80):020X}"

    def _event(self, kind: str, when: datetime, customer_id: str, *, detail: dict, account_id=None,
               device=None, region=None, channel=None, fraud: Event | None = None) -> Event:
        payload = {
            "event_type": kind,
            "event_ref": self._ref(),
            "occurred_at": when.isoformat(),
            "customer_id": customer_id,
            "account_id": account_id,
            "device_fingerprint": device,
            "ip_region": region,
            "channel": channel,
            "detail": detail,
        }
        return Event(payload, is_fraud=bool(fraud and fraud.is_fraud),
                     typology=fraud.typology if fraud else None,
                     incident_id=fraud.incident_id if fraud else None, kind=kind)

    def _login(self, out: list[Event], pay: Event, *, fraud: bool) -> None:
        p = pay.payload
        device = p.get("device_fingerprint")
        if not device or p["channel"] not in LOGIN_METHOD:
            return
        key = (p["customer_id"], device)
        when = datetime.fromisoformat(p["occurred_at"])
        last = self.last_login.get(key)
        if last and timedelta(0) <= when - last <= timedelta(minutes=SESSION_MINUTES):
            return
        at = when - timedelta(minutes=self.rng.uniform(0.5, 6))
        common = dict(device=device, region=p.get("ip_region"), channel=p["channel"],
                      fraud=pay if fraud else None)
        if not fraud and self.rng.random() < LEGIT_LOGIN_TYPO_RATE:
            for i in range(self.rng.randint(1, 2)):
                out.append(self._event("LOGIN", at - timedelta(seconds=30 * (i + 1)), p["customer_id"],
                                       detail={"result": "FAILED", "method": LOGIN_METHOD[p["channel"]],
                                               "failure_reason": "WRONG_CREDENTIAL"}, **common))
        out.append(self._event("LOGIN", at, p["customer_id"],
                               detail={"result": "SUCCESS", "method": LOGIN_METHOD[p["channel"]]}, **common))
        self.last_login[key] = at

    # ------------------------------------------------------------ per day

    def around(self, payments: list[Event]) -> list[Event]:
        """Events implied by a day's payments: sessions, enrolments, and fraud preludes."""
        out: list[Event] = []
        incidents: dict[str, list[Event]] = defaultdict(list)
        for pay in sorted(payments, key=lambda e: e.payload["occurred_at"]):
            if pay.incident_id:
                incidents[pay.incident_id].append(pay)
        for events in incidents.values():
            if events[0].typology == "ACCOUNT_TAKEOVER":
                out.extend(self._takeover_prelude(events))
            elif events[0].typology == "MULE_FANOUT":
                out.extend(self._mule_enrolment(events))
            elif events[0].typology == "SOCIAL_ENGINEERING":
                out.extend(self._scam_prelude(events))
            elif events[0].typology == "SIM_SWAP":
                out.extend(self._sim_swap_prelude(events))
            elif events[0].typology == "DORMANT_ACCOUNT":
                out.extend(self._dormant_prelude(events))

        for pay in sorted(payments, key=lambda e: e.payload["occurred_at"]):
            if pay.context:
                out.extend(self._ordinary_context(pay))
            self._login(out, pay, fraud=pay.is_fraud)
            if not pay.is_fraud:
                self._legit_payee(out, pay)
        return out

    # ------------------------------------------------------------ D82

    def _ordinary_context(self, pay: Event) -> list[Event]:
        """The legitimate twins' own history: a replaced SIM, a PIN reset at the branch."""
        p = pay.payload
        when = datetime.fromisoformat(p["occurred_at"])
        cid = p["customer_id"]
        if pay.context == "SIM_REPLACED":
            return [self._event("SIM_CHANGED", when - timedelta(minutes=self.rng.uniform(20, 480)), cid,
                                detail={"msisdn": _msisdn(cid), "carrier": self.rng.choice(["MTN", "AIRTEL", "GLO", "9MOBILE"])})]
        if pay.context == "OWNER_RETURNING" and self.rng.random() < 0.3:
            return [self._event("CREDENTIAL_CHANGED", when - timedelta(hours=self.rng.uniform(1, 30)), cid,
                                detail={"credential": self.rng.choice(["PIN", "PASSWORD"]),
                                        "initiated_by": self.rng.choice(["BRANCH", "CUSTOMER"])},
                                channel="BRANCH" if self.rng.random() < 0.5 else "MOBILE_APP")]
        return []

    def _scam_prelude(self, events: list[Event]) -> list[Event]:
        """The victim, coached on the phone, enrols the scammer's account and sometimes raises a limit."""
        first = events[0].payload
        cid = first["customer_id"]
        start = datetime.fromisoformat(first["occurred_at"])
        common = dict(device=first.get("device_fingerprint"), region=first.get("ip_region"),
                      channel=first["channel"], fraud=events[0])
        out: list[Event] = []
        if self.rng.random() < 0.15:
            old = self.rng.choice([200_000, 500_000]) * 100
            out.append(self._event("LIMIT_CHANGED", start - timedelta(minutes=self.rng.uniform(5, 30)), cid,
                                   account_id=first["account_id"],
                                   detail={"limit": "DAILY_TRANSFER", "from_minor": old, "to_minor": old * 5}, **common))
        seen: set[str] = set()
        for e in events:
            ben = e.payload.get("beneficiary_account_id")
            if not ben or ben in seen or ben in self.payees[cid]:
                continue
            seen.add(ben)
            if self.rng.random() < 0.75:
                when = datetime.fromisoformat(e.payload["occurred_at"]) - timedelta(minutes=self.rng.uniform(1, 10))
                out.append(self._event("PAYEE_ADDED", when, cid, account_id=e.payload["account_id"],
                                       detail={"beneficiary_account_id": ben}, **common))
        self.payees[cid] |= seen
        return out

    def _sim_swap_prelude(self, events: list[Event]) -> list[Event]:
        """The number moves to the attacker's SIM; on the app, they log in with an OTP and bind their phone."""
        first = events[0].payload
        cid = first["customer_id"]
        start = datetime.fromisoformat(first["occurred_at"])
        out = [self._event("SIM_CHANGED", start - timedelta(minutes=self.rng.uniform(20, 480)), cid,
                           detail={"msisdn": _msisdn(cid), "carrier": self.rng.choice(["MTN", "AIRTEL", "GLO", "9MOBILE"])},
                           fraud=events[0])]
        device = first.get("device_fingerprint")
        if first["channel"] == "MOBILE_APP" and device:
            login_at = start - timedelta(minutes=self.rng.uniform(2, 15))
            common = dict(device=device, region=first.get("ip_region"), channel="MOBILE_APP", fraud=events[0])
            out.append(self._event("LOGIN", login_at, cid, detail={"result": "SUCCESS", "method": "OTP"}, **common))
            self.last_login[(cid, device)] = login_at
            if self.rng.random() < 0.5:
                out.append(self._event("DEVICE_BOUND", login_at + timedelta(minutes=1), cid,
                                       detail={"binding": "BOUND"}, **common))
        self.payees[cid] |= {e.payload.get("beneficiary_account_id") for e in events} - {None}
        return out

    def _dormant_prelude(self, events: list[Event]) -> list[Event]:
        """The contact details change so no alert reaches the owner; then a login from somewhere new."""
        first = events[0].payload
        cid = first["customer_id"]
        start = datetime.fromisoformat(first["occurred_at"])
        out: list[Event] = []
        if self.rng.random() < 0.55:
            out.append(self._event("CREDENTIAL_CHANGED", start - timedelta(hours=self.rng.uniform(1, 48)), cid,
                                   detail={"credential": self.rng.choice(["PHONE", "EMAIL"]),
                                           "initiated_by": self.rng.choice(["BRANCH", "CONTACT_CENTRE"])},
                                   channel="BRANCH", fraud=events[0]))
        device = first.get("device_fingerprint")
        if device and first["channel"] in LOGIN_METHOD:
            login_at = start - timedelta(minutes=self.rng.uniform(2, 20))
            common = dict(device=device, region=first.get("ip_region"), channel=first["channel"], fraud=events[0])
            out.append(self._event("LOGIN", login_at, cid, detail={"result": "SUCCESS", "method": "OTP"}, **common))
            self.last_login[(cid, device)] = login_at
            if first["channel"] == "MOBILE_APP" and self.rng.random() < 0.7:
                out.append(self._event("DEVICE_BOUND", login_at + timedelta(minutes=1), cid,
                                       detail={"binding": "BOUND"}, **common))
        self.payees[cid] |= {e.payload.get("beneficiary_account_id") for e in events} - {None}
        return out

    def _legit_payee(self, out: list[Event], pay: Event) -> None:
        p = pay.payload
        ben, cid = p.get("beneficiary_account_id"), p["customer_id"]
        if not ben or ben in self.payees[cid]:
            return
        self.payees[cid].add(ben)
        if self.rng.random() < LEGIT_PAYEE_ENROL_RATE:
            when = datetime.fromisoformat(p["occurred_at"]) - timedelta(minutes=self.rng.uniform(1, 600))
            out.append(self._event("PAYEE_ADDED", when, cid, account_id=p["account_id"],
                                   device=p.get("device_fingerprint"), channel=p["channel"],
                                   detail={"beneficiary_account_id": ben}))

    def _takeover_prelude(self, events: list[Event]) -> list[Event]:
        """Get in, take the phone, lock the owner out, line up the mules."""
        first = events[0].payload
        cid = first["customer_id"]
        start = datetime.fromisoformat(first["occurred_at"])
        device = first.get("device_fingerprint")
        region = first.get("ip_region")
        out: list[Event] = []
        common = dict(device=device, region=region, channel="MOBILE_APP", fraud=events[0])

        way_in = self.rng.choices(["STUFFING", "SIM_SWAP", "PHISHED"], [0.55, 0.30, 0.15])[0]
        login_at = start - timedelta(minutes=self.rng.uniform(4, 25))
        if way_in == "STUFFING":
            # Placed backwards from the login, so the guessing always ends before it.
            gaps = [self.rng.uniform(10, 180) for _ in range(self.rng.randint(3, 15))]
            t = login_at - timedelta(seconds=20 + sum(gaps))
            for gap in gaps:
                out.append(self._event("LOGIN", t, cid, detail={"result": "FAILED", "method": "PASSWORD",
                                                                "failure_reason": "WRONG_CREDENTIAL"}, **common))
                t += timedelta(seconds=gap)
        elif way_in == "SIM_SWAP":
            out.append(self._event("SIM_CHANGED", login_at - timedelta(minutes=self.rng.uniform(15, 300)), cid,
                                   detail={"msisdn": _msisdn(cid), "carrier": self.rng.choice(["MTN", "AIRTEL", "GLO"])},
                                   channel=None, region=None, device=None, fraud=events[0]))
        method = "OTP" if way_in == "SIM_SWAP" else "PASSWORD"
        out.append(self._event("LOGIN", login_at, cid, detail={"result": "SUCCESS", "method": method}, **common))
        if device:
            self.last_login[(cid, device)] = login_at

        t = login_at + timedelta(minutes=self.rng.uniform(0.5, 3))
        if device and self.rng.random() < 0.8:
            out.append(self._event("DEVICE_BOUND", t, cid, detail={"binding": "BOUND"}, **common))
            t += timedelta(minutes=self.rng.uniform(0.5, 2))
        if self.rng.random() < 0.45:
            out.append(self._event("CREDENTIAL_CHANGED", t, cid,
                                   detail={"credential": self.rng.choice(["PIN", "MFA_METHOD", "PHONE"]),
                                           "initiated_by": "CUSTOMER"}, **common))

        # Enrol the destinations the owner has never used, minutes before each is first paid.
        seen: set[str] = set()
        for e in events:
            ben = e.payload.get("beneficiary_account_id")
            if not ben or ben in seen or ben in self.payees[cid]:
                continue
            seen.add(ben)
            if self.rng.random() < 0.65:
                when = datetime.fromisoformat(e.payload["occurred_at"]) - timedelta(minutes=self.rng.uniform(1, 15))
                out.append(self._event("PAYEE_ADDED", when, cid, account_id=e.payload["account_id"],
                                       detail={"beneficiary_account_id": ben}, **common))
        # Attackers' payees stay enrolled; the owner's later payments are unaffected.
        self.payees[cid] |= seen
        return out

    def _mule_enrolment(self, events: list[Event]) -> list[Event]:
        """A controlled account's destinations are often enrolled in a batch first."""
        if self.rng.random() >= 0.5:
            return []
        first = events[0].payload
        cid = first["customer_id"]
        start = datetime.fromisoformat(first["occurred_at"])
        out = []
        seen: set[str] = set()
        for e in events:
            ben = e.payload.get("beneficiary_account_id")
            if not ben or ben in seen or ben in self.payees[cid] or self.rng.random() >= 0.6:
                continue
            seen.add(ben)
            out.append(self._event("PAYEE_ADDED", start - timedelta(minutes=self.rng.uniform(2, 30)), cid,
                                   account_id=e.payload["account_id"], device=first.get("device_fingerprint"),
                                   channel=first["channel"], detail={"beneficiary_account_id": ben}, fraud=events[0]))
        self.payees[cid] |= seen
        return out

    def background(self, day: datetime, end: datetime) -> list[Event]:
        """What ordinary customers do on a day that has nothing to do with a payment."""
        out: list[Event] = []
        n = len(self.customers)
        for kind, rate in (("CREDENTIAL_CHANGED", CREDENTIAL_CHANGE_RATE), ("SIM_CHANGED", SIM_CHANGE_RATE),
                           ("DEVICE_BOUND", DEVICE_REBIND_RATE), ("LIMIT_CHANGED", LIMIT_CHANGE_RATE)):
            count = sum(1 for _ in range(n) if self.rng.random() < rate)
            for customer in self.rng.sample(self.customers, min(count, n)):
                lo, hi = customer.active_hours
                when = day.replace(hour=self.rng.randint(lo, min(hi, 23)), minute=self.rng.randint(0, 59),
                                   second=self.rng.randint(0, 59), microsecond=0)
                if when > end:
                    continue
                if kind == "CREDENTIAL_CHANGED":
                    detail = {"credential": self.rng.choice(["PASSWORD", "PIN", "MFA_METHOD", "EMAIL", "PHONE"]),
                              "initiated_by": self.rng.choices(["CUSTOMER", "BRANCH", "CONTACT_CENTRE"], [0.8, 0.1, 0.1])[0]}
                    out.append(self._event(kind, when, customer.customer_id, detail=detail,
                                           device=self.rng.choice(customer.devices) if customer.devices else None,
                                           channel="MOBILE_APP", region=customer.home_region))
                elif kind == "SIM_CHANGED":
                    out.append(self._event(kind, when, customer.customer_id,
                                           detail={"msisdn": _msisdn(customer.customer_id),
                                                   "carrier": self.rng.choice(["MTN", "AIRTEL", "GLO", "9MOBILE"])}))
                elif kind == "DEVICE_BOUND" and customer.devices:
                    # Reinstalling the app on the same phone binds it again.
                    out.append(self._event(kind, when, customer.customer_id, detail={"binding": "BOUND"},
                                           device=self.rng.choice(customer.devices), channel="MOBILE_APP",
                                           region=customer.home_region))
                elif kind == "LIMIT_CHANGED":
                    old = self.rng.choice([500_000, 1_000_000, 5_000_000]) * 100
                    out.append(self._event(kind, when, customer.customer_id, account_id=customer.primary.account_id,
                                           channel="MOBILE_APP", region=customer.home_region,
                                           detail={"limit": "DAILY_TRANSFER", "from_minor": old,
                                                   "to_minor": old * self.rng.choice([2, 3])}))
        return out


def _msisdn(customer_id: str) -> str:
    import hashlib

    return "2348" + str(int(hashlib.sha256(f"msisdn:{customer_id}".encode()).hexdigest(), 16))[:9]
