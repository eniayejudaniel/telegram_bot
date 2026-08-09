from __future__ import annotations

import unittest

from ebay_monitor.monitor import fetch_keyword_pages


class PartialFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_keeps_first_page_when_later_page_times_out(self) -> None:
        class Client:
            async def search_html(self, keyword: str, **kwargs: object) -> tuple[str, str]:
                if kwargs["page"] == 2:
                    raise TimeoutError("test timeout")
                return '<a class="pagination__next">next</a>', "https://example.test/page1"

        pages = await fetch_keyword_pages(
            Client(),
            "pokemon",
            buying_option="bin",
            limit=100,
            max_pages=2,
        )
        self.assertEqual(pages, [('<a class="pagination__next">next</a>', "https://example.test/page1")])


if __name__ == "__main__":
    unittest.main()
