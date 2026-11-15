"""Parse dated product claims from the supplied Markdown snapshots."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlsplit

from contract import Source


DASHES = str.maketrans("–—−’‘", "---''")
PERCENT = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%")
MONEY = re.compile(r"\$\s*(\d[\d,]*(?:\.\d+)?)(?:\s*([km])\b)?(?![\d,.]\d)", re.I)
EFFECTIVE = re.compile(
    r"\beffective\s+(?:as of\s+|on\s+)?(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})",
    re.I,
)
MONTH_DATE = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}"
END = re.compile(
    rf"\b(?:offer|promotion|special|registration)\s+(?:has\s+)?(?:end(?:s|ed)?|expire[sd]?|is valid (?:until|through))"
    rf"\s*(?:on\s+)?({MONTH_DATE}|\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}/\d{{1,2}}/\d{{2,4}})",
    re.I,
)
LINK = re.compile(r"(?<!!)\[([^\[\]\n]+)\]\(([^)\s]+)\)")
TABLE = re.compile(r"^\|[^\n]+(?:\n\|[^\n]*)+", re.M)
DEPOSIT_NAMES = (
    (r"\bhigh[ -]+yield\b", "High-Yield Savings"),
    (r"\bachieve\b", "Achieve Money Market"),
    (r"\b(?:investor'?s choice|money market)\b", "Investor's Choice Money Market"),
    (r"\b(?:health savings|hsa)\b", "Health Savings Account"),
    (r"\blaunchpad\b", "Launchpad Savings"),
    (r"\bbright(?: online.?only)? savings\b", "Bright Savings"),
    (r"\byou[ -]+name[ -]+it\b", "You Name It Savings"),
    (r"\bholiday club\b", "Holiday Club Savings"),
    (r"\b(?:1st|first) rate checking\b", "1st Rate Checking"),
    (r"\bfree checking\b", "Free Checking"),
    (r"\b(?:regular|share|basic) savings\b", "Share Savings"),
)
LOAN_NAMES = (
    (r"\b(?:atv|snowmobile|jet ski|powersport)", "ATV loan"),
    (r"\b(?:boat|rv\b|recreational vehicle|camper)", "Boat and RV loan"),
    (r"\b(?:auto\b|car loan|vehicle loan|motorcycle)", "Auto loan"),
    (r"\b(?:heloc\b|home equity line)", "HELOC"),
    (r"\bhome equity loan", "Home equity loan"),
    (r"\bhome improvement loan", "Home improvement loan"),
    (r"\b(?:personal loan|signature loan|unsecured loan)", "Personal loan"),
)
WORD_NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "thirty-six": 36,
    "forty-eight": 48,
    "sixty": 60,
    "seventy-two": 72,
    "eighty-four": 84,
}
TERM = re.compile(
    rf"\b(\d{{1,3}}|{'|'.join(sorted(WORD_NUMBERS, key=len, reverse=True))})"
    r"\s*-?\s*(months?|mos?\.?|years?|yrs?\.?)\b",
    re.I,
)


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", value).translate(DASHES).casefold().split())


def plain(value):
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", value)
    return LINK.sub(r"\1", value).replace("**", "").replace("\\*", "").strip(" _")


def path_for(url):
    return urlsplit(url).path.rstrip("/").lower() or "/"


def parse_date(value):
    if not value:
        return None
    value = value.replace(",", "")
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def terms(text):
    return tuple(
        dict.fromkeys(
            (int(number) if number.isdigit() else WORD_NUMBERS[number.lower()])
            * (12 if unit.lower().startswith("y") else 1)
            for number, unit in TERM.findall(normalize(text))
        )
    )


def amounts(text):
    return [
        Decimal(number.replace(",", "")) * {"": 1, "k": 1000, "m": 1000000}[(unit or "").lower()]
        for number, unit in MONEY.findall(text)
    ]


def fmt_money(amount):
    return f"${amount:,.0f}" if amount == amount.to_integral_value() else f"${amount:,.2f}"


def pick_name(text, patterns):
    return next((name for pattern, name in patterns if re.search(pattern, text)), "")


@dataclass(frozen=True)
class Tier:
    kind: str
    low: Decimal | int | None
    high: Decimal | int | None
    label: str

    def contains(self, number):
        return (self.low is None or self.low <= number) and (self.high is None or number <= self.high)

    @property
    def bounds(self):
        return self.kind, self.low, self.high


@dataclass(frozen=True)
class Claim:
    product: str
    metric: str
    value: str
    source: Source
    authority: int
    effective: date | None = None
    expires: date | None = None
    term: int | None = None
    tier: Tier | None = None
    audience: str = "personal"
    variant: str = ""
    minimum: Decimal | None = None
    section: str = ""

    @property
    def scope(self):
        return self.product, self.audience, self.variant, self.term, self.metric

    @property
    def key(self):
        return (*self.scope, self.tier.bounds if self.tier else None)

    @property
    def rank(self):
        return self.authority, self.effective or date.min

    @property
    def comparable_value(self):
        numbers = PERCENT.findall(self.value)
        if numbers:
            return tuple(Decimal(number) for number in numbers)
        if self.metric == "fee" and amounts(self.value):
            return tuple(amounts(self.value))
        return normalize(self.value)


def balance_tier(label, kind="balance"):
    label = plain(label).strip()
    values = amounts(label)
    if len(values) == 1 and "-" in normalize(label):
        other = re.search(r"-\s*\$?\s*(\d[\d,]*(?:\.\d+)?)", normalize(label))
        if other:
            values.append(Decimal(other[1].replace(",", "")))
    if len(values) == 2:
        low, high = sorted(values)
        if kind == "loan amount" and high == high.to_integral_value() and high % 100 == 99:
            high += Decimal("0.99")
        return Tier(kind, low, high, label)
    if len(values) == 1 and ("+" in label or "over" in normalize(label)):
        return Tier(kind, values[0], None, label)
    if len(values) == 1 and re.search(r"less|up to|under", normalize(label)):
        upper = values[0] - Decimal("0.01") if "under" in normalize(label) else values[0]
        return Tier(kind, None, upper, f"{fmt_money(upper)} or less")
    return None


def vehicle_tier(text):
    match = re.search(r"\b(20\d\d)(?:\s*-\s*(\d{2,4}))?\b", normalize(text))
    if not match:
        return None
    year = int(match[1])
    if match[2]:
        other = int(match[2]) + (2000 if len(match[2]) == 2 else 0)
        low, high = sorted((year, other))
        return Tier("model year", low, high, f"{low}–{high} model years")
    if re.search(r"older|earlier", normalize(text)):
        return Tier("model year", None, year, f"{year} or older model years")
    return Tier("model year", year, year, f"{year} model year")


PRIMARY = {
    "Certificate": "/personal/savings-account",
    "IRA Certificate": "/personal/retirement",
    "IRA Savings": "/personal/retirement",
    **{name: "/personal/savings-account" for _, name in DEPOSIT_NAMES},
    "1st Rate Checking": "/personal/checking-accounts",
    "Free Checking": "/personal/checking-accounts",
    "Auto loan": "/personal/loans/auto-motorcycle-loans",
    "Personal loan": "/personal/loans/personal-loans",
    "HELOC": "/personal/loans/home-equity",
    "Home equity loan": "/personal/loans/home-equity",
    "Mortgage": "/personal/loans/home-loans",
    "Mortgage Rate Relief": "/personal/loans/home-loans",
    "Home improvement loan": "/personal/loans/home-loans",
    "Boat and RV loan": "/personal/loans/recreational-loans",
    "ATV loan": "/personal/loans/recreational-loans",
    "Platinum Visa": "/personal/credit-cards",
    "Rewards Visa": "/personal/credit-cards",
}


def authority(path, product, audience):
    if audience == "business":
        if product.endswith("Visa"):
            main = "/business/credit-cards"
        elif product.endswith("Checking"):
            main = "/business/checking-accounts"
        else:
            main = "/business/savings-accounts"
    else:
        main = PRIMARY.get(product)
    if path == main:
        return 100
    if path in ("/rates", "/business/rates") or path.startswith("/rates/"):
        return 96
    aliases = {
        "/personal/loans/auto-loans": {"Auto loan"},
        "/personal/loans/home-improvement-loan": {"Mortgage", "Home improvement loan"},
        "/personal/loans/home-equity": {"Mortgage"},
        "/personal/retirement": {"Launchpad Savings"},
        "/business/business-checking-accounts": {"1st Rate Checking", "Free Checking"},
    }
    if product in aliases.get(path, set()):
        return 88
    if (
        path.startswith(("/calculators/", "/learn/", "/newsroom/"))
        or path.endswith("-lp")
        or path == "/switch-and-earn"
    ):
        return 25
    return 55


def provenance(page, snapshot_id, quote, section=""):
    return Source(page.url.split("#", 1)[0] + section, snapshot_id, plain(quote)[:440])


def values_of(cell, allow_bare=False, pair=False):
    found = PERCENT.findall(cell)
    if found:
        return "–".join(number + "%" for number in found[: 2 if pair else 1])
    if allow_bare and (bare := re.match(r"\s*(\d+(?:\.\d+)?)(?:\s|$)", cell)):
        return bare[1] + "%"
    return ""


def _table_rows(markdown):
    end = 0
    for block in TABLE.finditer(markdown):
        before = [
            plain(line.lstrip("# "))
            for line in markdown[end : block.start()].splitlines()
            if line.strip() and not line.lstrip().startswith(("[", "![", "|"))
        ]
        after = markdown[block.end() : block.end() + 5000].split("\n|", 1)[0]
        lines = block.group().splitlines()
        columns = [list(map(str.strip, line.strip("|").split("|"))) for line in lines]
        if len(columns) >= 3 and all(re.fullmatch(r":?-{2,}:?", cell) for cell in columns[1]):
            when = EFFECTIVE.search(after)
            short_context = before[-8:]
            yield columns[0], columns[2:], lines[2:], short_context, parse_date(when[1]) if when else None
        end = block.end()


def _deposit(headers, row, context, audience):
    label = normalize(plain(row[0]))
    term_list = terms(label)
    section = normalize(" ".join(context[-2:]))
    minimum_col = next((i for i, h in enumerate(headers) if "min. to earn" in h), None)
    open_col = next((i for i, h in enumerate(headers) if "min. to open" in h), None)
    minimum = amounts(row[minimum_col]) if minimum_col is not None else []
    open_amount = amounts(row[open_col]) if open_col is not None else []
    variant = ""
    tier = None
    term = term_list[0] if term_list else None
    ira = re.match(r"(roth|traditional):", label)
    if term is not None:
        product = "IRA Certificate" if ira else "Certificate"
        variant = "jumbo special" if "jumbo" in label else "special" if "special" in label else "standard"
    elif ira:
        product = "IRA Savings"
        tier = balance_tier(row[0].split(":", 1)[1])
    else:
        product = pick_name(label, DEPOSIT_NAMES)
        if not product:
            product = pick_name(normalize(context[-1]), DEPOSIT_NAMES) if context else ""
        if not product and "saving account rates" in section:
            product = "Health Savings Account"
        if not product:
            return
        tier_col = (
            1 if "account type" in headers[0] and "balance" in headers[1] and "tier" in headers[1] else 0
        )
        tier = balance_tier(row[tier_col])
        if "min. balance" in headers and "max. balance" in headers:
            low = amounts(row[headers.index("min. balance")])
            high = amounts(row[headers.index("max. balance")])
            if low and high:
                tier = Tier("balance", low[0], high[0], f"{fmt_money(low[0])}–{fmt_money(high[0])}")
    if ira:
        variant = " ".join(filter(None, (ira[1], variant)))
    elif "business" in label:
        audience = "business"
    common = dict(
        product=product,
        term=term,
        variant=variant,
        tier=tier,
        audience=audience,
        minimum=minimum[0] if minimum else None,
    )
    for i, h in enumerate(headers):
        metric = "APY" if "yield" in h else "dividend rate" if "dividend rate" in h else ""
        if metric and (value := values_of(row[i])):
            yield dict(common, metric=metric, value=value)
    if open_amount:
        yield dict(common, metric="minimum to open", value=fmt_money(open_amount[0]))
    if minimum:
        yield dict(common, metric="minimum to earn", value=fmt_money(minimum[0]))


def _loan(headers, row, context, path):
    label = normalize(plain(row[0]))
    last = normalize(context[-1]) if context else ""
    terms_in_row = terms(" ".join(row[:2]))
    term = terms_in_row[0] if terms_in_row else None
    variant = ""
    tier = None
    if "points" in headers:
        if "relief program" in label:
            product = "Mortgage Rate Relief"
        elif label.startswith(("purchase", "refinance")):
            product = "Mortgage"
            variant = label.split(":", 1)[0]
            if variant == "refinance" and (
                "equity refinance" in last
                or "cash out" in last
                or "cash-out" in last
                or path == "/personal/loans/home-equity"
            ):
                variant = "cash-out refinance"
            tier = balance_tier(row[0], "loan amount")
        else:
            return
        for metric in ("rate", "APR", "points", "payment example"):
            column = next(
                (
                    i
                    for i, h in enumerate(headers)
                    if metric.lower() in h and (metric != "rate" or h == "rate")
                ),
                None,
            )
            if column is not None:
                value = values_of(row[column]) if metric in ("rate", "APR") else plain(row[column])
                if value:
                    yield dict(
                        product=product, metric=metric, value=value, term=term, tier=tier, variant=variant
                    )
        return

    column = next((i for i, h in enumerate(headers) if "apr" in h or "percentage rate" in h), None)
    card_label = " ".join((label, normalize(headers[0]), last))
    if ("platinum" in card_label or "rewards" in card_label) and re.search(
        r"\b(?:visa|credit card)\b", card_label
    ):
        product = "Platinum Visa" if "platinum" in card_label else "Rewards Visa"
        variant = "intro balance transfer" if "intro" in label else "standard"
        audience = "business" if "business" in card_label or path.startswith("/business/") else "personal"
        if column is not None:
            value = values_of(row[column], allow_bare=True, pair=variant == "standard")
            if value:
                yield dict(product=product, metric="APR", value=value, audience=audience, variant=variant)
        return

    if "model year" in label and term is not None:
        product = "ATV loan" if pick_name(last, LOAN_NAMES) == "ATV loan" else "Auto loan"
        tier = vehicle_tier(label)
        if column is None and len(row) == 3 and "apr" in normalize(row[2]):
            column = 2
    else:
        product = pick_name(label, LOAN_NAMES) or pick_name(last, LOAN_NAMES)
        if not product:
            product = next(
                (
                    name
                    for name, primary in PRIMARY.items()
                    if primary == path and name in ("Personal loan", "Auto loan")
                ),
                "",
            )
        if "intro" in label:
            variant = "intro"
        if product in ("Home improvement loan", "Boat and RV loan"):
            tier = balance_tier(row[0], "loan amount")
    if column is not None and product and (value := values_of(row[column], allow_bare=True)):
        yield dict(product=product, metric="APR", value=value, term=term, tier=tier, variant=variant)


def table_claims(page, snapshot_id):
    path = path_for(page.url)
    if (
        not path.startswith(("/personal/", "/business/", "/rates", "/calculators/", "/learn/"))
        and not path.endswith("-lp")
        and path != "/switch-and-earn"
    ):
        return
    audience = "business" if path.startswith("/business/") else "personal"
    for headers, rows, raw_rows, context, when in _table_rows(page.markdown):
        keys = [normalize(plain(col)) for col in headers]
        # Dates on offer pages apply only when the conditions actually give a calendar deadline.
        expiry = None
        if path.endswith("-lp"):
            end = END.search(page.markdown)
            if end:
                expiry = parse_date(end[1])
        for row, raw_row in zip(rows, raw_rows):
            if len(row) != len(keys):
                continue
            entries = (
                _deposit(keys, row, context, audience)
                if any("dividend rate" in h for h in keys)
                else _loan(keys, row, context, path)
            )
            for entry in entries:
                product = entry["product"]
                anchor = (
                    "#rates"
                    if path == PRIMARY.get(product)
                    and path in ("/personal/savings-account", "/personal/loans/home-loans")
                    else ""
                )
                yield Claim(
                    **entry,
                    effective=when,
                    expires=expiry,
                    section=context[-1] if context else "",
                    authority=authority(path, product, entry.get("audience", "personal")),
                    source=provenance(page, snapshot_id, raw_row, anchor),
                )


def prose_rates(page, snapshot_id):
    path = path_for(page.url)
    if path == "/personal/credit-cards":
        body = page.markdown.split("## Frequently Asked Questions", 1)[0]
        for match in re.finditer(r"^### (Platinum Visa|Rewards Visa)\s*\n(.*?)(?=^#|\Z)", body, re.M | re.S):
            for line in match[2].splitlines():
                vals = PERCENT.findall(plain(line))
                if not vals or not re.search(r"\bAPR\b", line):
                    continue
                intro = bool(
                    re.search(r"\b(?:intro\w*|new cardmember offer)\b", normalize(line))
                ) and "transfer" in normalize(line)
                variant = "intro balance transfer" if intro else "standard"
                value = values_of(plain(line), pair=not intro)
                if not intro and len(vals) != 2:
                    continue
                yield Claim(
                    match[1],
                    "APR",
                    value,
                    provenance(page, snapshot_id, line),
                    100,
                    variant=variant,
                    section="Personal credit cards",
                )
    if path.startswith(("/rates/", "/promo")):
        for line in page.markdown.splitlines():
            norm = normalize(plain(line))
            end = END.search(line)
            expiry = parse_date(end[1]) if end else None
            if re.search(r"\b(?:certificate|cd)\b", norm) and (term := terms(norm)) and "apy" in norm:
                value = values_of(norm)
                if value:
                    variant = (
                        "jumbo special"
                        if "jumbo" in norm
                        else "special"
                        if "special" in norm and not expiry
                        else "standard"
                    )
                    yield Claim(
                        "Certificate",
                        "APY",
                        value,
                        provenance(page, snapshot_id, line),
                        authority(path, "Certificate", "personal") if not path.startswith("/promo") else 25,
                        expires=expiry,
                        term=term[0],
                        variant=variant,
                    )
    if path == "/personal/loans/personal-loans":
        for line in page.markdown.splitlines():
            if "credit coach" in normalize(line) and "apr" in normalize(line) and (value := values_of(line)):
                yield Claim("Credit Coach loan", "APR", value, provenance(page, snapshot_id, line), 100)
