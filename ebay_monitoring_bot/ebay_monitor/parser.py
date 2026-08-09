from __future__ import annotations

import re
from html import unescape
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from parsel import Selector

from .config import ParserSettings, SearchSettings
from .models import Component, Listing


NUMBER_WORDS = {
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
    "twelve": 12,
}

PRODUCT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "pokemon_center_etb",
        re.compile(r"pokemon\s*center.{0,35}(?:elite trainer box|etbs?\b)|\bpc\s*etbs?\b", re.I),
    ),
    ("elite_trainer_box", re.compile(r"\b(?:elite trainer boxes|elite trainer box|etbs?)\b", re.I)),
    ("booster_bundle", re.compile(r"\bbooster bundles?\b", re.I)),
    ("booster_box", re.compile(r"\b(?:booster boxes|booster box|booster display|display box)\b", re.I)),
    (
        "premium_collection",
        re.compile(
            r"\b(?:premium collection|ultra premium collection|upc|collection boxes|collection box|collector chest|binder collection|poster collection)\b",
            re.I,
        ),
    ),
    ("tin", re.compile(r"\b(?:mini tins?|tins?)\b", re.I)),
    ("loose_packs", re.compile(r"\b(?:booster packs?|loose packs?|packs?)\b", re.I)),
    (
        "single_card",
        re.compile(
            r"\b(?:psa|bgs|cgc|ace graded|graded card|single card|raw card|black star promo|promo holo|holo promo|promo card|etb promo|svp\s*-?\s*\d+)\b",
            re.I,
        ),
    ),
]


def money_value(container: dict[str, Any] | None) -> float:
    if not container:
        return 0.0
    try:
        return float(container.get("value") or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_money_text(value: str) -> tuple[float, str]:
    text = unescape(value or "").replace("\xa0", " ")
    text = text.replace("\u0141", "\u00a3")
    currency = "GBP" if "\u00a3" in text or "GBP" in text.upper() else ""
    if "US $" in text.upper() or "US$" in text.upper():
        currency = "USD"
    elif "$" in text and not currency:
        currency = "USD"

    match = re.search(r"(?:\u00a3|GBP|US \$|US\$|\$)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text, re.I)
    if not match:
        return 0.0, currency or "GBP"
    try:
        return float(match.group(1).replace(",", "")), currency or "GBP"
    except ValueError:
        return 0.0, currency or "GBP"


def first_text(selector: Selector, css: str) -> str:
    for value in selector.css(css).getall():
        text = " ".join(value.split())
        if text:
            return text
    return ""


def shipping_cost(item: dict[str, Any]) -> float:
    values = []
    for option in item.get("shippingOptions") or []:
        cost = money_value(option.get("shippingCost"))
        values.append(cost)
    return min(values) if values else 0.0


def item_price(item: dict[str, Any]) -> float:
    return money_value(item.get("price")) or money_value(item.get("currentBidPrice"))


def item_currency(item: dict[str, Any], default: str = "GBP") -> str:
    price = item.get("price") or item.get("currentBidPrice") or {}
    return str(price.get("currency") or default)


def buying_option(item: dict[str, Any]) -> str:
    options = item.get("buyingOptions") or []
    if "AUCTION" in options:
        return "AUCTION"
    if "FIXED_PRICE" in options:
        return "BIN"
    return str(options[0]) if options else "UNKNOWN"


def location_bucket(country: str) -> str:
    value = (country or "").upper()
    if value in {"GB", "UK", "UNITED KINGDOM"}:
        return "UK"
    if value in {
        "AT",
        "BE",
        "BG",
        "CY",
        "CZ",
        "DE",
        "DK",
        "EE",
        "ES",
        "FI",
        "FR",
        "GR",
        "HR",
        "HU",
        "IE",
        "IT",
        "LT",
        "LU",
        "LV",
        "MT",
        "NL",
        "PL",
        "PT",
        "RO",
        "SE",
        "SI",
        "SK",
    }:
        return "EU"
    if value in {"US", "USA", "UNITED STATES"}:
        return "US"
    if value in {"CN", "HK", "JP", "KR", "SG", "TH", "TW", "MY", "ID", "VN"}:
        return "Asia"
    return country or "Unknown"


def normalize_text(value: str) -> str:
    text = value.casefold()
    text = text.replace("pokémon", "pokemon")
    text = re.sub(r"[^a-z0-9+&/\- ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def infer_franchise(text: str) -> str:
    if "one piece" in text or re.search(r"\bop\s?-\s?\d{2}\b", text):
        return "one_piece"
    if "pokemon" in text or "pikachu" in text:
        return "pokemon"
    return "unknown"


def infer_language(text: str) -> str:
    if re.search(r"\b(japanese|japan|jap|jp|jpn)\b", text):
        return "japanese"
    if re.search(r"\b(english|eng|en|uk|usa|us)\b", text):
        return "english"
    if re.search(r"\b(german|deutsch|ger)\b", text):
        return "german"
    if re.search(r"\b(chinese|china|chs|cht|cn)\b", text):
        return "chinese"
    if re.search(r"\b(spanish|espanol|espa(?:n|ñ)ol|esp)\b", text):
        return "spanish"
    if re.search(r"\b(french|francais|français|fra|fr)\b", text):
        return "french"
    if re.search(r"\b(italian|italiano|ita)\b", text):
        return "italian"
    if re.search(r"\b(korean|korea|kor|kr)\b", text):
        return "korean"
    return "unknown"


def find_set_name(text: str, franchise: str, settings: ParserSettings) -> str:
    aliases = settings.set_aliases.get(franchise, {})
    if franchise == "one_piece":
        # Prefer the first actual set code. This avoids a later range such as
        # "OP-01-15" overriding the product's leading OP-09 code.
        code_match = re.search(r"\bop-?(\d{1,2})\b", text)
        if code_match:
            number = int(code_match.group(1))
            for code in (f"op{number:02d}", f"op-{number:02d}"):
                if code in aliases:
                    return aliases[code]
    for alias in sorted(aliases, key=len, reverse=True):
        if alias in text:
            return aliases[alias]
    return "unknown_set"


def infer_card_identity(text: str) -> str:
    number_patterns = (
        r"\b(op\d{1,2}-\d{2,3})\b",
        r"\b(svp\s*-?\s*\d{2,3})\b",
        r"\b(\d{1,4}/\d{1,4})\b",
        r"#\s*(\d{2,4})\b",
        r"\b(\d{3})\b",
    )
    card_number = ""
    for pattern in number_patterns:
        match = re.search(pattern, text)
        if match:
            card_number = re.sub(r"\s+", "", match.group(1))
            break
    grade_match = re.search(r"\b(psa|bgs|cgc|ace)\s*(\d+(?:\.\d+)?)\b", text)
    grade = f"{grade_match.group(1)}-{grade_match.group(2)}" if grade_match else "raw"
    return f"{card_number}|{grade}" if card_number else ""


def valid_quantity(raw: str, *, explicit: bool = False) -> int | None:
    if not raw.isdigit():
        return NUMBER_WORDS.get(raw)
    # Leading-zero values are normally set/card identifiers (OP09, 027, 075).
    if len(raw) > 1 and raw.startswith("0"):
        return None
    value = int(raw)
    # Four-digit values are normally release years. Large bare values are usually
    # set numbers such as Pokemon 151, rather than sellable quantities.
    if 1900 <= value <= 2100 or value < 1:
        return None
    if not explicit and value > 100:
        return None
    return value


def parse_quantity(text: str, start: int, end: int, product_type: str, settings: ParserSettings) -> tuple[int, list[str]]:
    notes: list[str] = []
    window_start = max(0, start - 32)
    window_end = min(len(text), end + 32)
    before = text[window_start:start]
    after = text[end:window_end]
    window = text[window_start:window_end]

    # Explicit prefixes belong to the product even when an accessory case is
    # also mentioned later in the title.
    explicit_prefix = re.search(r"(?<![a-z0-9-])(\d+)\s*x\s*$", before)
    if not explicit_prefix and not re.search(r"\b(?:plus|and|with)\b", text[:start]):
        explicit_prefix = re.search(r"^\s*(?:x\s*(\d+)|(\d+)\s*x)\b", text)
    if explicit_prefix:
        quantity = valid_quantity(
            explicit_prefix.group(1) or explicit_prefix.group(2), explicit=True
        )
        if quantity is not None:
            return quantity, ["quantity parsed"]

    trailing_quantity = re.search(r"\bx\s*(\d+)\s*$", after)
    if trailing_quantity:
        quantity = valid_quantity(trailing_quantity.group(1), explicit=True)
        if quantity is not None:
            return quantity, ["quantity parsed"]

    # These numbers state a sealed product's contents, not product quantity.
    product_phrase = text[start:end]
    if product_type == "booster_box":
        display_count = re.search(r"(?<![a-z0-9-])(18|24|36)\s*$", before)
        pack_count = re.search(
            r"^\s*x?\s*(18|24|36)\s+(?:(?:booster\s+)?packs?|envelopes?)\b",
            after,
        )
        if ("display" in product_phrase and display_count) or pack_count:
            return 1, []
    if product_type == "booster_bundle" and re.search(
        r"^\s*[,(-]?\s*\d+\s+(?:booster\s+)?packs?\b", after
    ):
        return 1, []
    if product_type == "elite_trainer_box" and re.search(r"^\s*\d+\s+cards?\b", after):
        return 1, []
    if product_type == "tin" and re.search(r"^\s*\d+\s+cards?\b", after):
        return 1, []

    non_wholesale_case = re.search(
        r"\b(?:from|in|with)\s+(?:a\s+|the\s+)?(?:fresh\s+)?case\b|[+&]\s*case\b|\bcase\s+fresh\b",
        window,
    )
    if non_wholesale_case:
        return 1, []

    accessory_case = re.search(
        r"\b(?:acrylic|arcylic|pet|protector|protective|display|storage)(?:\s+\w+){0,2}\s+case\b",
        window,
    )
    case_match = None if accessory_case else re.search(
        r"\b(?:sealed |full )?case(?:\s+of)?(?:\s+(\d+))?\b", window
    )
    if case_match:
        if case_match.group(1):
            quantity = valid_quantity(case_match.group(1), explicit=True)
            if quantity is not None:
                return quantity, ["case quantity parsed"]
        franchise = infer_franchise(text)
        assumed = settings.assumed_case_units.get(
            f"{franchise}_{product_type}", settings.assumed_case_units.get(product_type, 1)
        )
        return assumed, [f"case quantity assumed as {assumed}"]

    explicit_patterns: list[tuple[re.Match[str] | None, bool]] = [
        (re.search(r"(?<![a-z0-9-])(\d+)\s*[x×]\s*$", before), True),
        (re.search(r"^\s*[x×]\s*(\d+)\b", after), True),
        (re.search(r"\b(?:lot|set|bundle)\s+of\s+(\d+)\s*$", before), True),
        (re.search(r"^\s*(\d+)\s+(?:boxes|bundles|tins|cards)\b", after), True),
    ]
    bare_patterns: list[tuple[re.Match[str] | None, bool]] = [
        (re.search(r"(?<![a-z0-9-])(\d+)\s*$", before), False),
        (re.search(r"^\s*(\d+)\b(?!\s+packs?\b)", after), False),
    ]
    # Bare numbers beside cards are usually grades, years, or collector IDs.
    patterns = explicit_patterns if product_type == "single_card" else [*explicit_patterns, *bare_patterns]
    for match, explicit in patterns:
        if not match:
            continue
        raw = match.group(1)
        quantity = valid_quantity(raw, explicit=explicit)
        if quantity is not None:
            return quantity, ["quantity parsed"]
    return 1, notes


def parse_components(title: str, settings: ParserSettings) -> tuple[list[Component], bool, list[str]]:
    text = normalize_text(title)
    notes: list[str] = []
    franchise = infer_franchise(text)
    language = infer_language(text)
    set_name = find_set_name(text, franchise, settings)
    single_card_context = is_single_card_context(text)
    card_identity = infer_card_identity(text) if single_card_context else ""
    has_container_product = any(
        pattern.search(text)
        for product_type, pattern in PRODUCT_PATTERNS
        if product_type in {"booster_box", "booster_bundle"}
    )
    components: list[Component] = []

    for product_type, pattern in PRODUCT_PATTERNS:
        for match in pattern.finditer(text):
            if single_card_context and product_type not in {"single_card"}:
                continue
            # "Booster box 36 packs" describes the contents of one box; it is
            # not a mixed lot containing 36 boxes plus 36 loose packs.
            if product_type == "loose_packs" and has_container_product:
                continue
            if product_type == "elite_trainer_box" and (
                is_pokemon_center_etb_context(text, match.start())
                or "pokemon center" in text
                or re.search(r"\bpc\s*etb", text)
            ):
                continue
            variant = "pokemon_center" if product_type == "pokemon_center_etb" else "standard"
            base_type = "elite_trainer_box" if product_type == "pokemon_center_etb" else product_type
            quantity, quantity_notes = parse_quantity(text, match.start(), match.end(), product_type, settings)
            confidence = 0.9
            if set_name == "unknown_set":
                confidence -= 0.25
            if franchise == "unknown":
                confidence -= 0.2
            if language == "unknown":
                confidence -= 0.1
            if any("assumed" in note for note in quantity_notes):
                confidence -= 0.1
            if base_type == "single_card" and not card_identity:
                confidence -= 0.3
            components.append(
                Component(
                    franchise=franchise,
                    language=language,
                    product_type=base_type,
                    set_name=set_name,
                    quantity=quantity,
                    variant=variant,
                    confidence=max(0.1, confidence),
                    notes=tuple(quantity_notes),
                    identity=card_identity if base_type == "single_card" else "",
                )
            )

    components = dedupe_components(components)
    uncertain_terms = re.search(r"\b(job lot|mixed lot|collection|misc|assorted|various)\b", text)
    if re.search(r"\bbundle\b", text) and not any(
        component.product_type == "booster_bundle" for component in components
    ):
        uncertain_terms = uncertain_terms or re.search(r"\bbundle\b", text)
    uncertain = False
    if not components:
        uncertain = True
        notes.append("No known sealed/card component could be parsed.")
    if uncertain_terms and len(components) <= 1:
        uncertain = True
        notes.append("Listing appears to be a mixed lot but contents are incomplete.")
    if any(component.confidence < 0.7 for component in components):
        uncertain = True
        notes.append("One or more components have low parser confidence.")
    return components, uncertain, notes


def is_single_card_context(text: str) -> bool:
    if re.search(r"\b(?:psa|bgs|cgc|ace)\s*\d+\b", text):
        return True
    if re.search(r"\b(?:black star promo|promo holo|holo promo|promo card|etb promo|svp\s*\d+)\b", text):
        return not re.search(r"\b(?:includes?|with|plus|and)\s+(?:a\s+)?promo\b", text)
    card_signals = (
        r"\b(?:psa|bgs|cgc|ace)\s*\d",
        r"\b(?:gem mint|near mint|nm|mint)\b",
        r"\b(?:black star promo|promo holo|promo card|single card|raw card|graded card)\b",
        r"\b\d{1,4}/\d{1,4}\b",
    )
    sealed_signals = (
        r"\b(?:factory sealed|sealed|unopened|case of|full case|new sealed)\b",
    )
    return any(re.search(pattern, text) for pattern in card_signals) and not any(
        re.search(pattern, text) for pattern in sealed_signals
    )


def is_pokemon_center_etb_context(text: str, match_start: int) -> bool:
    before = text[max(0, match_start - 96) : match_start]
    return "pokemon center" in before or re.search(r"\bpc\s*$", before) is not None


def dedupe_components(components: list[Component]) -> list[Component]:
    merged: dict[str, Component] = {}
    for component in components:
        key = component.group_key
        existing = merged.get(key)
        if existing:
            existing_explicit = bool(existing.notes)
            component_explicit = bool(component.notes)
            duplicate_synonym = existing.quantity == 1 and component.quantity == 1 and not (
                existing_explicit and component_explicit
            )
            # Multiple regex synonyms can match the same title ("ETB Elite
            # Trainer Box"). They describe one component, not additive lots.
            quantity = max(existing.quantity, component.quantity)
            merged[key] = Component(
                franchise=component.franchise,
                language=component.language,
                product_type=component.product_type,
                set_name=component.set_name,
                quantity=quantity,
                variant=component.variant,
                confidence=min(existing.confidence, component.confidence),
                notes=tuple(dict.fromkeys([*existing.notes, *component.notes])),
                identity=component.identity,
            )
        else:
            merged[key] = component
    return list(merged.values())


def item_end_time(item: dict[str, Any]) -> str:
    return str(item.get("itemEndDate") or item.get("itemCreationDate") or "")


def coupon_text(item: dict[str, Any]) -> str:
    values = []
    for key in ["marketingPrice", "discountPrice", "availableCoupons", "coupon"]:
        value = item.get(key)
        if value:
            values.append(str(value))
    return " | ".join(values)


def clean_item_url(url: str, item_id: str) -> str:
    if item_id:
        return f"https://www.ebay.co.uk/itm/{item_id}"
    parsed = urlparse(unescape(url or ""))
    if parsed.scheme and parsed.netloc and parsed.path:
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return unescape(url or "")


def parse_item_id(card: Selector, href: str) -> str:
    item_id = card.css("::attr(data-listingid)").get() or ""
    if item_id and item_id != "2500219655424533":
        return item_id
    match = re.search(r"/itm/(?:[^/?#]+/)?(\d{9,15})", href)
    return match.group(1) if match else ""


def visible_texts(selector: Selector) -> list[str]:
    texts = selector.xpath(
        ".//text()[not(ancestor::script) and not(ancestor::style) and not(ancestor::noscript)]"
    ).getall()
    return [" ".join(text.split()) for text in texts if text.strip()]


def parse_shipping_from_texts(texts: list[str]) -> float:
    combined = " | ".join(texts).replace("\u0141", "\u00a3")
    if re.search(r"\bfree (?:postage|delivery|shipping)\b", combined, re.I):
        return 0.0
    match = re.search(
        r"\+?\s*(\u00a3\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:postage|delivery|shipping)",
        combined,
        re.I,
    )
    if not match:
        return 0.0
    return parse_money_text(match.group(1))[0]


def parse_location_from_texts(texts: list[str]) -> str:
    combined = " | ".join(texts)
    for pattern in [r"Located in:\s*([^|]+)", r"\bfrom\s+([^|]+)"]:
        match = re.search(pattern, combined, re.I)
        if match:
            value = match.group(1).strip(" .")
            if "sellers worldwide" in value.casefold():
                continue
            if "," in value:
                return value.split(",")[-1].strip()
            return value
    return ""


def parse_buying_options_from_texts(texts: list[str], hint: str = "") -> list[str]:
    option = hint.casefold()
    if option in {"auction", "auctions"}:
        return ["AUCTION"]
    if option in {"bin", "buy_it_now", "fixed_price"}:
        return ["FIXED_PRICE"]
    combined = " | ".join(texts).casefold()
    if "bid" in combined or "auction" in combined:
        return ["AUCTION"]
    if "buy it now" in combined or "best offer" in combined:
        return ["FIXED_PRICE"]
    return ["FIXED_PRICE"]


def parse_end_time_from_texts(texts: list[str]) -> str:
    combined = " | ".join(texts)
    match = re.search(r"(\d+d\s+\d+h|\d+h\s+\d+m|\d+m)\s+(?:left|remaining)", combined, re.I)
    if match:
        return match.group(1)
    return ""


def parse_coupon_from_texts(texts: list[str]) -> str:
    ignored = re.compile(r"\b(save seller|save on combined postage|bundle and save)\b", re.I)
    hits = [
        text
        for text in texts
        if not ignored.search(text)
        and re.search(r"\b(coupon|voucher|discount|save\s+(?:\d|£|\u00a3|\$)|% off)\b", text, re.I)
    ]
    return " | ".join(hits[:3])


def parse_search_card(card: Selector, buying_option_hint: str = "") -> dict[str, Any] | None:
    href = card.css("a.s-card__link::attr(href), a[href*='/itm/']::attr(href)").get() or ""
    item_id = parse_item_id(card, href)
    if not item_id or item_id == "123456":
        return None

    title = first_text(
        card,
        ".s-card__title span.su-styled-text.primary::text, "
        ".s-card__title::text, "
        "img.s-card__image::attr(alt)",
    )
    title = title.replace("Opens in a new window or tab", "").strip()
    if not title or title == "Shop on eBay":
        return None

    price_text = first_text(card, ".s-card__price::text, .s-item__price::text")
    price, currency = parse_money_text(price_text)
    texts = visible_texts(card)

    return {
        "itemId": item_id,
        "title": title,
        "itemWebUrl": clean_item_url(href, item_id),
        "price": {"value": f"{price:.2f}", "currency": currency},
        "shippingOptions": [
            {"shippingCost": {"value": f"{parse_shipping_from_texts(texts):.2f}", "currency": "GBP"}}
        ],
        "buyingOptions": parse_buying_options_from_texts(texts, buying_option_hint),
        "seller": {},
        "itemLocation": {"country": parse_location_from_texts(texts)},
        "itemEndDate": parse_end_time_from_texts(texts),
        "coupon": parse_coupon_from_texts(texts),
        "searchCardText": texts,
    }


def parse_search_html(
    html_text: str,
    settings: ParserSettings,
    search: SearchSettings,
    *,
    buying_option_hint: str = "",
) -> list[Listing]:
    selector = Selector(text=html_text)
    cards = selector.css("li.s-card, .su-grid__item > .s-item-card")
    listings: list[Listing] = []
    for card in cards:
        item = parse_search_card(card, buying_option_hint)
        if not item:
            continue
        listing = parse_listing(item, settings, search)
        if listing:
            listings.append(listing)
    return listings


def parse_detail_html(html_text: str) -> dict[str, Any]:
    selector = Selector(text=html_text)
    title = first_text(selector, "h1 span::text, h1::text")
    seller_texts = visible_texts(selector.css(".x-sellercard-atf_main, [data-testid='x-sellercard-atf']"))
    all_texts = visible_texts(selector)
    seller_html = selector.css(".x-sellercard-atf_main, [data-testid='x-sellercard-atf']").get() or ""

    seller_username = ""
    href_match = re.search(r"/sch/([^/?#]+)/m\.html|/usr/([^/?#\"']+)", seller_html)
    if href_match:
        seller_username = unescape(href_match.group(1) or href_match.group(2) or "")
    if not seller_username:
        for text in seller_texts:
            if re.fullmatch(r"[\w.-]{3,64}", text) and text.casefold() not in {"private", "message"}:
                seller_username = text
                break

    feedback_count = None
    count_match = re.search(r"\(([0-9,]+)\)", " | ".join(seller_texts))
    if count_match:
        feedback_count = int(count_match.group(1).replace(",", ""))
    feedback_percentage = None
    pct_match = re.search(r"([0-9]+(?:\.[0-9]+)?)%\s*positive", " | ".join(all_texts), re.I)
    if pct_match:
        feedback_percentage = float(pct_match.group(1))

    price_text = first_text(
        selector,
        "[data-testid='x-price-primary'] span::text, .x-price-primary span::text, #prcIsum::text, #mm-saleDscPrc::text",
    )
    price, currency = parse_money_text(price_text)

    shipping_texts = visible_texts(
        selector.css("[data-testid='ux-labels-values-shipping'], .ux-labels-values--shipping")
    )
    postage = parse_shipping_from_texts(shipping_texts)
    location = parse_location_from_texts(shipping_texts or all_texts)

    specifics_texts = visible_texts(
        selector.css("[data-testid='ux-labels-values'], .ux-labels-values, .ux-layout-section-evo")
    )
    return {
        "title": title,
        "price": price,
        "price_currency": currency,
        "postage": postage,
        "seller_username": seller_username,
        "seller_feedback_count": feedback_count,
        "seller_feedback_percentage": feedback_percentage,
        "seller_location": location,
        "item_specifics_text": " ".join(specifics_texts[:250]),
        "coupon": parse_coupon_from_texts(all_texts),
    }


def merge_detail_into_listing(
    listing: Listing,
    detail: dict[str, Any],
    settings: ParserSettings,
) -> None:
    if detail.get("title"):
        listing.title = str(detail["title"])
    if detail.get("price_currency") == "GBP" and detail.get("price"):
        listing.item_price = float(detail["price"])
    if detail.get("postage"):
        listing.postage = float(detail["postage"])
    if detail.get("seller_username"):
        listing.seller_username = str(detail["seller_username"])
    if detail.get("seller_feedback_count") is not None:
        listing.seller_feedback_count = int(detail["seller_feedback_count"])
    if detail.get("seller_feedback_percentage") is not None:
        listing.seller_feedback_percentage = float(detail["seller_feedback_percentage"])
    if detail.get("seller_location"):
        listing.seller_location = str(detail["seller_location"])
        listing.location_bucket = location_bucket(listing.seller_location)
    if detail.get("coupon"):
        listing.coupon = str(detail["coupon"])

    detail_text = f"{listing.title} {detail.get('item_specifics_text') or ''}"
    components, uncertain, notes = parse_components(detail_text, settings)
    if components:
        listing.components = components
        listing.contents_uncertain = uncertain
        listing.parser_notes = notes
    listing.raw["detail"] = detail


def seller_feedback_score(seller: dict[str, Any]) -> int | None:
    raw = seller.get("feedbackScore")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def seller_feedback_percentage(seller: dict[str, Any]) -> float | None:
    raw = seller.get("feedbackPercentage")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def should_exclude(title: str, search: SearchSettings) -> bool:
    text = normalize_text(title)
    configured_terms = (normalize_text(term) for term in search.excluded_title_terms)
    if any(term and term in text for term in configured_terms):
        return True
    empty_or_no_contents = (
        r"\bempty\b.*\b(?:box|case|tin|etb|display)\b",
        r"\b(?:box|case|tin|etb|display)\b.*\bempty\b",
        r"\bno\s+(?:cards?|packs?|contents?)\b",
        r"\b(?:unsealed|opened|open\s+box)\b",
        r"\btin\s+only\b",
    )
    accessory_only = (
        r"\b(?:booster (?:box|bundle)|etb|tin)\s+(?:acrylic\s+)?protector\s+case\b",
        r"\bacrylic\s+(?:display\s+)?case\s+for\b",
        r"\b(?:display|storage|protective)\s+case\s+for\b",
        r"\bacrylic\s+display\s+case\b.*\b(?:uv|protection|magnet)\b",
        r"\bacrylic\s+display\s+case\b.*\b(?:magnetic|lid)\b",
        r"\b(?:protective\s+bags?|card\s+sleeves?|sleeve\s+protectors?)\b",
        r"\b(?:tcg\s+live|live)\s+codes?\b|\bbundle\s+codes?\b",
        r"\bdouble\s+packs?\b",
        r"\bbooster\s+box\b.*\b(?:acrylic\s+display\s+case|display\s+case\s*\|?\s*acrylic|protective\s+acrylic\s+case)\b",
        r"\bhalf\s+booster\s+box\b",
        r"\b18\s+pack\s+display\s+box\b",
        r"\b(?:magnetic\s+)?acrylic\s+(?:display\s+|protector\s+)?case\b",
        r"\bacrylic\b.*\bcase\b|\bcase\b.*\bacrylic\b",
        r"\betb\b.*\b(?:sleeves?|coin|dice)\b",
        r"\bbulk\b.*\bcards?\b",
    )
    return any(re.search(pattern, text) for pattern in (*empty_or_no_contents, *accessory_only))


def parse_listing(item: dict[str, Any], settings: ParserSettings, search: SearchSettings) -> Listing | None:
    title = str(item.get("title") or "")
    if not title or should_exclude(title, search):
        return None

    seller = item.get("seller") or {}
    item_location = item.get("itemLocation") or {}
    country = str(item_location.get("country") or item_location.get("countryName") or "")
    components, uncertain, notes = parse_components(title, settings)
    listing = Listing(
        item_id=str(item.get("itemId") or item.get("legacyItemId") or ""),
        title=title,
        url=str(item.get("itemWebUrl") or item.get("itemAffiliateWebUrl") or ""),
        item_price=item_price(item),
        postage=shipping_cost(item),
        currency=item_currency(item),
        buying_option=buying_option(item),
        seller_username=str(seller.get("username") or ""),
        seller_feedback_count=seller_feedback_score(seller),
        seller_feedback_percentage=seller_feedback_percentage(seller),
        seller_location=country,
        location_bucket=location_bucket(country),
        end_time=item_end_time(item),
        coupon=coupon_text(item),
        raw=item,
        components=components,
        contents_uncertain=uncertain,
        parser_notes=notes,
    )
    if not listing.item_id or listing.total_price <= 0:
        return None
    return listing


def time_remaining(end_time: str) -> str:
    if not end_time:
        return "N/A"
    relative = re.fullmatch(r"\s*(\d+d\s+\d+h|\d+h\s+\d+m|\d+m)\s*", end_time, re.I)
    if relative:
        return relative.group(1)
    try:
        normalized = end_time.replace("Z", "+00:00")
        end = datetime.fromisoformat(normalized)
        delta = end - datetime.now(timezone.utc)
    except ValueError:
        return end_time
    if delta.total_seconds() <= 0:
        return "ended"
    days = delta.days
    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
