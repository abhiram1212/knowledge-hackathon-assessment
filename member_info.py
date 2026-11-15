"""Extract member-service evidence and answer queries outside the rate tables."""

from __future__ import annotations

import re
from html import unescape
from datetime import date
from typing import NamedTuple

from contract import Answer, Conflict, Source
from evidence import (
    Claim,
    EFFECTIVE,
    LINK,
    MONTH_DATE,
    amounts,
    normalize,
    parse_date,
    path_for,
    plain,
    provenance,
)

BRANCH_ADDRESS = re.compile(
    r"\bDupaco Branch\s*\n\s*(\d[^\n]+?,\s*([^,\n]+),\s*(?:IA|IL|WI),?\s*\d{5}(?:-\d{4})?)"
)
HOURS = re.compile(
    r"(?im)^\*\*(Lobby|Drive[ -]up)\*\*((?:\s*(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)[^\n]+\n?)+)"
)
BRANCH_ACTION = re.compile(rf"\b(?:will close|closing on|scheduled to close)\s+({MONTH_DATE})", re.I)
BRANCH_COUNT = re.compile(r"\b(\d+) (?:branch office locations|full-service branches)\b")
NEWS_DATE = re.compile(rf"(?m)^({MONTH_DATE})\s*$")
PHONE = re.compile(r"\b\d{3}-\d{3}-\d{4}\b")
SEPARATOR = re.compile(r"(?im)^## (?:Handpicked for you|Related (?:articles|posts)|Explore more)\b")
CARD_FEE = re.compile(
    r"\b(?:annual fee|foreign transaction fee|cash advance fee|late payment fee|returned payment fee|balance transfer fee)\b",
    re.I,
)
FEE_ALIASES = (
    (
        r"\b(?:replacement|replace|lost|stolen).{0,40}(?:debit|atm) card|\b(?:debit|atm) card.{0,40}(?:replacement|replace|lost|stolen)",
        "Debit card replacement",
    ),
    (r"\brush.{0,25}debit card|\bdebit card.{0,25}(?:rush|expedit)", "Debit card rush delivery"),
    (r"\bstop[ -]payment\b", "Stop payment"),
    (r"\boverdraft\b", "Overdraft"),
    (r"\b(?:incoming|receive|receiving|inbound).{0,35}\bwir", "Incoming wire transfer"),
    (r"\b(?:international|foreign|overseas).{0,25}\bwir", "International wire transfer"),
    (r"\b(?:domestic|outgoing|send|sending).{0,25}\bwir", "Domestic wire transfer"),
    (r"\b(?:foreign currency|currency exchange)\b", "Foreign currency exchange"),
    (r"\bmoney orders?\b", "Money order"),
    (r"\bcertified draft\b", "Certified draft"),
    (r"\b(?:official|cashier'?s?) check\b", "Official check"),
    (r"\b(?:non[ -]dupaco|out[ -]of[ -]network|other).{0,20}\batm\b", "Non-Dupaco ATM use"),
    (r"\b(?:coin counting|count coins)\b", "Coin counting"),
    (r"\bnotar(?:y|ize|ization)\b", "Notary services"),
    (r"\b(?:non.?member).{0,25}\bcheck[ -]cash|\bcheck[ -]cash", "Check cashing"),
    (r"\b(?:re[ -]?key|lost key)\b", "Safe-deposit box re-key"),
    (r"\bsweep", "Business sweeps"),
    (r"\b(?:garnishment|levy|child support|document copy|account research)\b", ""),
)
FEE_INTENT = r"\b(?:fees?|charge[sd]?|costs?|how much|price|free)\b"
BRANCH_MARKER = r"\b(?:branch(?:es)?|location|office|lobby|drive[ -]?(?:up|thru|through))\b"
COMMON = set(
    """
a an the as of can could what which how when where why who is are was were do does did for and or about it me
my your you our their they to in with on from by i we want like would please tell give know have any get
dupaco credit union over account accounts service services use using should will
""".split()
)


class Branch(NamedTuple):
    aliases: tuple[str, ...]
    city: str
    address: str
    source: Source


class Schedule(NamedTuple):
    branch: Branch
    service: str
    hours: str
    published: date | None


class Article(NamedTuple):
    page: object
    text: str
    published: date | None


class FAQ(NamedTuple):
    prompt: str
    response: str
    source: Source
    tokens: frozenset[str]
    path: str


def tokens(text):
    result = re.findall(r"[a-z][a-z0-9]+", normalize(text))
    return frozenset(word.removesuffix("s") for word in result if word not in COMMON)


def answer(text, *sources):
    by_url = {}
    for source in sources:
        by_url.setdefault(source.url, source)
    return Answer(text, list(by_url.values()))


def unknown(source=None):
    return Answer(
        "I don't know. The available snapshots don't give enough information to confirm this.",
        [source] if source else [],
        confident=False,
    )


def article(page):
    body = SEPARATOR.split(page.markdown, maxsplit=1)[0]
    date_match = NEWS_DATE.search(body[:425])
    published = parse_date(date_match[1]) if date_match else None
    return Article(page, body, published)


def fee_name(text):
    for pattern, label in FEE_ALIASES:
        match = re.search(pattern, text)
        if match:
            return label or match[0].capitalize()
    return ""


def fee_claims(page, sid):
    path = path_for(page.url)
    if path.startswith("/fee") or "fee-schedule" in path:
        effective = EFFECTIVE.search(page.markdown)
        when = parse_date(effective[1]) if effective else None
        section = ""
        carry = ""
        for raw in page.markdown.splitlines():
            line = plain(unescape(unescape(raw))).strip("|").replace("|", " ").strip()
            if not line:
                carry = ""
                continue
            if re.match(r"^(?:Deposit Services|Debit Card|Teller and Cash|Other Service)", line):
                section = normalize(line)
            label = normalize(line)
            name = fee_name(label)
            if not name and "debit card service" in section and label.startswith("foreign transaction fee"):
                name = "Debit card foreign transaction"
            if name and not amounts(line) and "%" not in line and "free" not in normalize(line):
                carry = line
            if not name and carry and amounts(line):
                line = carry + " " + line
                name = fee_name(normalize(line))
            if not name or not (amounts(line) or "%" in line or "free" in normalize(line)):
                continue
            carry = ""
            audience = "business" if "business" in section or name == "Business sweeps" else "personal"
            variant = ""
            if name == "Overdraft" and "paid items" not in label:
                continue
            if name == "Stop payment" and "we will charge" in label:
                continue
            fee = re.search(
                r"\$\s*\d[\d,]*(?:\.\d+)?\**(?:\s*(?:per [a-z]+(?:/[a-z]+)?|each)\b)?", line, re.I
            )
            percentage = re.search(r"\b\d+(?:\.\d+)?%\s+of (?:the )?amount", line, re.I)
            value = fee[0].replace("*", "") if fee else percentage[0] if percentage else "Free"
            if name == "Overdraft":
                value = value.rstrip(".")
            if name in ("Coin counting", "Non-Dupaco ATM use"):
                value = line
            yield Claim(
                name,
                "fee",
                value.strip(),
                provenance(page, sid, line),
                105,
                when,
                audience=audience,
                variant=variant,
            )
    if path == "/personal/checking-accounts":
        for line in page.markdown.splitlines():
            if (
                "avoid a" in normalize(line)
                and "monthly fee" in normalize(line)
                and (dollars := amounts(line))
            ):
                yield Claim(
                    "1st Rate Checking",
                    "fee",
                    f"${dollars[0]:.2f} per month",
                    provenance(page, sid, line),
                    100,
                    variant="monthly",
                )
    if path in ("/personal/credit-cards", "/business/credit-cards"):
        for raw in page.markdown.splitlines():
            line = plain(raw)
            if len(line) > 200 and (
                card := re.search(
                    r"(?<![A-Za-z])(Platinum Visa|(?:Visa )?Rewards(?: Visa)?)\s+Rate", line, re.I
                )
            ):
                audience = "business" if path.startswith("/business") else "personal"
                for match in CARD_FEE.finditer(line):
                    rest = line[match.end() :]
                    segment = re.split(
                        r";|\.(?:\s|$)|\band no\b|\bother fees include\b", rest, maxsplit=1, flags=re.I
                    )[0]
                    previous = normalize(line[: match.start()])
                    free = bool(re.search(r"\bno\s*$", previous))
                    if free or amounts(segment) or "%" in segment:
                        value = "No fee" if free else segment.strip(" :—–-")
                        product = "Platinum Visa" if "platinum" in normalize(card[1]) else "Rewards Visa"
                        yield Claim(
                            product,
                            "fee",
                            value,
                            provenance(page, sid, line),
                            100,
                            audience=audience,
                            variant=normalize(match[0]),
                        )


def _aliases(city, address):
    street = address.split(",", 1)[0]
    clean = normalize(re.sub(r"^\d+\s+", "", street)).strip(".")
    short = re.sub(r"\s+(?:avenue|ave|street|st|road|rd|boulevard|blvd|drive|dr|lane|ln)\.?$", "", clean)
    return tuple(dict.fromkeys((normalize(city), clean, short)))


def _branch_articles(articles, sid):
    for item in articles:
        town = re.search(r"(?m)^([A-Z][A-Z .'-]{2,30}),\s*(?:Iowa|Wis\.?|Wisconsin|Illinois)\b", item.text)
        address = re.search(
            r"\b(?:located at|at its new location at)\s+(\d[^.!\n]{3,70}?(?:Blvd|Road|Avenue|Ave|St|Street|Rd|Drive|Dr)\b)",
            item.text,
            re.I,
        )
        if not town:
            continue
        city = town[1].title()
        address_text = address[1] if address else ""
        if not address_text:
            fallback_address = re.search(r"\blocated at\s+(\d[^,\n]{3,60}),", item.text, re.I)
            if fallback_address:
                address_text = fallback_address[1]
        if not address_text:
            continue
        aliases = _aliases(city, address_text)
        nearby = re.search(
            r"intersection of .*? and ([A-Z][A-Za-z]+(?: [A-Za-z]+)?) (?:Road|Street)\b", item.text
        )
        if nearby:
            aliases += (normalize(nearby[1]),)
        for match in HOURS.finditer(item.text):
            hours = "; ".join(line.strip() for line in match[2].strip().splitlines() if line.strip())
            branch = Branch(aliases, city, address_text, provenance(item.page, sid, match[0]))
            yield Schedule(branch, normalize(match[1]).replace(" ", "-"), hours, item.published)


def _extract_faq(page, sid):
    path = path_for(page.url)
    if not path.startswith(
        ("/personal/", "/business/", "/online-and-mobile-banking/", "/about/")
    ) and path not in ("/shine", "/contact"):
        return
    chunks = re.split(
        r"(?m)^- \[([^\]\n]+\?)\]\([^\n]+\)\s*\n", SEPARATOR.split(page.markdown, maxsplit=1)[0]
    )
    for prompt, body in zip(chunks[1::2], chunks[2::2]):
        body = re.split(r"(?m)^#{1,4} |^\* \* \*$|^✕", body)[0].strip()
        quote = plain(body)
        if len(quote) > 25:
            yield FAQ(plain(prompt), quote[:650], provenance(page, sid, body), tokens(prompt), path)


class MemberInfo:
    def __init__(self, snapshot):
        self.sid = snapshot.snapshot_id
        self.pages = {}
        self.news = []
        self.faq = []
        for page in sorted(snapshot.pages, key=lambda p: (p.url, p.fetched_at)):
            path = path_for(page.url)
            preferred = self.pages.get(path)
            if preferred is None or (
                (not bool(re.search(r"[?#]", page.url)), page.fetched_at)
                > (not bool(re.search(r"[?#]", preferred.url)), preferred.fetched_at)
            ):
                self.pages[path] = page
            if path.startswith("/newsroom/") and "/tag/" not in path and "/author/" not in path:
                item = article(page)
                if item.published:
                    self.news.append(item)
            self.faq.extend(_extract_faq(page, self.sid))
        self.news.sort(key=lambda item: item.published, reverse=True)
        contact = self.pages.get("/contact")
        self.branches = [
            Branch(_aliases(m[2], m[1]), m[2], m[1], provenance(contact, self.sid, m[1]))
            for m in BRANCH_ADDRESS.finditer(contact.markdown if contact else "")
        ]
        self.schedules = list(_branch_articles(self.news, self.sid))

    def _source(self, page, text):
        return provenance(page, self.sid, text)

    def _contact(self):
        return self.pages.get("/contact")

    def _lookup(self, path, pattern, flags=re.I):
        page = self.pages.get(path)
        if page:
            for line in SEPARATOR.split(page.markdown, maxsplit=1)[0].splitlines():
                cleaned = plain(line)
                if re.search(pattern, cleaned, flags):
                    return page, cleaned
        return None, ""

    def _branch_count_data(self, as_of):
        contact, statement = self._lookup("/contact", r"\b\d+ (?:dupaco |full.service )?branches\b")
        if contact:
            count = int(re.search(r"\b\d+\b", statement)[0])
            return count, [self._source(contact, statement)], "The branch directory gives the current count."
        published = [
            (item, match)
            for item in self.news
            if item.published <= as_of and (match := BRANCH_COUNT.search(item.text))
        ]
        closures = self._closures(as_of)
        if not published:
            return None
        latest, match = published[0]
        count = int(match[1])
        sources = [self._source(latest.page, match[0])]
        # An explicit closure is stronger evidence than repeated press-release boilerplate.
        for item, closed, sentence in closures.values():
            if latest.published < closed <= as_of:
                count -= 1
                sources.append(self._source(item.page, sentence))
        reason = (
            "The latest published count is adjusted for a subsequent scheduled closure."
            if len(sources) > 1
            else "The newest dated organization-wide statement takes precedence."
        )
        return count, sources, reason

    def _branch_count(self, as_of):
        counts = self._branch_count_data(as_of)
        if counts is None:
            return unknown()
        count, sources, _ = counts
        return answer(f"The snapshots indicate {count} Dupaco branch office locations.", *sources)

    def conflicts(self, as_of):
        counts = self._branch_count_data(as_of)
        if counts is None:
            return []
        count, sources, reason = counts
        competing = {}
        for item in self.news:
            if item.published <= as_of and (match := BRANCH_COUNT.search(item.text)):
                value = int(match[1])
                if value != count:
                    competing.setdefault(value, self._source(item.page, match[0]))
        if not competing:
            return []
        ordered = sorted(competing, reverse=True)
        return [
            Conflict(
                "Dupaco branch office count",
                [str(count), *(str(value) for value in ordered)],
                sources + [competing[value] for value in ordered],
                str(count),
                reason,
            )
        ]

    def _closures(self, as_of):
        closures = {}
        for item in self.news:
            if item.published > as_of or "clos" not in item.page.title.lower():
                continue
            name = re.search(r"\b([A-Z][\w-]+(?:\s+[A-Z][\w-]+){0,2})\s+branch\b", item.page.title)
            sentence = next(
                (plain(line) for line in item.text.splitlines() if BRANCH_ACTION.search(line)), ""
            )
            event = BRANCH_ACTION.search(sentence)
            if name and event and (closed := parse_date(event[1])):
                closures.setdefault(normalize(name[1]), (item, closed, sentence))
        return closures

    def _branch(self, q, as_of):
        contact = self._contact()
        if re.search(r"\b(?:locator|find (?:a|my|the nearest)|where (?:can|do) i find)\b", q) or (
            re.search(r"\b(?:url|link|website|map)\b", q) and re.search(r"\b(?:atm|branch)", q)
        ):
            if contact:
                return answer(
                    f"Use Dupaco’s branch and ATM locator at {contact.url}.",
                    self._source(contact, contact.title),
                )
        if re.search(r"\b(?:how many|number of|branch count)\b", q):
            return self._branch_count(as_of)
        closures = self._closures(as_of)
        closure = next(
            (data for name, data in closures.items() if re.search(rf"\b{re.escape(name)}\b", q)), None
        )
        if closure:
            item, closed, sentence = closure
            src = self._source(item.page, sentence)
            if re.search(r"\b(?:alternative|where|instead|full.service|nearby)\b", q):
                for line in item.text.splitlines():
                    if re.search(r"\bfull-service branches at\b", line):
                        match = re.search(r"full-service branches at ([^.\u201d]+)", line, re.I)
                        if match:
                            alternatives = match[1].strip(" ,")
                            return answer(
                                f"The announcement directs members to the {alternatives} branches.",
                                self._source(item.page, line),
                            )
            if re.search(r"\b(?:closed?|open|operat|hours|shut)", q):
                day = f"{closed:%B} {closed.day}, {closed.year}"
                if as_of >= closed:
                    return answer(
                        f"Dupaco’s announcement set the branch closure for {day}. That date has passed.", src
                    )
                return answer(f"Dupaco scheduled the branch to close on {day}.", src)

        branches = self.branches + [
            s.branch for s in self.schedules if not s.published or s.published <= as_of
        ]
        matched = [
            b for b in branches if any(re.search(rf"\b{re.escape(alias)}\b", q) for alias in b.aliases)
        ]
        if re.search(r"\b(?:address|located|where is|directions)\b", q):
            exact = {b.address: b for b in matched}
            if exact:
                values = list(exact.values())
                return answer("\n".join(item.address for item in values), *(item.source for item in values))
            return unknown(self._source(contact, contact.title) if contact else None)
        if not re.search(r"\b(?:lobby|hours?|drive[ -]?(?:up|thru|through)|open|close)\b", q):
            return None
        service = "drive-up" if re.search(r"drive[ -]?(?:up|thru|through)", q) else "lobby"
        schedules = [
            s for s in self.schedules if s.service == service and (not s.published or s.published <= as_of)
        ]
        generic = tokens(
            re.sub(
                r"\b(?:branch(?:es)?|locations?|offices?|lobby|drive[ -]?(?:up|thru|through)|hours?|open|close)\b",
                " ",
                q,
            )
        )
        generic -= {
            "all",
            "branch",
            "general",
            "same",
            "keep",
            "usually",
            "typical",
            "banking",
            "today",
            "current",
            "standard",
            "different",
            "vary",
            "regular",
            "weekend",
            "weekday",
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
            "time",
        }
        if matched:
            targets = [s for s in schedules if s.branch in matched]
            if targets:
                entry = targets[0]
                return answer(
                    f"The published {service} schedule for {entry.branch.city} ({entry.branch.address}) is {entry.hours}. "
                    "Check the branch locator for any later changes.",
                    entry.branch.source,
                )
            return unknown(self._source(contact, contact.title) if contact else None)
        if generic or not schedules:
            return unknown(self._source(contact, contact.title) if contact else None)
        # Prefer representative schedules with distinct service windows. Cite each actual example.
        regular = (
            next((s for s in schedules if "noon" in normalize(s.hours)), None)
            if service == "drive-up"
            else next((s for s in schedules if "monday-thursday" in normalize(s.hours)), None)
        )
        regular = regular or schedules[0]
        different = next((s for s in schedules if "saturday-sunday" in normalize(s.hours)), None)
        text = f"{service.capitalize()} hours vary by branch. For example, {regular.branch.city} lists {regular.hours}"
        sources = [regular.branch.source]
        if service == "lobby" and re.search(r"\b(?:all|same|different|vary)\b", q):
            noon = next((s for s in schedules if "noon" in normalize(s.hours)), None)
            if noon and noon != regular:
                text += f". The published schedule for {noon.branch.city} includes a Saturday closing at noon"
                sources.append(noon.branch.source)
        if different:
            text += f". {different.branch.city} lists {different.hours}"
            sources.append(different.branch.source)
        if contact:
            text += f". Check {contact.url} for your branch"
            sources.append(self._source(contact, "Find a Location Near You"))
        return answer(text + ".", *sources)

    def _news_answer(self, q, as_of):
        if "forbes" in q and re.search(r"\b(?:rank|recogni|award|best|top|honor|how many)", q):
            if re.search(r"\b(?:employer|workplace)", q):
                criterion = r"forbes.*(?:small employer|employers)"
            else:
                criterion = r"forbes.*(?:top-rated credit unions|best.in.state|time forbes recognized)"
            year = re.search(r"\b(20\d{2})\b", q)
            for item in self.news:
                if (
                    item.published > as_of
                    or "forbes" not in item.page.title.lower()
                    or year
                    and item.published.year != int(year[1])
                ):
                    continue
                for line in item.text.splitlines():
                    if (
                        re.search(criterion, line, re.I)
                        and len(line) < 700
                        and not line.startswith(("#", "["))
                    ):
                        return answer(plain(line), self._source(item.page, line))
        if re.search(r"\bvolunteer time off\b|\bvto\b", q) and re.search(
            r"\b(?:how many|employees|used)\b", q
        ):
            if re.search(r"\b(?:percent|percentage|share|fraction|ratio)\b", q):
                return unknown()
            year = re.search(r"\b(20\d{2})\b", q)
            matches = []
            for item in self.news:
                if item.published > as_of:
                    continue
                for line in item.text.splitlines():
                    clean = plain(line)
                    match = re.search(
                        r"(\d[\d,]*) (?:Dupaco )?employees\s+used.*?\bin\s+(20\d{2})\b.*?Volunteer Time Off",
                        clean,
                        re.I,
                    )
                    if match:
                        matches.append((int(match[2]), match[1], item, clean))
            matches = [m for m in matches if not year or m[0] == int(year[1])]
            if matches:
                event_year, count, item, line = max(matches, key=lambda m: m[0])
                text = f"{count} employees used Volunteer Time Off in {event_year}."
                if "last year" in q and event_year != as_of.year - 1:
                    return Answer(
                        f"I couldn't confirm the count for {as_of.year - 1}. {text}",
                        [self._source(item.page, line)],
                        confident=False,
                    )
                return answer(text, self._source(item.page, line))
        if re.search(r"\bhome[\$s]?tart\b|\bhome start\b", q):
            for item in self.news:
                if item.published <= as_of and "home$tart" in normalize(item.page.title):
                    line = next(
                        (
                            line
                            for line in item.text.splitlines()
                            if amounts(line) and "down payment" in line.lower()
                        ),
                        "",
                    )
                    if line:
                        summary = re.search(r"Home\$tart program\.\s*(.+)", line, re.I)
                        return answer(
                            "Home$tart is a Federal Home Loan Bank program for eligible first-time homebuyers. "
                            + (summary[1].replace("This initiative", "It") if summary else plain(line)),
                            self._source(item.page, line),
                        )
        return None

    def specific(self, q, as_of):
        contact = self._contact()
        if re.search(r"\brouting(?:\s+(?:transit|aba))?\s*(?:number|#)|\baba\s+number|\brtn\b", q):
            for path in ("/business/cash-management", "/contact"):
                page, line = self._lookup(path, r"routing.{0,25}\b\d{9}\b")
                if page:
                    number = re.search(r"\b\d{9}\b", line)[0]
                    return answer(f"Dupaco’s routing number is {number}.", self._source(page, line))
            return unknown()
        if re.search(BRANCH_MARKER, q) or re.search(r"\b(?:atm locator|find an atm)\b", q):
            branch_answer = self._branch(q, as_of)
            if branch_answer:
                return branch_answer
        if re.search(r"\b(?:join|eligible|eligibility|become a member|membership requirements?)\b", q):
            page = self.pages.get("/about/join")
            if page and not re.search(r"\b(?:loan|card|grant|scholarship|apy|overdraft)\b", q):
                if re.search(r"\b(?:who|eligible|eligibility|qualify|requirements?|live|work)\b", q):
                    lines = [
                        plain(line).lstrip("- ")
                        for line in page.markdown.splitlines()
                        if re.search(
                            r"you live or work within|you have an immediate family|you belong to the",
                            line,
                            re.I,
                        )
                    ]
                    region = next(
                        (plain(line) for line in page.markdown.splitlines() if "You can find us in" in line),
                        "",
                    )
                    if lines:
                        return answer(
                            " ".join(lines + ([region.split(".")[0] + "."] if region else [])),
                            self._source(page, "\n".join(lines)),
                        )
                for label, url in LINK.findall(page.markdown):
                    if re.search(r"\b(?:become a member|join dupaco|apply online)\b", normalize(label)):
                        return answer(f"Apply for Dupaco membership at {url}.", self._source(page, label))
        if re.search(r"\b(?:update|change|edit)\b", q) and re.search(
            r"\b(?:contact info(?:rmation)?|phone number|email address|mailing address|personal info(?:rmation)?)\b",
            q,
        ):
            if contact:
                line = next(
                    (
                        plain(line)
                        for line in contact.markdown.splitlines()
                        if PHONE.search(line) and "Shine" in line
                    ),
                    "",
                )
                if line:
                    return answer(
                        f"Use the Contact Us tab in Shine Online or Mobile Banking, or call Dupaco at {PHONE.search(line)[0]}.",
                        self._source(contact, line),
                    )
        if re.search(r"\b(?:mobile wallets?|apple pay|google pay|samsung pay|tap to pay)\b", q):
            page, line = self._lookup(
                "/online-and-mobile-banking/mobile-payments", r"Apple Pay.*Google Pay.*Samsung Pay"
            )
            if page and not re.search(
                r"\b(?:how do i set|how to|fee|charge|which device|compatible with my)\b", q
            ):
                default_page, default_line = self._lookup(path_for(page.url), r"set one as your default")
                return answer(
                    line + (" You can also set one card as your default." if default_page else ""),
                    self._source(page, default_line or line),
                )
        if (
            re.search(r"\b(?:student loans?|student|university|college)\b", q)
            and re.search(r"\b(?:loan|financ|borrowing)", q)
            and not re.search(r"\b(?:apr|rate|interest|fee|how much)\b", q)
        ):
            path = "/personal/loans/student-loans"
            page, line = self._lookup(path, r"defer full payments.*flexible")
            _, fee = self._lookup(path, r"(?:no|\$0) origination fees?")
            if page:
                return answer(
                    "Dupaco offers student loans for school funding and student loan refinancing. "
                    + line
                    + (" " + fee if fee else ""),
                    self._source(page, line + " " + fee),
                )
        if (
            re.search(r"\b(?:contact|phone|call|mailing address)\b", q)
            and contact
            and not re.search(r"\b(?:wire|card activation|pin)\b", q)
        ):
            if "mailing address" in q:
                match = re.search(r"\*\*Address:\*\*\s*([^\n]+)\s*\n\s*([^\n]+)", contact.markdown)
                if match:
                    return answer(f"{match[1]}, {match[2]}.", self._source(contact, match[0]))
            if re.search(r"\b(?:phone number|customer service|how (?:do|can) i (?:call|contact))\b", q):
                number = PHONE.search(contact.markdown)
                if number:
                    return answer(f"Call Dupaco at {number[0]}.", self._source(contact, number[0]))
        if re.search(r"\b(?:credit cards?|visa)\b", q) and not re.search(FEE_INTENT, q):
            path = "/business/credit-cards" if "business" in q else "/personal/credit-cards"
            page = self.pages.get(path)
            if page and re.search(r"\b(?:lost|stolen|missing)\b", q):
                faq = next(
                    (
                        entry
                        for entry in self.faq
                        if entry.path == path and "lost or stolen" in normalize(entry.prompt)
                    ),
                    None,
                )
                if faq:
                    return answer(faq.response, faq.source)
            if page and re.search(r"\b(?:apply|application)\b", q):
                requested = "platinum" if "platinum" in q else "reward" if "reward" in q else ""
                links = [
                    (label, url)
                    for label, url in LINK.findall(page.markdown)
                    if label.startswith("Apply") and requested in normalize(label) and not url.endswith("#")
                ]
                if links:
                    return answer(
                        "\n".join(f"{label} at {url}." for label, url in links),
                        self._source(page, " ".join(label for label, _ in links)),
                    )
        return self._news_answer(q, as_of)

    def find_faq(self, q):
        wanted = tokens(q)
        if len(wanted) < 2:
            return None
        ranked = []
        for faq in self.faq:
            overlap = len(faq.tokens & wanted)
            # Require terms to match the question in the FAQ itself. Body-only matches can
            # confuse an example, an unrelated product, or a footnote for a direct answer.
            if overlap < 2 or (overlap < len(wanted) and overlap < len(faq.tokens)):
                continue
            if ("business" in wanted) != ("/business/" in faq.path):
                continue
            coverage = overlap / max(len(faq.tokens), len(wanted))
            if coverage >= 0.65:
                ranked.append((coverage, overlap, faq))
        if not ranked:
            return None
        ranked.sort(key=lambda item: (item[0], item[1], item[2].prompt), reverse=True)
        top = ranked[0][2]
        return answer(top.response, top.source)
