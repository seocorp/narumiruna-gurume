from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests
from curl_cffi.requests import exceptions as request_errors

from .exceptions import InvalidParameterError
from .http_client import DEFAULT_IMPERSONATE
from .restaurant import Restaurant

DETAIL_PARSE_EXCEPTIONS = (AttributeError, TypeError, ValueError)
PHONE_PATTERN = re.compile(r"0\d{1,4}-\d{1,4}-\d{3,4}|0\d{9,10}")
STATION_PATTERN = re.compile(r"([^\s、,，]+駅)")
DISTANCE_PATTERN = re.compile(r"([^\s、,，。]+駅)から\s*([\d,.]+\s?k?m)")
BUDGET_PATTERN = re.compile(r"￥[^\s]+")


@dataclass
class Review:
    """Review information."""

    reviewer: str
    content: str
    rating: float | None = None
    visit_date: str | None = None
    title: str | None = None
    helpful_count: int | None = None


@dataclass
class MenuItem:
    """Menu item."""

    name: str
    price: str | None = None
    description: str | None = None
    category: str | None = None


@dataclass
class Course:
    """Course information."""

    name: str
    price: str | None = None
    description: str | None = None
    items: list[str] = field(default_factory=list)


@dataclass
class RestaurantDetail:
    """Restaurant detail information."""

    restaurant: Restaurant
    reviews: list[Review] = field(default_factory=list)
    menu_items: list[MenuItem] = field(default_factory=list)
    courses: list[Course] = field(default_factory=list)


@dataclass
class RestaurantDetailRequest:
    """Restaurant detail request."""

    restaurant_url: str
    fetch_reviews: bool = True
    fetch_menu: bool = True
    fetch_courses: bool = True
    max_review_pages: int = 1

    def __post_init__(self) -> None:
        if not self.restaurant_url:
            raise InvalidParameterError("restaurant_url cannot be empty")

        if not self.restaurant_url.startswith("https://tabelog.com/"):
            raise InvalidParameterError(f"restaurant_url must be a Tabelog URL; received: {self.restaurant_url}")

        if self.max_review_pages < 1:
            raise InvalidParameterError(f"max_review_pages must be at least 1; received: {self.max_review_pages}")

    def _get_base_url(self) -> str:
        """Get the restaurant base URL without trailing slashes or query parameters."""
        url = self.restaurant_url
        if "?" in url:
            url = url.split("?")[0]
        return url.rstrip("/")

    def _parse_reviews(self, html: str) -> list[Review]:
        """Parse review information."""
        soup = BeautifulSoup(html, "html.parser")
        reviews = []

        review_items = soup.find_all("div", class_="rvw-item")

        for item in review_items:
            try:
                review = self._parse_review(item)
                if review is not None:
                    reviews.append(review)
            except DETAIL_PARSE_EXCEPTIONS:
                continue

        return reviews

    def _parse_menu_items(self, html: str) -> list[MenuItem]:
        """Parse menu items."""
        soup = BeautifulSoup(html, "html.parser")
        menu_items = []

        # Current menu pages list a category heading followed by its items, in document order.
        category = None
        for element in soup.select(".rstdtl-menu-lst__heading, .rstdtl-menu-lst__contents"):
            if "rstdtl-menu-lst__heading" in element.get_attribute_list("class"):
                category = self._get_text(element.select_one(".rstdtl-menu-lst__title"))
                continue
            with contextlib.suppress(*DETAIL_PARSE_EXCEPTIONS):
                menu_items.append(
                    MenuItem(
                        name=self._get_required_text(element.select_one(".rstdtl-menu-lst__menu-title")),
                        price=self._get_text(element.select_one(".rstdtl-menu-lst__price")),
                        description=self._get_text(element.select_one(".rstdtl-menu-lst__ex")),
                        category=category,
                    )
                )
        if menu_items:
            return menu_items

        # Legacy menu pages: find all menu categories.
        menu_sections = soup.find_all("div", class_="c-offerlist-item")

        for section in menu_sections:
            category = self._get_text(section.find("h4"))
            for item_elem in section.find_all("dl"):
                with contextlib.suppress(*DETAIL_PARSE_EXCEPTIONS):
                    menu_item = self._parse_menu_item(item_elem, category)
                    if menu_item is not None:
                        menu_items.append(menu_item)

        return menu_items

    def _parse_courses(self, html: str) -> list[Course]:
        """Parse course information."""
        soup = BeautifulSoup(html, "html.parser")
        courses = []

        course_items = soup.find_all("div", class_="rstdtl-course-list")
        if not course_items:
            course_items = soup.find_all("div", class_="c-offerlist-item")

        for item in course_items:
            with contextlib.suppress(*DETAIL_PARSE_EXCEPTIONS):
                course = self._parse_course(item)
                if course is not None:
                    courses.append(course)

        return courses

    def _parse_review(self, item: Any) -> Review | None:
        reviewer = self._get_required_text(item.find("a", class_="rvw-item__rvwr-name"))
        content = self._get_required_text(item.find("div", class_="rvw-item__rvw-comment"))
        title = self._get_text(item.select_one(".rvw-item__title, .rvw-item__rvw-title"), separator=" ")
        return Review(
            reviewer=reviewer,
            content=content,
            rating=self._parse_float(
                item.select_one(".rvw-item__ratings--val, .rvw-item__ratings-total .c-rating-v3__val")
                or item.find("span", class_="c-rating__val")
            ),
            visit_date=self._get_text(item.find(class_="rvw-item__date")),
            # Titles wrap across indented markup lines; keep one space between the parts.
            title=" ".join(title.split()) if title else None,
            helpful_count=self._parse_int(item.find("em", class_="rvw-item__usefulpost-count")),
        )

    def _parse_menu_item(self, item_elem: Any, category: str | None) -> MenuItem | None:
        name = self._get_required_text(item_elem.find("dt"))
        return MenuItem(
            name=name,
            price=self._get_text(item_elem.find("dd")),
            category=category,
        )

    def _parse_course(self, item: Any) -> Course | None:
        if "rstdtl-course-list" in item.get("class", []):
            return self._parse_party_course(item)

        name = self._get_required_text(item.find("h4"))
        items = []
        items_elem = item.find("ul")
        if items_elem:
            items = [item_text for element in items_elem.find_all("li") if (item_text := element.get_text(strip=True))]

        return Course(
            name=name,
            price=self._get_text(item.find("p", class_="c-offerlist-item__price")),
            description=self._get_text(item.find("p", class_="c-offerlist-item__comment")),
            items=items,
        )

    def _parse_party_course(self, item: Any) -> Course | None:
        name = self._get_required_text(item.find(class_="rstdtl-course-list__course-title-text"))
        items = []
        if item_count := self._get_text(item.find(class_="rstdtl-course-list__label")):
            items.append(item_count)

        description = self._get_text(
            item.select_one(".rstdtl-course-list__desc, .rstdtl-course-list__course-comment"), separator=" "
        )
        return Course(
            name=name,
            price=self._get_text(item.find(class_="rstdtl-course-list__price-num"), separator=" "),
            # Course text keeps the page's line breaks, but not the HTML indentation around them.
            description=re.sub(r"\s*\n\s*", "\n", description) if description else None,
            items=items,
        )

    def _get_text(self, element: Any, *, separator: str = "") -> str | None:
        return element.get_text(separator, strip=True) if element else None

    def _get_required_text(self, element: Any) -> str:
        text = self._get_text(element)
        if text is None:
            raise ValueError("required element missing")
        return text

    def _parse_float(self, element: Any) -> float | None:
        if not element:
            return None
        with contextlib.suppress(ValueError):
            return float(element.get_text(strip=True))
        return None

    def _parse_int(self, element: Any) -> int | None:
        if not element:
            return None
        with contextlib.suppress(ValueError):
            return int(element.get_text(strip=True))
        return None

    def _parse_restaurant(self, html: str, base_url: str) -> Restaurant:
        soup = BeautifulSoup(html, "html.parser")
        ld_data = self._extract_restaurant_json_ld(soup)
        info_map = self._extract_info_map(soup)

        address = self._extract_address(ld_data) or info_map.get("住所")
        traffic_text = info_map.get("交通手段")
        station = self._extract_station(soup, traffic_text)
        header_budgets = self._extract_header_budgets(soup)
        lunch_price, dinner_price = (
            header_budgets
            if header_budgets is not None
            else (self._extract_budget(soup, index=1), self._extract_budget(soup, index=0))
        )

        return Restaurant(
            name=self._extract_restaurant_name(soup, ld_data),
            url=base_url,
            rating=self._extract_rating(soup, ld_data),
            review_count=self._extract_review_count(soup, ld_data),
            save_count=self._parse_count(soup.select_one(".rdheader-rating__hozon-target .num")),
            area=self._extract_area(ld_data, address),
            station=station,
            distance=self._extract_distance(traffic_text, station),
            genres=self._extract_genres(soup, ld_data, info_map),
            description=self._extract_description(soup, ld_data),
            lunch_price=lunch_price,
            dinner_price=dinner_price,
            address=address,
            phone=self._extract_phone(info_map),
            business_hours=info_map.get("営業時間") or info_map.get("営業時間・定休日"),
            closed_days=self._get_text(soup.select_one(".rdheader-subinfo__closed-text")) or info_map.get("定休日"),
            reservation_url=self._extract_reservation_url(soup, base_url),
            image_urls=self._extract_image_urls(ld_data),
        )

    def _extract_restaurant_json_ld(self, soup: BeautifulSoup) -> dict[str, Any] | None:
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text(strip=True)
            if not raw:
                continue

            with contextlib.suppress(json.JSONDecodeError, TypeError):
                data = json.loads(raw)
                restaurant = self._find_restaurant_ld_node(data)
                if restaurant is not None:
                    return restaurant

        return None

    def _find_restaurant_ld_node(self, data: Any) -> dict[str, Any] | None:
        if isinstance(data, dict):
            node_type = data.get("@type")
            if node_type == "Restaurant" or (isinstance(node_type, list) and "Restaurant" in node_type):
                return data

            for key in ("@graph", "mainEntity"):
                if key in data and (node := self._find_restaurant_ld_node(data[key])) is not None:
                    return node

        if isinstance(data, list):
            for item in data:
                if (node := self._find_restaurant_ld_node(item)) is not None:
                    return node

        return None

    def _extract_info_map(self, soup: BeautifulSoup) -> dict[str, str]:
        info_map: dict[str, str] = {}
        for row in soup.find_all("tr"):
            header = row.find("th")
            value = row.find("td")
            if header is None or value is None:
                continue

            key = self._normalize_info_key(header.get_text(" ", strip=True))
            text = value.get_text(" ", strip=True)
            if key and text and key not in info_map:
                info_map[key] = text

        return info_map

    def _normalize_info_key(self, text: str) -> str:
        return re.sub(r"\s+", "", text)

    def _extract_restaurant_name(self, soup: BeautifulSoup, ld_data: dict[str, Any] | None) -> str:
        if ld_data and isinstance(ld_data.get("name"), str):
            return ld_data["name"].strip()

        selectors = [
            "h2.display-name",
            "h2.rdheader-rstname",
            "h2",
            "h1",
        ]
        for selector in selectors:
            if element := soup.select_one(selector):
                text = element.get_text(" ", strip=True)
                if text:
                    return text

        return ""

    def _extract_rating(self, soup: BeautifulSoup, ld_data: dict[str, Any] | None) -> float | None:
        aggregate = ld_data.get("aggregateRating") if ld_data else None
        if isinstance(aggregate, dict):
            rating_value = aggregate.get("ratingValue")
            if isinstance(rating_value, str | int | float):
                with contextlib.suppress(ValueError):
                    return float(rating_value)

        for selector in ("strong.rdheader-rating__score-val-dtl", "span.c-rating__val"):
            if value := self._parse_float(soup.select_one(selector)):
                return value

        return None

    def _extract_review_count(self, soup: BeautifulSoup, ld_data: dict[str, Any] | None) -> int | None:
        aggregate = ld_data.get("aggregateRating") if ld_data else None
        if isinstance(aggregate, dict):
            review_count = aggregate.get("reviewCount")
            if isinstance(review_count, str | int):
                with contextlib.suppress(ValueError):
                    return int(review_count)

        text = soup.get_text("\n", strip=True)
        match = re.search(r"口コミ\s*([0-9,]+)\s*人", text)
        if match:
            with contextlib.suppress(ValueError):
                return int(match.group(1).replace(",", ""))

        return None

    def _extract_address(self, ld_data: dict[str, Any] | None) -> str | None:
        address = ld_data.get("address") if ld_data else None
        if not isinstance(address, dict):
            return None

        parts = [
            address.get("addressRegion"),
            address.get("addressLocality"),
            address.get("streetAddress"),
        ]
        text = " ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
        return text or None

    def _extract_area(self, ld_data: dict[str, Any] | None, address: str | None) -> str | None:
        address_obj = ld_data.get("address") if ld_data else None
        if isinstance(address_obj, dict):
            for key in ("addressRegion", "addressLocality"):
                value = address_obj.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        if address:
            return address.split()[0]

        return None

    def _extract_station(self, soup: BeautifulSoup, traffic_text: str | None) -> str | None:
        # The header names the nearest station; the access text mixes line names into station names.
        if text := self._get_text(soup.select_one(".rdheader-subinfo__item--station .linktree__parent-target-text")):
            return text

        if traffic_text:
            if match := STATION_PATTERN.search(traffic_text):
                return match.group(1)
            return traffic_text

        text = soup.get_text("\n", strip=True)
        if match := re.search(r"最寄り駅[:：]\s*([^\n]+駅)", text):
            return match.group(1).strip()

        return None

    def _extract_distance(self, traffic_text: str | None, station: str | None) -> str | None:
        """Read the walking distance from access text such as `東銀座駅から420m`."""
        matches = DISTANCE_PATTERN.findall(traffic_text or "")
        for match_station, distance in matches:
            if match_station == station:
                return distance.replace(" ", "")
        return matches[-1][1].replace(" ", "") if matches else None

    def _extract_genres(
        self,
        soup: BeautifulSoup,
        ld_data: dict[str, Any] | None,
        info_map: dict[str, str],
    ) -> list[str]:
        serves_cuisine = ld_data.get("servesCuisine") if ld_data else None
        if isinstance(serves_cuisine, list):
            return [item.strip() for item in serves_cuisine if isinstance(item, str) and item.strip()]
        if isinstance(serves_cuisine, str) and serves_cuisine.strip():
            return [item.strip() for item in re.split(r"[、,/]", serves_cuisine) if item.strip()]

        genres_text = info_map.get("ジャンル")
        if genres_text:
            return [item.strip() for item in re.split(r"[、,/]", genres_text) if item.strip()]

        text = soup.get_text("\n", strip=True)
        if match := re.search(r"ジャンル[:：]\s*([^\n]+)", text):
            return [item.strip() for item in re.split(r"[、,/]", match.group(1)) if item.strip()]

        return []

    def _extract_description(self, soup: BeautifulSoup, ld_data: dict[str, Any] | None) -> str | None:
        if ld_data and isinstance(ld_data.get("description"), str) and ld_data["description"].strip():
            return ld_data["description"].strip()

        if pr_comment := self._extract_pr_comment(soup):
            return pr_comment

        for selector in ("p.rdheader-caption__comment", "div.rdheader-caption__comment"):
            if element := soup.select_one(selector):
                text = element.get_text(" ", strip=True)
                if text:
                    return text

        # Meta descriptions wrap the restaurant's own text in Tabelog's generic page summary.
        for meta in (
            soup.find("meta", attrs={"name": "description"}),
            soup.find("meta", attrs={"property": "og:description"}),
        ):
            if meta is not None:
                content = meta.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()

        return None

    def _extract_pr_comment(self, soup: BeautifulSoup) -> str | None:
        """Read the restaurant's own PR title and body without the collapsed-text controls."""
        title = self._get_text(soup.select_one(".pr-comment-title"))
        body = soup.select_one(".pr-comment__body")
        if body is not None:
            for control in body.select(".js-pr-comment-more"):
                control.decompose()
        body_text = self._get_text(body, separator="\n")
        return "\n".join(text for text in (title, body_text) if text) or None

    def _extract_header_budgets(self, soup: BeautifulSoup) -> tuple[str | None, str | None] | None:
        """Read lunch and dinner budgets from the header icons; `-` means no budget for that meal."""
        icons = soup.select(".rdheader-budget__icon")
        if not icons:
            return None

        budgets: dict[str, str] = {}
        for icon in icons:
            time_icon = icon.select_one(".c-rating-v3__time")
            time_classes = " ".join(time_icon.get_attribute_list("class")) if time_icon else ""
            meal = "dinner" if "--dinner" in time_classes else "lunch" if "--lunch" in time_classes else None
            if meal and (match := BUDGET_PATTERN.search(icon.get_text(" ", strip=True))):
                budgets[meal] = match.group(0)
        return budgets.get("lunch"), budgets.get("dinner")

    def _parse_count(self, element: Any) -> int | None:
        text = self._get_text(element)
        return int(text.replace(",", "")) if text and text.replace(",", "").isdigit() else None

    def _extract_budget(self, soup: BeautifulSoup, index: int) -> str | None:
        text = soup.get_text("\n", strip=True)
        budget_start = text.find("予算")
        if budget_start == -1:
            return None

        window = text[budget_start : budget_start + 200]
        matches = BUDGET_PATTERN.findall(window)
        if len(matches) > index:
            return matches[index]

        return None

    def _extract_phone(self, info_map: dict[str, str]) -> str | None:
        for key in ("予約・お問い合わせ", "お問い合わせ", "電話番号"):
            value = info_map.get(key)
            if value and (match := PHONE_PATTERN.search(value)):
                return match.group(0)

        return None

    def _extract_reservation_url(self, soup: BeautifulSoup, base_url: str) -> str | None:
        # Site-wide links such as the request-booking guide also say 予約; only links naming this restaurant count.
        restaurant_id = base_url.rstrip("/").rsplit("/", 1)[-1]
        candidates: list[tuple[int, str]] = []
        for link in soup.find_all("a", href=True):
            text = link.get_text(" ", strip=True)
            href = link.get("href")
            if not text or not isinstance(href, str) or "予約" not in text:
                continue
            if text in {"予約確認", "予約内容確認"} or restaurant_id not in href:
                continue
            # Review titles can mention 予約 and link to this restaurant's review pages.
            if not any(token in href for token in ("reserve", "booking", "yoyaku")):
                continue

            score = 0
            if "ネット予約" in text:
                score += 3
            if "予約する" in text:
                score += 2
            if any(token in href for token in ("reserve", "booking", "yoyaku")):
                score += 2
            if href.startswith(("/", "http")):
                score += 1
            candidates.append((score, urljoin(f"{base_url}/", href)))

        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]

        return None

    def _extract_image_urls(self, ld_data: dict[str, Any] | None) -> list[str]:
        if not ld_data:
            return []

        image = ld_data.get("image")
        if isinstance(image, str) and image:
            return [image]
        if isinstance(image, list):
            return [item for item in image if isinstance(item, str) and item]
        return []

    def _is_not_found_error(self, error: request_errors.HTTPError) -> bool:
        return error.response is not None and error.response.status_code == 404

    def _fetch_optional_sync(self, url: str) -> str | None:
        resp = requests.get(
            url,
            timeout=30.0,
            allow_redirects=True,
            impersonate=DEFAULT_IMPERSONATE,
        )
        try:
            resp.raise_for_status()
        except request_errors.HTTPError as e:
            if self._is_not_found_error(e):
                return None
            raise
        return resp.text

    async def _fetch_optional_async(
        self,
        client: requests.AsyncSession,
        url: str,
    ) -> str | None:
        resp = await client.get(url)
        try:
            resp.raise_for_status()
        except request_errors.HTTPError as e:
            if self._is_not_found_error(e):
                return None
            raise
        return resp.text

    def fetch_sync(self) -> RestaurantDetail:
        """Fetch restaurant details synchronously."""
        base_url = self._get_base_url()

        main_resp = requests.get(
            base_url,
            timeout=30.0,
            allow_redirects=True,
            impersonate=DEFAULT_IMPERSONATE,
        )
        main_resp.raise_for_status()
        restaurant = self._parse_restaurant(main_resp.text, base_url)

        reviews = []
        menu_items = []
        courses = []

        # Fetch reviews.
        if self.fetch_reviews:
            for page in range(1, self.max_review_pages + 1):
                review_url = f"{base_url}/dtlrvwlst/"
                if page > 1:
                    review_url += f"?PG={page}"

                resp = requests.get(
                    review_url,
                    timeout=30.0,
                    allow_redirects=True,
                    impersonate=DEFAULT_IMPERSONATE,
                )
                resp.raise_for_status()
                reviews.extend(self._parse_reviews(resp.text))

        # Fetch menu items.
        if self.fetch_menu:
            menu_url = f"{base_url}/dtlmenu/"
            if menu_html := self._fetch_optional_sync(menu_url):
                menu_items = self._parse_menu_items(menu_html)

        # Fetch courses.
        if self.fetch_courses:
            course_url = f"{base_url}/party/"
            if course_html := self._fetch_optional_sync(course_url):
                courses = self._parse_courses(course_html)

        return RestaurantDetail(
            restaurant=restaurant,
            reviews=reviews,
            menu_items=menu_items,
            courses=courses,
        )

    async def fetch(self) -> RestaurantDetail:
        """Fetch restaurant details asynchronously."""
        base_url = self._get_base_url()

        reviews = []
        menu_items = []
        courses = []

        async with requests.AsyncSession(
            timeout=30.0,
            allow_redirects=True,
            impersonate=DEFAULT_IMPERSONATE,
        ) as client:
            main_resp = await client.get(base_url)
            main_resp.raise_for_status()
            restaurant = self._parse_restaurant(main_resp.text, base_url)

            # Fetch reviews.
            if self.fetch_reviews:
                for page in range(1, self.max_review_pages + 1):
                    review_url = f"{base_url}/dtlrvwlst/"
                    if page > 1:
                        review_url += f"?PG={page}"

                    resp = await client.get(review_url)
                    resp.raise_for_status()
                    reviews.extend(self._parse_reviews(resp.text))

            # Fetch menu items.
            if self.fetch_menu:
                menu_url = f"{base_url}/dtlmenu/"
                if menu_html := await self._fetch_optional_async(client, menu_url):
                    menu_items = self._parse_menu_items(menu_html)

            # Fetch courses.
            if self.fetch_courses:
                course_url = f"{base_url}/party/"
                if course_html := await self._fetch_optional_async(client, course_url):
                    courses = self._parse_courses(course_html)

        return RestaurantDetail(
            restaurant=restaurant,
            reviews=reviews,
            menu_items=menu_items,
            courses=courses,
        )

    async def fetch_browser(self) -> RestaurantDetail:
        """Fetch restaurant details with the explicitly requested headed browser, reading every page in one page."""
        from .browser import BrowserRetrievalError
        from .browser import fetch_documents

        base_url = self._get_base_url()
        review_urls = [
            f"{base_url}/dtlrvwlst/" + (f"?PG={page}" if page > 1 else "")
            for page in range(1, self.max_review_pages + 1)
            if self.fetch_reviews
        ]
        menu_url = f"{base_url}/dtlmenu/"
        course_url = f"{base_url}/party/"
        urls = [base_url, *review_urls]
        if self.fetch_menu:
            urls.append(menu_url)
        if self.fetch_courses:
            urls.append(course_url)
        documents = dict(zip(urls, await fetch_documents(urls, 30.0), strict=True))

        def read(url: str) -> str:
            document = documents[url]
            if document.status >= 400:
                raise BrowserRetrievalError(
                    f"HTTP Error {document.status}: Browser detail request failed for {url}",
                    status=document.status,
                )
            return document.html

        def read_optional(url: str) -> str | None:
            return None if documents[url].status == 404 else read(url)

        restaurant = self._parse_restaurant(read(base_url), base_url)
        reviews = [review for url in review_urls for review in self._parse_reviews(read(url))]
        menu_html = read_optional(menu_url) if self.fetch_menu else None
        course_html = read_optional(course_url) if self.fetch_courses else None
        return RestaurantDetail(
            restaurant=restaurant,
            reviews=reviews,
            menu_items=self._parse_menu_items(menu_html) if menu_html else [],
            courses=self._parse_courses(course_html) if course_html else [],
        )
