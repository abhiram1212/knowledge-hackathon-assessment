"""Offline credit-union knowledge system for the hackathon evaluator."""

from __future__ import annotations

import calendar
import re
from bisect import bisect_right
from collections import defaultdict
from datetime import date
from decimal import Decimal

from contract import Conflict
from evidence import (
    DEPOSIT_NAMES,
    LOAN_NAMES,
    MONTH_DATE,
    PERCENT,
    amounts,
    fmt_money,
    normalize,
    parse_date,
    path_for,
    pick_name,
    prose_rates,
    table_claims,
    terms,
)
from member_info import CARD_FEE, FEE_INTENT, MemberInfo, answer, fee_claims, fee_name, unknown


RATE_INTENT = re.compile(
    r"\b(?:apy|apr|interest|dividend|rates?|yield|earn(?:s|ing)?|pays?|points?|payments?|minimum|deposit|balance|"
    r"how much|highest|best|lowest|returns?)\b"
)
CERTIFICATE = re.compile(r"\b(?:certificates?|term[ -]share|cds?)\b")
CARD = re.compile(r"\b(?:credit cards?|visa|platinum card|rewards card)\b")
MONTH_NAMES = "|".join(calendar.month_name[1:])
DATE_CUE = r"\b(?:as of|on|in|during|back in|through|by)\s+"
MORTGAGE = re.compile(r"\b(?:mortgages?|home loans?|refinanc(?:e|ing)|(?:15|20|30)[ -]year fixed)\b")


class SnapshotView:
    def __init__(self, snapshot):
        self.claim_groups = defaultdict(list)
        self.services = MemberInfo(snapshot)
        seen = set()
        for page in snapshot.pages:
            sid = snapshot.snapshot_id
            for parser in (table_claims, prose_rates, fee_claims):
                for claim in parser(page, sid):
                    signature = (
                        claim.key,
                        claim.comparable_value,
                        claim.source.url,
                        claim.effective,
                        claim.expires,
                    )
                    if signature not in seen:
                        seen.add(signature)
                        self.claim_groups[claim.product].append(claim)
        for claims in self.claim_groups.values():
            claims.sort(key=lambda c: c.source.url)
            claims.sort(key=lambda c: c.rank, reverse=True)

    def active(self, product, day, **filters):
        return [
            claim
            for claim in self.claim_groups.get(product, ())
            if active_at(claim, day) and all(getattr(claim, key) == value for key, value in filters.items())
        ]


def active_at(claim, day):
    return (not claim.effective or claim.effective <= day) and (not claim.expires or day <= claim.expires)


def main_rows(claims):
    """Keep the best complete table, excluding outdated tiers missing from that table."""
    representatives = {}
    for claim in claims:
        dimension = claim.variant
        if dimension not in representatives:
            representatives[dimension] = claim
    selected = {}
    for claim in claims:
        reference = representatives[claim.variant]
        if claim.source.url == reference.source.url:
            selected.setdefault(claim.key, claim)
    return list(selected.values())


def row_order(claim):
    return (
        claim.term or 0,
        claim.tier.low or 0 if claim.tier else 0,
        claim.tier.high or Decimal("Infinity") if claim.tier else Decimal("Infinity"),
        claim.variant,
    )


def claim_title(claim):
    if claim.metric == "fee":
        product = (
            "Business " if claim.audience == "business" and not claim.product.startswith("Business ") else ""
        ) + claim.product
        return f"{product} {claim.variant or 'fee'}"
    label = ("Business " if claim.audience == "business" else "") + claim.product
    if claim.product.startswith("IRA"):
        label = label.replace("IRA", claim.variant.split()[0].title() + " IRA")
    if claim.term is not None:
        term = f"{claim.term // 12}-year" if claim.product == "Mortgage" else f"{claim.term}-month"
        label = f"{term} {label}"
    variant = re.sub(r"^(?:roth|traditional)\s*", "", claim.variant)
    if variant and variant != "standard":
        label += f" ({variant})"
    if claim.tier:
        label += f" for {claim.tier.label}"
    return label


def disagreement_reason(winner, other, day):
    if other.expires and other.expires < day:
        return "The competing promotion expired. The active product listing supplies the current value."
    if winner.authority > other.authority:
        return "The product page or official schedule takes precedence over a copied, promotional, or calculator listing."
    if winner.effective and other.effective and winner.effective > other.effective:
        return "The selected listing has a later stated effective date."
    if winner.source.snapshot_id != other.source.snapshot_id:
        return "The newer snapshot supplies the current value. The older capture remains available for historical queries."
    return "The selected value appears in the preferred product schedule for this tier."


def overlaps(left, right):
    if left.key == right.key:
        return True
    # Compare only intersecting model-year bands within the same loan term. Other
    # endpoints are explicitly published balance bands or mortgage amount caps.
    if not left.tier or not right.tier or left.tier.kind != "model year" or right.tier.kind != "model year":
        return False
    return (left.tier.high is None or right.tier.low is None or right.tier.low <= left.tier.high) and (
        right.tier.high is None or left.tier.low is None or left.tier.low <= right.tier.high
    )


def percentage_metric(q, default):
    if re.search(r"\b(?:dividend rate|nominal rate)\b", q):
        return "dividend rate"
    if re.search(r"\b(?:apy|annual percentage yield|yield)\b", re.sub(r"\bhigh[ -]yield\b", "", q)):
        return "APY"
    if re.search(
        r"\b(?:minimum|cost|how much|initial deposit)\b.{0,35}\b(?:open|opening|start)\b|\b(?:open|opening)\b.{0,20}\bminimum\b",
        q,
    ):
        return "minimum to open"
    if re.search(r"\bminimum\b|\bhow much.{0,30}(?:open|earn)\b", q):
        return "minimum to earn"
    return default


def query_amounts(q):
    cleaned = re.sub(r"\b(?:usd|us dollars?)\s*", "$", q)
    result = amounts(cleaned)
    if result:
        # A table question can write "$10,000 to 24,999" with one dollar sign.
        end = re.search(r"\$[\d,.]+\s*(?:-|to|and)\s*(\d[\d,]*(?:\.\d+)?)\b", cleaned)
        if end and len(result) == 1:
            result.append(Decimal(end[1].replace(",", "")))
        return result
    match = re.search(
        r"\b(?:balance|borrow|loan (?:size|amount)|deposit|with|of)\s*(?:of|is|about|around|at)?\s*"
        r"(\d[\d,]*(?:\.\d+)?)\s*(k|m|dollars)\b",
        q,
    )
    if match:
        return amounts("$" + match[1] + ("" if match[2] == "dollars" else match[2]))
    return []


def question_date(question):
    full = re.search(
        DATE_CUE + rf"({MONTH_DATE}|\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}/\d{{1,2}}/\d{{4}})", question, re.I
    )
    if full:
        return parse_date(full[1].title())
    month = re.search(DATE_CUE + rf"({MONTH_NAMES})\s+(20\d{{2}})\b", question, re.I)
    if month:
        number = list(calendar.month_name).index(month[1].title())
        year = int(month[2])
        return date(year, number, calendar.monthrange(year, number)[1])
    iso_month = re.search(DATE_CUE + r"(20\d{2})-(0[1-9]|1[0-2])(?![-\d])", question, re.I)
    if iso_month:
        year, month = map(int, iso_month.groups())
        return date(year, month, calendar.monthrange(year, month)[1])
    return None


class Knowledge:
    def __init__(self):
        self._dates = []
        self._views = []

    def ingest(self, snapshot):
        day = date.fromisoformat(snapshot.taken_on)
        view = SnapshotView(snapshot)
        slot = bisect_right(self._dates, day)
        if slot and self._dates[slot - 1] == day:
            self._views[slot - 1] = view
        else:
            self._dates.insert(slot, day)
            self._views.insert(slot, view)

    def answer(self, question, as_of=None):
        if not self._dates or not question.strip():
            return unknown()
        day = parse_date(as_of) if as_of is not None else question_date(question)
        if as_of is not None and day is None:
            return unknown()
        day = day or self._dates[-1]
        slot = bisect_right(self._dates, day)
        if slot == 0:
            return unknown()
        view = self._views[slot - 1]
        q = normalize(question)
        audience = "business" if re.search(r"\b(?:business|commercial|company)\b", q) else "personal"
        result = view.services.specific(q, day)
        if result is not None:
            return result
        if re.search(FEE_INTENT, q):
            result = self._fee(q, day, view, audience, self._views[: slot - 1])
            if result is not None:
                return result
        result = self._product_answer(q, day, view, audience)
        if result is not None:
            return result
        if RATE_INTENT.search(q) and re.search(
            r"\b(?:loan|mortgage|credit card|certificate|checking|savings|arm)\b", q
        ):
            return unknown()
        return view.services.find_faq(q) or unknown()

    def conflicts(self):
        if not self._views:
            return []
        latest = self._dates[-1]
        observations = defaultdict(list)
        last_winners = {}
        current = {}
        excluded = {"minimum to earn", "minimum to open", "payment example", "points"}
        for day, view in zip(self._dates, self._views):
            current = {}
            for rows in view.claim_groups.values():
                by_table = defaultdict(list)
                for claim in rows:
                    if claim.metric in excluded or claim.effective and claim.effective > day:
                        continue
                    observations[claim.scope].append((day, claim))
                    if active_at(claim, day):
                        by_table[claim.audience, claim.metric].append(claim)
                for claims in by_table.values():
                    for claim in main_rows(claims):
                        current[claim.key] = claim
            last_winners.update(current)

        report = self._views[-1].services.conflicts(latest)
        current_by_scope = defaultdict(list)
        for claim in current.values():
            current_by_scope[claim.scope].append(claim)
        for key, winner in last_winners.items():
            is_current = key in current
            if not is_current and any(overlaps(winner, other) for other in current_by_scope[winner.scope]):
                continue
            observed = sorted(
                observations[winner.scope], key=lambda pair: (pair[0], pair[1].rank), reverse=True
            )
            competing = {}
            for _, other in observed:
                if other.comparable_value != winner.comparable_value and overlaps(winner, other):
                    competing.setdefault(other.comparable_value, other)
            if not competing:
                continue
            examples = [winner, *competing.values()]
            topic = claim_title(winner)
            if winner.metric != "fee":
                topic += f" {winner.metric}"
            reason = (
                disagreement_reason(winner, examples[1], latest)
                if is_current
                else "Earlier listings disagree. The latest capture has no eligible listing for this tier."
            )
            report.append(
                Conflict(
                    topic,
                    [c.value for c in examples],
                    [c.source for c in examples],
                    winner.value if is_current else None,
                    reason,
                )
            )
        return sorted(report, key=lambda c: c.topic)

    def _respond(self, view, day, lines, selected):
        if not selected:
            return unknown()
        sources = [claim.source for claim in selected]
        conflict_note = None
        if len(selected) <= 2:
            for winner in selected:
                other = next(
                    (
                        claim
                        for claim in view.claim_groups.get(winner.product, ())
                        if claim.scope == winner.scope
                        and overlaps(winner, claim)
                        and claim.comparable_value != winner.comparable_value
                        and (not claim.effective or claim.effective <= day)
                    ),
                    None,
                )
                if other:
                    conflict_note = disagreement_reason(winner, other, day)
                    break
        result = answer("\n".join(lines), *sources)
        result.conflict_note = conflict_note
        return result

    def _fee(self, q, day, view, audience, earlier):
        if CARD.search(q) and "debit card" not in q and (card_fee := CARD_FEE.search(q)):
            products = self._card_products(q)
            claims = [
                row
                for product in products
                for row in view.active(product, day, audience=audience, metric="fee", variant=card_fee[0])
            ]
            rows = list({row.key: row for row in reversed(claims)}.values())
            return self._respond(view, day, [f"{claim_title(row)}: {row.value}." for row in rows], rows)
        name = fee_name(q)
        if not name and "debit card" in q and "foreign transaction fee" in q:
            name = "Debit card foreign transaction"
        if (
            not name
            and re.search(r"\b(?:monthly|fee|cost|charge)\w*\b", q)
            and pick_name(q, DEPOSIT_NAMES) == "1st Rate Checking"
        ):
            name = "1st Rate Checking"
        if not name and re.search(r"\bwir(?:e|es|ing)\b", q):
            names = ("Incoming wire transfer", "Domestic wire transfer", "International wire transfer")
            fees = [
                rows[0] for item in names if (rows := view.active(item, day, metric="fee", audience=audience))
            ]
            return self._respond(view, day, [f"{row.product}: {row.value}." for row in fees], fees)
        if not name:
            return None
        fee_audience = "business" if name == "Business sweeps" else audience
        rows = view.active(name, day, audience=fee_audience, metric="fee")
        if not rows and not any(
            path.startswith("/fee") or "fee-schedule" in path for path in view.services.pages
        ):
            for prior in reversed(earlier):
                rows = prior.active(name, day, audience=fee_audience, metric="fee")
                if rows:
                    break
        if not rows:
            return unknown()
        row = rows[0]
        if name in ("Coin counting", "Non-Dupaco ATM use"):
            text = row.value.rstrip(".") + "."
        else:
            text = f"{claim_title(row)}: {row.value.rstrip('.')}."
        if name == "1st Rate Checking":
            text = row.source.quote.rstrip(".") + "."
        if name == "Overdraft":
            page = view.services.pages.get(path_for(row.source.url))
            if page:
                limit = re.search(
                    r"You will not be charged[^\n]*?Maximum charge of one fee per day\.", page.markdown, re.I
                )
                if limit:
                    text += " " + limit[0]
        result = self._respond(view, day, [text], [row])
        return result

    def _card_products(self, q):
        if "platinum" in q:
            return ["Platinum Visa"]
        if "reward" in q:
            return ["Rewards Visa"]
        return ["Platinum Visa", "Rewards Visa"]

    def _product_answer(self, q, day, view, audience):
        if CERTIFICATE.search(q) and re.search(
            r"\b(?:url|link|website|page|where (?:can|do) i (?:find|see))\b", q
        ):
            page = view.services.pages.get(
                "/business/savings-accounts" if audience == "business" else "/personal/savings-account"
            )
            if page:
                matches = view.active("Certificate", day, audience=audience, metric="APY")
                src = next(
                    (row.source for row in matches if path_for(row.source.url) == path_for(page.url)), None
                )
                if src:
                    return answer(f"The certificate rate table is at {src.url}.", src)
        if CARD.search(q) and (
            RATE_INTENT.search(q) or re.search(r"\b(?:what|which|offer|types|options)\b", q)
        ):
            if re.search(FEE_INTENT, q) and not re.search(r"\b(?:apr|rate|interest|rate range)\b", q):
                return None
            return self._cards(q, day, view, audience)
        if not RATE_INTENT.search(q):
            return None
        if re.search(
            r"\b(?:construction loan|adjustable[ -]rate|\d+\s*/\s*\d+|arm\b|jumbo mortgage|va loan|fha loan|student loan)",
            q,
        ):
            return unknown()
        if CERTIFICATE.search(q) or (re.search(r"\b(?:roth|traditional|ira)\b", q) and terms(q)):
            return self._certificates(q, day, view, audience)
        product = pick_name(q, DEPOSIT_NAMES)
        if product or re.search(r"\b(?:savings|checking)\b", q) and not re.search(r"\bloan\b", q):
            return self._deposits(q, day, view, audience, product)
        if "credit coach" in q:
            return self._loans(q, day, view, audience, "Credit Coach loan")
        product = pick_name(q, LOAN_NAMES)
        if (
            not product
            and re.search(r"\b(?:car|truck|vehicle)\b", q)
            and re.search(r"\b(?:loan|financ\w*|apr)\b", q)
        ):
            product = "Auto loan"
        if product:
            return self._loans(q, day, view, audience, product)
        if "mortgage rate relief" in q:
            return self._mortgage(q, day, view, audience, "Mortgage Rate Relief")
        if MORTGAGE.search(q):
            return self._mortgage(q, day, view, audience, "Mortgage")
        return None

    def _certificates(self, q, day, view, audience):
        product = "IRA Certificate" if re.search(r"\b(?:ira|roth|traditional)\b", q) else "Certificate"
        requested_metric = percentage_metric(q, "APY")
        metric = "minimum to earn" if requested_metric == "minimum to open" else requested_metric
        selected = main_rows(view.active(product, day, audience=audience, metric=metric))
        term_list = terms(q)
        dollars = query_amounts(q)
        jumbo = "jumbo" in q
        standard = bool(re.search(r"\b(?:standard|regular|non[ -]jumbo)\b", q))
        special = bool(re.search(r"\b(?:special|promo\w*)\b", q))
        filtered = []
        for row in selected:
            if term_list and row.term not in term_list:
                continue
            if (
                product.startswith("IRA")
                and any(x in q for x in ("roth", "traditional"))
                and row.variant.split()[0] not in q
            ):
                continue
            if jumbo and not standard and "jumbo" not in row.variant:
                continue
            if (standard or special) and special != row.variant.endswith("special"):
                continue
            if dollars and row.minimum is not None and row.minimum > dollars[0]:
                continue
            filtered.append(row)
        if dollars and not jumbo:
            for term in {r.term for r in filtered}:
                if any(r.term == term and "jumbo" in r.variant for r in filtered):
                    filtered = [r for r in filtered if r.term != term or "jumbo" in r.variant]
        if term_list and not special and not jumbo and any(r.variant.endswith("standard") for r in filtered):
            explicit_terms = {r.term for r in filtered if r.variant.endswith("standard")}
            filtered = [r for r in filtered if r.term not in explicit_terms or r.variant.endswith("standard")]
        if re.search(r"\b(?:highest|best|maximum)\b", q) and metric in ("APY", "dividend rate") and filtered:
            max_value = max(r.comparable_value for r in filtered)
            filtered = [r for r in filtered if r.comparable_value == max_value]
        if term_list and any(term not in {row.term for row in filtered} for term in term_list):
            return unknown()
        filtered.sort(key=row_order)
        lines = []
        for row in filtered:
            label = claim_title(row)
            if metric.startswith("minimum"):
                lines.append(f"{label} requires {row.value} minimum to earn.")
            else:
                minimum = (
                    f" with a {fmt_money(row.minimum)} minimum to earn" if row.minimum is not None else ""
                )
                lines.append(f"{label} pays {row.value} {metric}{minimum}.")
        return self._respond(view, day, lines, filtered)

    def _deposits(self, q, day, view, audience, product):
        if not product and re.search(r"\b(?:roth|traditional|ira)\b", q):
            product = "IRA Savings"
        if not product and re.search(r"\b(?:club|jump start|prime|student|youth) (?:checking|savings)\b", q):
            return unknown()
        product_names = (
            (product,)
            if product
            else ("1st Rate Checking", "Free Checking")
            if "checking" in q
            else ("Share Savings",)
        )
        metric = percentage_metric(q, "APY")
        dollars = query_amounts(q)
        query_rates = [Decimal(number) for number in PERCENT.findall(q)]
        selected = []
        for name in product_names:
            rows = main_rows(view.active(name, day, audience=audience, metric=metric))
            if name == "IRA Savings" and re.search(r"\b(?:roth|traditional)\b", q):
                rows = [r for r in rows if r.variant in q]
            if dollars:
                rows = [
                    row
                    for row in rows
                    if not row.tier or all(row.tier.contains(amount) for amount in dollars)
                ]
            selected.extend(rows)
        if not selected:
            return unknown()
        if metric == "minimum to open":
            unique = {}
            for row in selected:
                unique.setdefault((row.product, row.value), row)
            if len(unique) == len({row.product for row in selected}):
                deduped = list(unique.values())
                lines = [
                    f"{'Business ' if row.audience == 'business' else ''}{row.product} requires {row.value} to open."
                    for row in deduped
                ]
                return self._respond(view, day, lines, deduped)
        if re.search(r"\b(?:highest|best|top|maximum|max)\b", q) or query_rates:
            if all(isinstance(row.comparable_value, tuple) for row in selected):
                max_value = max(row.comparable_value for row in selected)
                matching = [row for row in selected if row.comparable_value[0] in query_rates]
                selected = matching or [row for row in selected if row.comparable_value == max_value]
        lines = []
        selected.sort(key=row_order)
        if query_rates and all(isinstance(row.comparable_value, tuple) for row in selected):
            positive = any(row.comparable_value[0] in query_rates for row in selected)
            lines.append(
                "Yes." if positive else f"No published {', '.join(product_names)} tier matches that rate."
            )
        for row in selected:
            value = f"{row.value} {metric}"
            lines.append(
                f"{claim_title(row)} pays {value}." if "%" in value else f"{claim_title(row)}: {value}."
            )
        return self._respond(view, day, lines, selected)

    def _cards(self, q, day, view, audience):
        products = self._card_products(q)
        audiences = [audience]
        if ("business" in q and "personal" in q) or (
            len(products) == 2 and not re.search(r"\b(?:business|commercial|personal|consumer)\b", q)
        ):
            audiences = ["personal", "business"]
        if re.search(r"\b(?:cash advance|penalty|late payment)\b", q):
            return unknown()
        intro = bool(re.search(r"\b(?:intro\w*|balance transfers?)\b", q)) and not re.search(
            r"\b(?:purchase|standard|ongoing|regular)\b", q
        )
        variant = "intro balance transfer" if intro else "standard"
        rows = []
        for kind in audiences:
            for product in products:
                rows.extend(
                    main_rows(view.active(product, day, metric="APR", audience=kind, variant=variant))
                )
        return self._respond(
            view, day, [f"{claim_title(row)} has a published {row.value} APR." for row in rows], rows
        )

    def _loans(self, q, day, view, audience, product):
        if audience == "business":
            return unknown()
        if product == "HELOC" and re.search(r"\b(?:maximum|max|cap|minimum|floor)\b", q):
            page = view.services.pages.get("/personal/loans/home-equity")
            target = (
                r"maximum APR\s+(\d+(?:\.\d+)?%)"
                if re.search(r"\b(?:maximum|max|cap)\b", q)
                else r"floor rate as low as (\d+(?:\.\d+)?%)"
            )
            if page and (match := re.search(target, page.markdown, re.I)):
                return answer(f"The listing states {match[0]}.", view.services._source(page, match[0]))
            return unknown()
        variant = "intro" if product == "HELOC" and re.search(r"\b(?:intro\w*|promo\w*)\b", q) else ""
        if product == "Auto loan" and re.search(r"\bmini[ -]?max\b", q):
            # The conventional table may be used only when the product's published
            # Mini-Max adjustment is present in the selected capture.
            page = view.services.pages.get("/personal/loans/auto-motorcycle-loans")
            adjustment = (
                re.search(r"Mini-Max Loans: Add (\d+(?:\.\d+)?)%", page.markdown, re.I) if page else None
            )
            if not adjustment:
                return unknown()
        else:
            adjustment = None
        rows = main_rows(view.active(product, day, metric="APR", variant=variant))
        term_list = terms(q)
        year = None
        if product in ("Auto loan", "ATV loan"):
            q_without_date = re.sub(
                DATE_CUE
                + rf"(?:{MONTH_DATE.lower()}|(?:{MONTH_NAMES.lower()})\s+20\d{{2}}|20\d{{2}}-\d{{2}}(?:-\d{{2}})?)",
                " ",
                q,
            )
            year_match = re.search(r"\b(19\d{2}|20\d{2})\b", q_without_date)
            if year_match:
                year = int(year_match[1])
            elif re.search(r"\bnew\b", q) and rows:
                years = [r.tier.high for r in rows if r.tier and r.tier.high is not None]
                year = max(years) if years else None
        dollars = query_amounts(q)
        selected = [
            r
            for r in rows
            if (not term_list or r.term in term_list)
            and (year is None or r.tier and r.tier.contains(year))
            and (not dollars or not r.tier or r.tier.kind != "loan amount" or r.tier.contains(dollars[0]))
        ]
        if term_list and any(term not in {r.term for r in selected} for term in term_list):
            return unknown()
        if re.search(r"\b(?:lowest|best)\b", q) and selected:
            best = min(r.comparable_value for r in selected)
            selected = [r for r in selected if r.comparable_value == best]
        lines = []
        for row in sorted(selected, key=row_order):
            if adjustment:
                value = Decimal(PERCENT.search(row.value)[1]) + Decimal(adjustment[1])
                lines.append(f"{claim_title(row)} through Mini-Max starts at {value:.2f}% APR.")
            else:
                lines.append(f"{claim_title(row)} starts at {row.value} APR, subject to qualification.")
        result = self._respond(view, day, lines, selected)
        if adjustment and selected:
            result.sources.append(view.services._source(page, adjustment[0]))
        return result

    def _mortgage(self, q, day, view, audience, product):
        if audience == "business":
            return unknown()
        variant = (
            "cash-out refinance"
            if re.search(r"\bcash[ -]out\b", q)
            else "refinance"
            if "refinanc" in q
            else "purchase"
        )
        if product == "Mortgage Rate Relief":
            variant = ""
        explicit_apr = re.search(r"\b(?:apr|annual percentage rate)\b", q)
        metric = (
            "points"
            if re.search(r"\bpoints\b", q)
            else "payment example"
            if re.search(r"\b(?:monthly|example) payment\b", q)
            else "APR"
            if explicit_apr
            else "rate"
        )
        rows = main_rows(view.active(product, day, metric=metric, variant=variant))
        dollars = query_amounts(q)
        term_list = terms(q)
        selected = [r for r in rows if not term_list or r.term in term_list]
        if dollars:
            selected = [r for r in selected if not r.tier or r.tier.contains(dollars[0])]
        if term_list and any(term not in {r.term for r in selected} for term in term_list):
            return unknown()
        selected.sort(key=row_order, reverse=True)
        if dollars or not term_list:
            by_term = defaultdict(list)
            for row in reversed(selected):
                by_term[row.term].append(row)
            selected = [by_term[term][0] for term in sorted(by_term, key=lambda v: v or 0, reverse=True)]
        else:
            # Show every published amount cap when the member specified the term only.
            selected.sort(key=row_order)
        label = {
            "rate": "interest rate",
            "APR": "APR",
            "points": "points",
            "payment example": "example principal and interest payment",
        }[metric]
        lines = [f"{claim_title(row)}: {row.value} {label}." for row in selected]
        return self._respond(view, day, lines, selected)


def build():
    return Knowledge()
